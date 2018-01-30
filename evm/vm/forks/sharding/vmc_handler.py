import logging

from cytoolz import (
    pipe,
)

import rlp

from web3.contract import (
    Contract,
)

from eth_utils import (
    event_signature_to_log_topic,
    is_canonical_address,
    to_checksum_address,
    to_dict,
    to_tuple,
)

from evm.rlp.sedes import (
    address,
    hash32,
)

from evm.utils.hexadecimal import (
    encode_hex,
    decode_hex,
)
from evm.utils.numeric import (
    big_endian_to_int,
)

from evm.vm.forks.sharding.config import (
    get_sharding_config,
)


class NextLogUnavailable(Exception):
    pass


class UnknownShard(Exception):
    pass


@to_dict
def parse_collation_added_data(data_hex):
    data_bytes = decode_hex(data_hex)
    score = big_endian_to_int(data_bytes[-32:])
    is_new_head = bool(big_endian_to_int(data_bytes[-64:-32]))
    header_bytes = data_bytes[:-64]
    # [num, num, bytes32, bytes32, bytes32, address, bytes32, bytes32, num, bytes]
    sedes = rlp.sedes.List([
        rlp.sedes.big_endian_int,
        rlp.sedes.big_endian_int,
        hash32,
        hash32,
        hash32,
        address,
        hash32,
        hash32,
        rlp.sedes.big_endian_int,
        rlp.sedes.binary,
    ])
    header_values = rlp.decode(header_bytes, sedes=sedes)
    yield 'header', header_values
    yield 'is_new_head', is_new_head
    yield 'score', score


class ShardTracker:
    '''Track logs `CollationAdded` in mainchain
    '''
    # For handling logs filtering
    # Event:
    #   CollationAdded(indexed uint256 shard, bytes collationHeader, bool isNewHead, uint256 score)
    COLLATION_ADDED_TOPIC = event_signature_to_log_topic(
        "CollationAdded(int128,bytes4096,bool,int128)"
    )
    # older <---------------> newer
    current_score = None
    new_logs = None
    unchecked_logs = None

    def __init__(self, shard_id, log_handler, vmc_address):
        # TODO: currently set one log_handler for each shard. Should see if there is a better way
        #       to make one log_handler shared over all shards.
        self.shard_id = shard_id
        self.log_handler = log_handler
        self.vmc_address = vmc_address
        self.current_score = None
        # older <---------------> newer
        self.new_logs = []
        self.unchecked_logs = []

    @to_tuple
    def _get_new_logs(self):
        shard_id_topic_hex = encode_hex(self.shard_id.to_bytes(32, byteorder='big'))
        new_logs = self.log_handler.get_new_logs(
            address=self.vmc_address,
            topics=[
                encode_hex(self.COLLATION_ADDED_TOPIC),
                shard_id_topic_hex,
            ],
        )
        for log in new_logs:
            yield parse_collation_added_data(log['data'])

    def get_next_log(self):
        new_logs = self._get_new_logs()
        self.new_logs.extend(new_logs)
        if len(self.new_logs) == 0:
            raise NextLogUnavailable("No more next logs")
        return self.new_logs.pop()

    # TODO: this method may return wrong result when new logs arrive before the logs inside
    #       `self.new_logs` are consumed entirely. This issue can be resolved by saving the
    #       status of `new_logs`, `unchecked_logs`, and `current_score`, when it start to run
    #       `GUESS_HEAD`. If there is a new block arriving, just restore them to the saved status,
    #       append new logs to `new_logs`, and re-run `GUESS_HEAD`
    def fetch_candidate_head(self):
        # Try to return a log that has the score that we are checking for,
        # checking in order of oldest to most recent.
        unchecked_logs = pipe(
            self.unchecked_logs,
            enumerate,
            tuple,
            reversed,
            tuple,
        )
        current_score = self.current_score

        for idx, logs_entry in unchecked_logs:
            if logs_entry['score'] == current_score:
                return self.unchecked_logs.pop(idx)
        # If no further recorded but unchecked logs exist, go to the next
        # is_new_head = true log
        while True:
            # TODO: currently just raise when there is no log anymore
            log_entry = self.get_next_log()
            if log_entry['is_new_head']:
                break
            self.unchecked_logs.append(log_entry)
        self.current_score = log_entry['score']
        return log_entry


class VMC(Contract):

    logger = logging.getLogger("evm.chain.sharding.VMC")

    def __init__(self, *args, default_privkey, **kwargs):
        self.default_privkey = default_privkey
        self.default_sender_address = default_privkey.public_key.to_canonical_address()
        self.config = get_sharding_config()
        self.shard_trackers = {}

        super().__init__(*args, **kwargs)

    def set_shard_tracker(self, shard_id, shard_tracker):
        self.shard_trackers[shard_id] = shard_tracker

    def get_shard_tracker(self, shard_id):
        if shard_id not in self.shard_trackers:
            raise UnknownShard('Shard {} is not tracked'.format(shard_id))
        return self.shard_trackers[shard_id]

    # TODO: currently just calls `shard_tracker.get_next_log`
    def get_next_log(self, shard_id):
        shard_tracker = self.get_shard_tracker(shard_id)
        return shard_tracker.get_next_log()

    # TODO: currently just calls `shard_tracker.fetch_candidate_head`
    def fetch_candidate_head(self, shard_id):
        shard_tracker = self.get_shard_tracker(shard_id)
        return shard_tracker.fetch_candidate_head()

    @to_dict
    def mk_build_transaction_detail(self,
                                    nonce,
                                    gas,
                                    chain_id=None,
                                    value=None,
                                    gas_price=None,
                                    data=None):
        if not (isinstance(nonce, int) and nonce >= 0):
            raise ValueError('nonce should be provided as non-negative integer')
        if not (isinstance(gas, int) and gas > 0):
            raise ValueError('gas should be provided as positive integer')
        yield 'nonce', nonce
        yield 'gas', gas
        yield 'chainId', chain_id
        if value is not None:
            yield 'value', value
        if gas_price is not None:
            yield 'gasPrice', gas_price
        if data is not None:
            yield 'data', data

    def send_transaction(self,
                         func_name,
                         args,
                         nonce=None,
                         chain_id=None,
                         gas=None,
                         value=0,
                         gas_price=None,
                         data=None):
        if gas is None:
            gas = self.config['DEFAULT_GAS']
        if gas_price is None:
            gas_price = self.config['GAS_PRICE']
        privkey = self.default_privkey
        if nonce is None:
            nonce = self.web3.eth.getTransactionCount(privkey.public_key.to_checksum_address())
        build_transaction_detail = self.mk_build_transaction_detail(
            nonce=nonce,
            gas=gas,
            chain_id=chain_id,
            value=value,
            gas_price=gas_price,
            data=data,
        )
        build_transaction_instance = self.buildTransaction(build_transaction_detail)
        func_instance = getattr(build_transaction_instance, func_name)
        unsigned_transaction = func_instance(*args)
        signed_transaction_dict = self.web3.eth.account.signTransaction(
            unsigned_transaction,
            privkey.to_hex(),
        )
        tx_hash = self.web3.eth.sendRawTransaction(signed_transaction_dict['rawTransaction'])
        return tx_hash

    @to_dict
    def mk_contract_tx_detail(self,
                              sender_address,
                              gas,
                              value=None,
                              gas_price=None,
                              data=None):
        # Both 'from' and 'gas' are required in eth_tester
        if not is_canonical_address(sender_address):
            raise ValueError('sender_address should be provided in the canonical format')
        if not (isinstance(gas, int) and gas > 0):
            raise ValueError('gas should be provided as positive integer')
        yield 'from', to_checksum_address(sender_address)
        yield 'gas', gas
        if value is not None:
            yield 'value', value
        if gas_price is not None:
            yield 'gas_price', gas_price
        if data is not None:
            yield 'data', data

    # contract calls ##############################################

    def get_eligible_proposer(self, shard_id, period=None, gas=None):
        """get_eligible_proposer(shard_id: num, period: num) -> address
        """
        if gas is None:
            gas = self.config['DEFAULT_GAS']
        if period is None:
            period = self.web3.eth.blockNumber // self.config['PERIOD_LENGTH']
        tx_detail = self.mk_contract_tx_detail(sender_address=self.default_sender_address, gas=gas)
        address_in_hex = self.call(tx_detail).get_eligible_proposer(shard_id, period)
        return decode_hex(address_in_hex)

    def deposit(self, gas=None, gas_price=None):
        """deposit() -> num
        """
        tx_hash = self.send_transaction(
            'deposit',
            [],
            value=self.config['DEPOSIT_SIZE'],
            gas=gas,
            gas_price=gas_price,
        )
        return tx_hash

    def withdraw(self, validator_index, sig, gas=None, gas_price=None):
        """withdraw(validator_index: num, sig: bytes <= 1000) -> bool
        """
        tx_hash = self.send_transaction(
            'withdraw',
            [
                validator_index,
                sig,
            ],
            gas=gas,
            gas_price=gas_price,
        )
        return tx_hash

    def add_header(self, header, gas=None, gas_price=None):
        """add_header(header: bytes <= 4096) -> bool
        """
        tx_hash = self.send_transaction(
            'add_header',
            [header],
            gas=gas,
            gas_price=gas_price,
        )
        return tx_hash

    def tx_to_shard(self,
                    to,
                    shard_id,
                    tx_startgas,
                    tx_gasprice,
                    data,
                    value,
                    gas=None,
                    gas_price=None):
        """tx_to_shard(
            to: address, shard_id: num, tx_startgas: num, tx_gasprice: num, data: bytes <= 4096
           ) -> num
        """
        tx_hash = self.send_transaction(
            'tx_to_shard',
            [
                to_checksum_address(to),
                shard_id,
                tx_startgas,
                tx_gasprice,
                data,
            ],
            value=value,
            gas=gas,
            gas_price=gas_price,
        )
        return tx_hash

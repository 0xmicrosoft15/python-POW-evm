from eth.vm.forks.frontier.constants import REFUND_SELFDESTRUCT
from typing import Type

from eth_hash.auto import keccak
from eth_utils.exceptions import ValidationError
from eth_utils import (
    encode_hex,
)

from eth.abc import (
    AccountDatabaseAPI,
    ComputationAPI,
    MessageAPI,
    SignedTransactionAPI,
    StateAPI,
    TransactionContextAPI,
    TransactionExecutorAPI,
)
from eth.constants import (
    CREATE_CONTRACT_ADDRESS,
    SECPK1_N,
)
from eth.db.account import (
    AccountDB
)
from eth.vm.message import (
    Message,
)
from eth.vm.forks.berlin.state import (
    BerlinState,
    BerlinTransactionExecutor,
)
from eth._utils.address import (
    generate_contract_address,
)

from .computation import LondonComputation
from .validation import validate_london_normalized_transaction


class LondonTransactionExecutor(BerlinTransactionExecutor):
    def build_evm_message(self, transaction: SignedTransactionAPI) -> MessageAPI:
        # Use vm_state.get_gas_price instead of transaction_context.gas_price so
        #   that we can run get_transaction_result (aka~ eth_call) and estimate_gas.
        #   Both work better if the GASPRICE opcode returns the original real price,
        #   but the sender's balance doesn't actually deduct the gas. This get_gas_price()
        #   will return 0 for eth_call, but transaction_context.gas_price will return
        #   the same value as the GASPRICE opcode.
        gas_fee = transaction.gas * self.vm_state.get_gas_price(transaction)

        # Buy Gas
        self.vm_state.delta_balance(transaction.sender, -1 * gas_fee)

        # Increment Nonce
        self.vm_state.increment_nonce(transaction.sender)

        # Setup VM Message
        message_gas = transaction.gas - transaction.intrinsic_gas

        if transaction.to == CREATE_CONTRACT_ADDRESS:
            contract_address = generate_contract_address(
                transaction.sender,
                self.vm_state.get_nonce(transaction.sender) - 1,
            )
            data = b''
            code = transaction.data
        else:
            contract_address = None
            data = transaction.data
            code = self.vm_state.get_code(transaction.to)

        self.vm_state.logger.debug2(
            (
                "TRANSACTION: sender: %s | to: %s | value: %s | gas: %s | "
                "max_priority_fee_per_gas: %s | max_fee_per_gas: %s | s: %s | "
                "r: %s | y_parity: %s | data-hash: %s"
            ),
            encode_hex(transaction.sender),
            encode_hex(transaction.to),
            transaction.value,
            transaction.gas,
            transaction.max_priority_fee_per_gas,
            transaction.max_fee_per_gas,
            transaction.s,
            transaction.r,
            transaction.y_parity,
            encode_hex(keccak(transaction.data)),
        )

        message = Message(
            gas=message_gas,
            to=transaction.to,
            sender=transaction.sender,
            value=transaction.value,
            data=data,
            code=code,
            create_address=contract_address,
        )
        return message

    def finalize_computation(
            self,
            transaction: SignedTransactionAPI,
            computation: ComputationAPI
    ) -> ComputationAPI:
        transaction_context = self.vm_state.get_transaction_context(transaction)

        # Self Destruct Refunds
        num_deletions = len(computation.get_accounts_for_deletion())
        if num_deletions:
            computation.refund_gas(REFUND_SELFDESTRUCT * num_deletions)

        # Gas Refunds
        gas_remaining = computation.get_gas_remaining()
        gas_refunded = computation.get_gas_refund()
        gas_used = transaction.gas - gas_remaining
        gas_refund = min(gas_refunded, gas_used // 2)
        gas_refund_amount = (gas_refund + gas_remaining) * transaction_context.gas_price

        if gas_refund_amount:
            self.vm_state.logger.debug2(
                'TRANSACTION REFUND: %s -> %s',
                gas_refund_amount,
                encode_hex(computation.msg.sender),
            )

            self.vm_state.delta_balance(computation.msg.sender, gas_refund_amount)

        # Miner Fees
        transaction_fee = \
            (transaction.gas - gas_remaining - gas_refund) * transaction.max_priority_fee_per_gas
        self.vm_state.logger.debug2(
            'TRANSACTION FEE: %s -> %s',
            transaction_fee,
            encode_hex(self.vm_state.coinbase),
        )
        self.vm_state.delta_balance(self.vm_state.coinbase, transaction_fee)

        # Process Self Destructs
        for account, _ in computation.get_accounts_for_deletion():
            # TODO: need to figure out how we prevent multiple selfdestructs from
            # the same account and if this is the right place to put this.
            self.vm_state.logger.debug2('DELETING ACCOUNT: %s', encode_hex(account))

            # TODO: this balance setting is likely superflous and can be
            # removed since `delete_account` does this.
            self.vm_state.set_balance(account, 0)
            self.vm_state.delete_account(account)

        return computation


class LondonState(BerlinState):
    account_db_class: Type[AccountDatabaseAPI] = AccountDB
    computation_class = LondonComputation
    transaction_executor_class: Type[TransactionExecutorAPI] = LondonTransactionExecutor

    def validate_transaction(
        self,
        transaction: SignedTransactionAPI
    ) -> None:
        # frontier validation (without the gas price checks)
        sender_nonce = self.get_nonce(transaction.sender)
        if sender_nonce != transaction.nonce:
            raise ValidationError(
                f"Invalid transaction nonce: Expected {sender_nonce}, but got {transaction.nonce}"
            )

        # homestead validation
        if transaction.s > SECPK1_N // 2 or transaction.s == 0:
            raise ValidationError("Invalid signature S value")

        validate_london_normalized_transaction(
            state=self,
            transaction=transaction,
            base_fee_per_gas=self.execution_context.base_fee_per_gas
        )

    def get_transaction_context(self: StateAPI,
                                transaction: SignedTransactionAPI) -> TransactionContextAPI:
        """
        London-specific transaction context creation,
        where gas_price includes the block base fee
        """
        effective_gas_price = min(
            transaction.max_priority_fee_per_gas + self.execution_context.base_fee_per_gas,
            transaction.max_fee_per_gas,
        )
        # See how this reduces in a pre-1559 transaction:
        # 1. effective_gas_price = min(
        #     transaction.gas_price + self.execution_context.base_fee_per_gas,
        #     transaction.gas_price,
        # )
        # base_fee_per_gas is non-negative, so:
        # 2. effective_gas_price = transaction.gas_price

        return self.get_transaction_context_class()(
            gas_price=effective_gas_price,
            origin=transaction.sender
        )

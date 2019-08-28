from functools import partial

from eth_keys.datatypes import PrivateKey
from eth_typing import Address

from eth.vm.forks.constantinople.transactions import (
    ConstantinopleTransaction,
    ConstantinopleUnsignedTransaction,
)

from eth._utils.transactions import (
    create_transaction_signature,
    calculate_intrinsic_gas,
)
from eth.vm.forks.homestead.transactions import (
    HOMESTEAD_TX_GAS_SCHEDULE,
)

from .constants import (
    GAS_TXDATANONZERO_EIP2028,
)

ISTANBUL_TX_GAS_SCHEDULE = HOMESTEAD_TX_GAS_SCHEDULE._replace(
    gas_txdatanonzero=GAS_TXDATANONZERO_EIP2028,
)


istanbul_get_intrinsic_gas = partial(calculate_intrinsic_gas, ISTANBUL_TX_GAS_SCHEDULE)


class IstanbulTransaction(ConstantinopleTransaction):
    @classmethod
    def create_unsigned_transaction(cls,
                                    *,
                                    nonce: int,
                                    gas_price: int,
                                    gas: int,
                                    to: Address,
                                    value: int,
                                    data: bytes) -> 'IstanbulUnsignedTransaction':
        return IstanbulUnsignedTransaction(nonce, gas_price, gas, to, value, data)

    def get_intrinsic_gas(self) -> int:
        return istanbul_get_intrinsic_gas(self)


class IstanbulUnsignedTransaction(ConstantinopleUnsignedTransaction):
    def as_signed_transaction(self,
                              private_key: PrivateKey,
                              chain_id: int=None) -> IstanbulTransaction:
        v, r, s = create_transaction_signature(self, private_key, chain_id=chain_id)
        return IstanbulTransaction(
            nonce=self.nonce,
            gas_price=self.gas_price,
            gas=self.gas,
            to=self.to,
            value=self.value,
            data=self.data,
            v=v,
            r=r,
            s=s,
        )

    def get_intrinsic_gas(self) -> int:
        return istanbul_get_intrinsic_gas(self)

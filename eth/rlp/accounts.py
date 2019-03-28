import rlp
from rlp.sedes import (
    big_endian_int,
)

from eth.constants import (
    EMPTY_SHA3,
    BLANK_ROOT_HASH,
)

from .sedes import (
    trie_root,
    hash32,
)

from typing import Any


class Account(rlp.Serializable):
    """
    RLP object for accounts.
    """
    fields = [
        ('nonce', big_endian_int),
        ('balance', big_endian_int),
        ('storage_root', trie_root),
        ('code_hash', hash32)
    ]

    def __init__(self,
                 nonce: int=0,
                 balance: int=0,
                 storage_root: bytes=BLANK_ROOT_HASH,
                 code_hash: bytes=EMPTY_SHA3,
                 **kwargs: Any) -> None:
        super().__init__(nonce, balance, storage_root, code_hash, **kwargs)

    def __repr__(self):
        return "Account(nonce=%d, balance=%d, storage_root=0x%s, code_hash=0x%s)" % (
            self.nonce,
            self.balance,
            self.storage_root.hex(),
            self.code_hash.hex(),
        )

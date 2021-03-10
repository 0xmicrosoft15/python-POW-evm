import copy
from typing import Dict

from eth_utils.toolz import merge

from eth.vm import (
    mnemonics,
    opcode_values,
)
from eth.vm.opcode import (
    Opcode,
    as_opcode,
)
from eth import constants

from eth.vm.forks.byzantium.opcodes import (
    ensure_no_static,
)
from eth.vm.forks.muir_glacier.opcodes import (
    MUIR_GLACIER_OPCODES,
)

from . import logic


UPDATED_OPCODES: Dict[int, Opcode] = {
    opcode_values.BALANCE: as_opcode(
        gas_cost=constants.GAS_NULL,
        logic_fn=logic.balance_eip2929,
        mnemonic=mnemonics.BALANCE,
    ),
    opcode_values.EXTCODESIZE: as_opcode(
        gas_cost=constants.GAS_NULL,
        logic_fn=logic.extcodesize_eip2929,
        mnemonic=mnemonics.EXTCODESIZE,
    ),
    opcode_values.EXTCODECOPY: as_opcode(
        gas_cost=constants.GAS_NULL,
        logic_fn=logic.extcodecopy_eip2929,
        mnemonic=mnemonics.EXTCODECOPY,
    ),
    opcode_values.EXTCODEHASH: as_opcode(
        gas_cost=constants.GAS_NULL,
        logic_fn=logic.extcodehash_eip2929,
        mnemonic=mnemonics.EXTCODEHASH,
    ),
    opcode_values.SLOAD: as_opcode(
        gas_cost=constants.GAS_NULL,
        logic_fn=logic.sload_eip2929,
        mnemonic=mnemonics.SLOAD,
    ),
    opcode_values.SSTORE: as_opcode(
        logic_fn=ensure_no_static(logic.sstore_eip2929),
        mnemonic=mnemonics.SSTORE,
        gas_cost=constants.GAS_NULL,
    ),
    # System opcodes
    opcode_values.STATICCALL: logic.StaticCallEIP2929.configure(
        __name__='opcode:STATICCALL',
        mnemonic=mnemonics.STATICCALL,
        gas_cost=constants.GAS_NULL,
    )(),
    opcode_values.CALL: logic.CallEIP2929.configure(
        __name__='opcode:CALL',
        mnemonic=mnemonics.CALL,
        gas_cost=constants.GAS_NULL,
    )(),
}


BERLIN_OPCODES = merge(
    copy.deepcopy(MUIR_GLACIER_OPCODES),
    UPDATED_OPCODES,
)

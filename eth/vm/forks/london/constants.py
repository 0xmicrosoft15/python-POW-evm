from eth.vm.forks.berlin.constants import (
    ACCESS_LIST_ADDRESS_COST_EIP_2930,
    ACCESS_LIST_STORAGE_KEY_COST_EIP_2930,
)

# EIP 1559
DYNAMIC_FEE_TRANSACTION_TYPE = 2
DYNAMIC_FEE_ADDRESS_COST = ACCESS_LIST_ADDRESS_COST_EIP_2930
DYNAMIC_FEE_STORAGE_KEY_COST = ACCESS_LIST_STORAGE_KEY_COST_EIP_2930

BASE_FEE_MAX_CHANGE_DENOMINATOR = 8
INITIAL_BASE_FEE = 1000000000
ELASTICITY_MULTIPLIER = 2

EIP3541_RESERVED_STARTING_BYTE = b'\xef'
EIP3529_MAX_REFUND_QUOTIENT = 5

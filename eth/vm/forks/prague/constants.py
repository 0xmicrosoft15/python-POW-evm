from eth_typing import (
    Address,
)

# EIP-7702 #
SET_CODE_TRANSACTION_TYPE = 4
MAGIC = 5
PER_AUTH_BASE_COST = 12_500
PER_EMPTY_ACCOUNT_BASE_COST = 25_000
DELEGATION_DESIGNATION = b"\xef\x01\x00"

# EIP-7623 #
STANDARD_TOKEN_COST = 4
TOTAL_COST_FLOOR_PER_TOKEN = 10

# EIP-2935 #
HISTORY_SERVE_WINDOW = 8_191
HISTORY_STORAGE_ADDRESS = Address(
    b"\x00\x00\xf9\x08'\xf1\xc5:\x10\xcbz\x023[\x17S \x00)5"
)
HISTORY_STORAGE_CONTRACT_CODE = b"3s\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xfe\x14`FW` 6\x03`BW_5`\x01C\x03\x81\x11`BWa\x1f\xff\x81C\x03\x11`BWa\x1f\xff\x90\x06T_R` _\xf3[__\xfd[_5a\x1f\xff`\x01C\x03\x06U\x00"  # noqa: E501

# EIP-7691 #
MAX_BLOB_GAS_PER_BLOCK = 1_179_648
TARGET_BLOB_GAS_PER_BLOCK = 786_432
BLOB_BASE_FEE_UPDATE_FRACTION_PRAGUE = 5_007_716

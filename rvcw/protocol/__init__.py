"""Vendored pure-Python protocol layer from phurth/ha-onecontrol (MIT)."""
from .cobs import CobsByteDecoder, cobs_encode
from .crc8 import crc8
from .tea import (
    CAN_BLE_KEY_SEED_CIPHER,
    X180T_KEY_SEED_CIPHER,
    calculate_can_ble_key_seed_key,
    calculate_step1_key,
    calculate_step2_key,
    tea_encrypt,
)
from .ids_can_wire import (
    IdsCanWireFrame,
    compose_ids_can_extended_wire_frame,
    compose_ids_can_standard_wire_frame,
    parse_ids_can_wire_frame,
)
from .function_names import FUNCTION_NAMES
from .const import (  # noqa: F401
    CAN_READ_CHAR_UUID,
    CAN_VERSION_CHAR_UUID,
    CAN_WRITE_CHAR_UUID,
    DATA_READ_CHAR_UUID,
    DATA_WRITE_CHAR_UUID,
    KEY_CHAR_UUID,
    PASSWORD_UNLOCK_CHAR_UUID,
    SEED_CHAR_UUID,
    UNLOCK_STATUS_CHAR_UUID,
)
import pytest

from rvcw.protocol import (
    CAN_WRITE_CHAR_UUID,
    compose_ids_can_extended_wire_frame,
    crc8,
    cobs_encode,
    parse_ids_can_wire_frame,
    tea_encrypt,
)


def test_crc8_init_value():
    assert crc8(b"") == 0x55


def test_cobs_encode_wraps_payload():
    encoded = cobs_encode(b"\x01\x02")
    assert encoded[0] == 0x00 and encoded[-1] == 0x00  # frame delimiters
    assert len(encoded) > 2


def test_extended_frame_roundtrip():
    frame = compose_ids_can_extended_wire_frame(0x82, 0x2E, 0x41, 0x01, b"")
    assert frame[0] == 0  # dlc
    wire = parse_ids_can_wire_frame(frame)
    assert wire.message_type == 0x82
    assert wire.source_address == 0x2E
    assert wire.target_address == 0x41
    assert wire.message_data == 0x01


def test_tea_encrypt_is_deterministic():
    assert tea_encrypt(0x9E3779B9, 0) == tea_encrypt(0x9E3779B9, 0)


def test_can_write_uuid():
    assert CAN_WRITE_CHAR_UUID == "00000001-0200-a58e-e411-afe28044e62c"
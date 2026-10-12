import pytest

from rvcw.ble.transport import GatewayLink, LinkError, decode_notification


def test_v2_packed_notification_yields_four_frames():
    # 1 byte type + 18 header bytes + status data, per the V2 Packed layout
    raw = bytes([0x01, 0x41]) + b"\x00" + b"\x12" + b"\x00\x2E" + b"\x00\x00" * 5
    raw = raw[:19] + b"\x01"  # status_len=1 placeholder at index 18
    frames = decode_notification(raw)
    assert len(frames) == 4


def test_v2_twentynine_bit_notification_yields_one_frame():
    id4 = bytes.fromhex("80BA4101")  # 29-bit COMMAND frame id (0x82, 0x2E, 0x41, 0x01)
    raw = bytes([0x03]) + id4 + bytes([0x00])
    frames = decode_notification(raw)
    assert len(frames) == 1
    assert frames[0].message_type == 0x82
    assert frames[0].source_address == 0x2E
    assert frames[0].target_address == 0x41


def test_non_v2_bytes_fall_back_to_wire_parse():
    # Valid raw 11-bit wire frame: [dlc=6][id 0x832E (type 3, src 0x2E)][6 payload bytes].
    # First byte 0x06 is outside the V2 notification types (1/2/3).
    raw = bytes([0x06, 0x83, 0x2E, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06])
    frames = decode_notification(raw)
    assert len(frames) == 1
    assert frames[0].message_type == 0x03
    assert frames[0].source_address == 0x2E


def test_unconnected_write_raises():
    link = GatewayLink("10:06:1C:E7:B8:3E")
    with pytest.raises(LinkError):
        link.client

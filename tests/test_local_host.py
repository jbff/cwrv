import pytest

from rvcw.ble.can import (
    choose_local_host_address,
    identity_frames,
    pseudo_mac,
)
from rvcw.protocol import parse_ids_can_wire_frame


def test_pseudo_mac_is_stable_and_nonzero():
    a, b = pseudo_mac("rvcw:X"), pseudo_mac("rvcw:X")
    assert a == b and len(a) == 6 and a != bytes(6)
    assert pseudo_mac("other") != a


def test_choose_avoids_observed_and_reserved():
    observed = {1, 2, 3}
    addr = choose_local_host_address(observed, pseudo_mac("rvcw:X"))
    assert addr not in observed and addr not in (0x00, 0xFF)
    assert 0x01 <= addr <= 0xFE


def test_choose_deterministic_for_same_mac():
    assert choose_local_host_address(set(), pseudo_mac("a")) == choose_local_host_address(
        set(), pseudo_mac("a")
    )


def test_identity_frames_parse_back():
    mac = pseudo_mac("rvcw:X")
    frames = identity_frames(0x2D, mac)
    kinds = [parse_ids_can_wire_frame(f) for f in frames]
    assert [w.message_type for w in kinds] == [0x00, 0x06, 0x02, 0x01, 0x03]
    # DEVICE_ID frame: product_id=46 big-endian, device_type=22, function_name=2
    dev = kinds[2]
    assert int.from_bytes(dev.payload[0:2], "big") == 46
    assert dev.payload[3] == 22
    assert int.from_bytes(dev.payload[4:6], "big") == 2
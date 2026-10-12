"""IDS-CAN participant identity: LocalHost claim, identity frames, commands."""
from __future__ import annotations

import asyncio
import hashlib

from rvcw.ble.transport import GatewayLink
from rvcw.protocol import (
    IdsCanWireFrame,
    compose_ids_can_standard_wire_frame,
    parse_ids_can_wire_frame,
)

PRODUCT_ID_ANDROID_APP = 46  # OneControl Android Mobile App (0x002E)
DEVICE_TYPE_ANDROID = 22  # ANDROID_MOBILE_DEVICE
FUNCTION_NAME_MYRV_TABLET = 2


def pseudo_mac(key: str) -> bytes:
    """Stable 6-byte pseudo-MAC for this rvcw LocalHost (mirrors upstream CanAdapterFactory)."""
    mac = bytearray(hashlib.sha256(key.encode("utf-8")).digest()[:6])
    if all(b == 0x00 for b in mac):
        mac[0] = 0x01
    return bytes(mac)


def choose_local_host_address(observed: set[int], mac: bytes, seed_text: str = "rvcw") -> int:
    """Deterministically pick an unclaimed participant address (never 0x00/0xFF).

    Candidates are the free addresses 0x01..0xFE; the sha256-derived start index
    picks one deterministically from (mac, seed_text). Falls back to 0xFA only
    when every candidate address is already observed.
    """
    candidates = [c for c in range(0x01, 0xFF) if c not in observed]
    if not candidates:
        return 0xFA
    seed = hashlib.sha256(mac + seed_text.encode("utf-8")).digest()
    start = int.from_bytes(seed[:2], "big") % len(candidates)
    return candidates[start]


def identity_frames(source: int, mac: bytes) -> list[bytes]:
    """The five LocalHost identity frames, in official LocalDevice order."""
    s = source & 0xFF
    return [
        compose_ids_can_standard_wire_frame(0x00, s, bytes([0x00, 0x12]) + mac),  # NETWORK online
        compose_ids_can_standard_wire_frame(0x06, s, b"\x00"),  # PRODUCT_STATUS
        compose_ids_can_standard_wire_frame(  # DEVICE_ID
            0x02,
            s,
            bytes(
                [
                    0x00,
                    0x2E,  # product_id 46 (OneControl Android app), big-endian
                    s,  # product instance == this participant address
                    DEVICE_TYPE_ANDROID,  # 22 (ANDROID_MOBILE_DEVICE)
                    0x00,
                    0x02,  # function_name 2 (MYRV_TABLET), big-endian
                    0x00,  # device instance 0 | function instance 0
                    0x00,  # capabilities 0
                ]
            ),
        ),
        compose_ids_can_standard_wire_frame(0x01, s, b"\x00\x00\x00\x00"),  # CIRCUIT_ID
        compose_ids_can_standard_wire_frame(0x03, s, b""),  # DEVICE_STATUS
    ]


async def claim_local_host(link: GatewayLink, observed: set[int]) -> int:
    """Claim and advertise a LocalHost address; returns the claimed address."""
    mac = pseudo_mac(f"rvcw:{link.mac}")
    claimed = choose_local_host_address(observed, mac)
    await asyncio.sleep(1.05)  # official one-second address-claim listen window
    claim = compose_ids_can_standard_wire_frame(  # src 0xFF broadcast during claim
        0x00, 0xFF, bytes([claimed, 0x12]) + mac
    )
    await link.write_can_frame(claim)
    await asyncio.sleep(1.05)  # official claim window
    link.local_host_address = claimed
    for frame in identity_frames(claimed, mac):
        await link.write_can_frame(frame)
        await asyncio.sleep(0.04)
    return claimed
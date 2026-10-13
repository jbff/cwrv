"""IDS-CAN participant identity: LocalHost claim, identity frames, commands."""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING

from rvcw.ble.transport import GatewayLink, LinkError
from rvcw.protocol import (
    IdsCanWireFrame,
    compose_ids_can_extended_wire_frame,
    compose_ids_can_standard_wire_frame,
    parse_ids_can_wire_frame,
)

if TYPE_CHECKING:
    from rvcw.ble.session import RemoteControl

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


def relay_command(source: int, target: int, on: bool) -> bytes:
    """RelayBasicLatching Type2 COMMAND: 0x01=on / 0x00=off in message_data."""
    return compose_ids_can_extended_wire_frame(
        0x82, source, target, 0x01 if on else 0x00, b""
    )


class RelayConfirmError(RuntimeError):
    def __init__(self, device_id: int, on: bool) -> None:
        self.device_id = device_id
        self.on = on
        super().__init__(
            f"relay 0x{device_id:02X} did not confirm {'ON' if on else 'OFF'}"
        )


_CONFIRM_POLL_INTERVAL_S = 0.05


async def confirm_relay(link: GatewayLink, device_id: int, on: bool, timeout: float) -> bool:
    """Poll DEVICE_STATUS-derived relay state until the deadline.

    Reads the CAN-path state view (int-keyed by participant address); the
    data-path COBS family keeps its tuple-keyed ``relay_states`` separately.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = link.can_relay_states.get(device_id)
        if state is on:
            return True
        await asyncio.sleep(_CONFIRM_POLL_INTERVAL_S)
    return link.can_relay_states.get(device_id) is on


async def set_relay(
    link: GatewayLink,
    rc: RemoteControl,
    device_id: int,
    on: bool,
    confirm_timeout: float = 2.0,
) -> None:
    """Ensure a session, send the relay COMMAND, and confirm the state change.

    On a missed confirmation, one idempotent rewrite is sent (a latching relay
    command is safe to repeat) followed by one more confirmation window;
    a second miss raises :class:`RelayConfirmError`.
    """
    from rvcw.ble.session import RemoteControlSessionError

    if link.local_host_address is None:
        raise LinkError("LocalHost address not claimed")
    if not await rc.ensure(device_id):
        raise RemoteControlSessionError(f"session failed for device 0x{device_id:02X}")
    await link.write_can_frame(relay_command(link.local_host_address, device_id, on))
    if not await confirm_relay(link, device_id, on, timeout=confirm_timeout):
        # one rewrite (idempotent for a latching relay), then fail
        await link.write_can_frame(relay_command(link.local_host_address, device_id, on))
        if not await confirm_relay(link, device_id, on, timeout=confirm_timeout):
            raise RelayConfirmError(device_id, on)

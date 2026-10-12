# rvcw/discovery.py
"""Enumerate relay devices on the IDS-CAN bus; guided name learning."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rvcw.ble.session import RemoteControlSessionError
from rvcw.ble.transport import GatewayLink
from rvcw.protocol import (
    IdsCanWireFrame,
    compose_ids_can_extended_wire_frame,
    parse_ids_can_wire_frame,
)


@dataclass(frozen=True)
class DiscoveredDevice:
    address: int
    product_id: int
    device_type: int
    function_name: int
    function_instance: int


def parse_device_id_frame(frame_bytes: bytes) -> DiscoveredDevice | None:
    """Parse an 11-bit DEVICE_ID wire frame ([dlc][2][addr][8 payload bytes])."""
    if len(frame_bytes) < 3 or frame_bytes[1] != 0x02:
        return None
    wire = parse_ids_can_wire_frame(frame_bytes)
    if wire is None or wire.message_type != 0x02 or len(wire.payload) < 7:
        return None
    return DiscoveredDevice(
        address=wire.source_address,
        product_id=int.from_bytes(wire.payload[0:2], "big"),
        device_type=wire.payload[3],
        function_name=int.from_bytes(wire.payload[4:6], "big"),
        function_instance=wire.payload[6] & 0x0F,
    )


async def device_discovery_request(link: GatewayLink) -> None:
    """Broadcast a DEVICE_ID request (0x80/0x02 -> 0xFF) from the LocalHost."""
    source = link.local_host_address
    if source is None:
        raise RemoteControlSessionError("LocalHost address not claimed")
    await link.write_can_frame(
        compose_ids_can_extended_wire_frame(0x80, source, 0xFF, 0x02, b"")
    )


async def collect_devices(
    link: GatewayLink, window_s: float = 3.0
) -> list[DiscoveredDevice]:
    """Listen for DEVICE_ID frames for a window; dedupe by address."""
    found: dict[int, DiscoveredDevice] = {}
    previous = link.on_frame

    def hook(frame: IdsCanWireFrame) -> None:
        # DEVICE_ID frames arrive as decoded IdsCanWireFrame objects (the
        # notification path hands those over); wrap the fields back into raw
        # wire bytes so parse_device_id_frame sees one consistent input shape.
        if frame.message_type == 0x02:
            wire = frame
            rebuilt = bytes([len(wire.payload), 0x02, wire.source_address]) + wire.payload
            dev = parse_device_id_frame(rebuilt)
            if dev is not None:
                found[dev.address] = dev
        if previous is not None:
            previous(frame)

    link.on_frame = hook
    try:
        await device_discovery_request(link)
        await asyncio.sleep(window_s)
    finally:
        link.on_frame = previous
    return sorted(found.values(), key=lambda d: d.address)

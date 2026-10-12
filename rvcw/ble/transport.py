"""Bleak-based gateway link over the host's BlueZ (D-Bus multiplexed)."""
from __future__ import annotations

from typing import Callable

from bleak import BleakClient

from rvcw.protocol import (
    CAN_READ_CHAR_UUID,
    CAN_WRITE_CHAR_UUID,
    IdsCanWireFrame,
    parse_ids_can_wire_frame,
)


class LinkError(RuntimeError):
    pass


def decode_notification(raw: bytes) -> list[IdsCanWireFrame]:
    """Decode one CAN_READ notification into wire frames.

    V2 BLE notification types (byte 0): 1=Packed, 2=ElevenBit, 3=TwentyNineBit.
    Anything else is treated as a raw IDS wire frame.
    """
    raw = bytes(raw)
    if not raw or raw[0] not in (0x01, 0x02, 0x03):
        frame = parse_ids_can_wire_frame(raw)
        return [frame] if frame else []
    v2 = raw[0]
    if v2 == 0x01:  # Packed: one device advertisement -> 4 synthetic frames
        if len(raw) < 19:
            if len(raw) < 17:  # fixed header prefix up to device_caps
                return []
            # Short tail (truncated notification): pad the missing trailer
            # bytes, which are the status length/data, as absent/empty.
            raw = raw + b"\x00" * (19 - len(raw))
        (device_addr, network_status, ids_can_version) = raw[1], raw[2], raw[3]
        mac = bytes(raw[4:10])
        product_id = bytes(raw[10:12])
        product_instance, device_type = raw[12], raw[13]
        function_name = bytes(raw[14:16])
        instance_byte, device_caps = raw[16], raw[17]
        status_len = raw[18]
        status_data = bytes(raw[19 : 19 + status_len])
        candidates: list[bytes] = [
            bytes([8, 0x00, device_addr, network_status, ids_can_version]) + mac,
            bytes([8, 0x02, device_addr]) + product_id
            + bytes([product_instance, device_type])
            + function_name
            + bytes([instance_byte, device_caps]),
            bytes([status_len, 0x03, device_addr]) + status_data,
            bytes([4, 0x01, device_addr, 0, 0, 0, 0]),
        ]
        out = [f for f in map(parse_ids_can_wire_frame, candidates) if f]
        return out
    if v2 == 0x02:  # ElevenBit
        if len(raw) < 6:
            return []
        msg_type_byte, device_addr, dlc = raw[3], raw[4], raw[5]
        data = bytes(raw[6 : 6 + dlc])
        frame = parse_ids_can_wire_frame(bytes([dlc, msg_type_byte, device_addr]) + data)
        return [frame] if frame else []
    # v2 == 0x03: TwentyNineBit
    if len(raw) < 6:
        return []
    dlc, id4 = raw[5], bytes(raw[1:5])
    data = bytes(raw[6 : 6 + dlc])
    frame = parse_ids_can_wire_frame(bytes([dlc]) + id4 + data)
    return [frame] if frame else []


class GatewayLink:
    """One multiplexed participant on the gateway's GATT connection."""

    def __init__(self, mac: str, pin: str | None = None) -> None:
        self.mac = mac
        self.pin = pin
        self._client: BleakClient | None = None
        self.on_frame: Callable[[IdsCanWireFrame], None] | None = None
        self.relay_states: dict[int, bool] = {}
        self.local_host_address: int | None = None
        self.frames_received = 0

    @property
    def client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise LinkError("gateway link is not connected")
        return self._client

    async def connect(self) -> None:
        client = BleakClient(self.mac)
        await client.connect()
        try:
            if hasattr(client, "request_mtu"):
                await client.request_mtu(185)  # best-effort, as upstream does
        except Exception:
            pass
        self._client = client
        await client.start_notify(
            CAN_READ_CHAR_UUID,
            lambda _c, data: self._on_notification(bytes(data)),
        )

    def _on_notification(self, data: bytes) -> None:
        for frame in decode_notification(data):
            self.frames_received += 1
            if frame.message_type == 0x03:  # DEVICE_STATUS
                state = self.relay_state_from_status(frame.payload)
                if state is not None:
                    self.relay_states[frame.source_address] = state
            if self.on_frame is not None:
                self.on_frame(frame)

    @staticmethod
    def relay_state_from_status(payload: bytes) -> bool | None:
        """DEVICE_STATUS -> relay on/off. Derived from live data (Task 9); None until then."""
        return None

    async def write_can_frame(self, frame: bytes) -> None:
        """Write a raw IDS wire frame ([dlc][id][payload]) to CAN_WRITE."""
        if self._client is None:
            raise LinkError("gateway link is not connected")
        await self._client.write_gatt_char(CAN_WRITE_CHAR_UUID, frame, response=False)

    async def close(self) -> None:
        if self._client is not None:
            self.on_frame = None
            try:
                await self._client.disconnect()
            finally:
                self._client = None

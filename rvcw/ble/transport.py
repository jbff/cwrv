"""Bleak-based gateway link over the host's BlueZ (D-Bus multiplexed).

Two gateway families share this link class, detected from the GATT set at
connect time:

``"can"``
    IDS-CAN gateways (CAN_READ/CAN_WRITE). Raw wire frames both ways; the
    unlock/session machinery lives in :mod:`rvcw.ble.session` /
    :mod:`rvcw.ble.can`.

``"data"``
    Legacy MyRvLink COBS gateways (UNLOCK_STATUS/KEY/DATA_WRITE/DATA_READ).
    Step-1/step-2 TEA auth (:meth:`GatewayLink.authenticate_data`), a COBS
    event stream on DATA_READ, and a 5 s GetDevices keepalive — mirroring
    upstream ha_onecontrol coordinator flow.
"""
from __future__ import annotations

import asyncio
import logging
import time

from typing import Callable

from bleak import BleakClient

from rvcw import config
from rvcw.protocol import (
    CAN_READ_CHAR_UUID,
    CAN_WRITE_CHAR_UUID,
    CobsByteDecoder,
    DATA_READ_CHAR_UUID,
    DATA_WRITE_CHAR_UUID,
    IdsCanWireFrame,
    KEY_CHAR_UUID,
    parse_ids_can_wire_frame,
    SEED_CHAR_UUID,
    UNLOCK_STATUS_CHAR_UUID,
    calculate_step1_key,
    calculate_step2_key,
    cobs_encode,
)
from rvcw.protocol.commands import CommandBuilder
from rvcw.protocol.const import (
    EVENT_GATEWAY_INFORMATION,
    EVENT_RELAY_BASIC_LATCHING_1,
    EVENT_RELAY_BASIC_LATCHING_2,
    HEARTBEAT_INTERVAL,
    NOTIFICATION_ENABLE_DELAY,
    UNLOCK_VERIFY_DELAY,
)
from rvcw.protocol.events import (
    GatewayInformation,
    parse_gateway_information,
    parse_relay_status,
)

_log = logging.getLogger(__name__)

KEEPALIVE_TICK_S = HEARTBEAT_INTERVAL  # GetDevices keepalive (Android: 5000 ms)
WAIT_GATEWAY_INFO_POLL_S = 0.1


class LinkError(RuntimeError):
    pass


class GatewayAuthError(RuntimeError):
    """Data-path (step-1/step-2) authentication failure.

    This is the SAME exception type as ``rvcw.ble.session.GatewayUnlockError``
    (session.py aliases this class), so callers catching either name see both
    the CAN key/seed unlock and the COBS data-path auth errors.
    """


_DATA_REQUIRED_CHARS = (
    UNLOCK_STATUS_CHAR_UUID,
    KEY_CHAR_UUID,
    DATA_WRITE_CHAR_UUID,
    DATA_READ_CHAR_UUID,
)
_CAN_REQUIRED_CHARS = (CAN_READ_CHAR_UUID, CAN_WRITE_CHAR_UUID)


def _uuid_prefixes(uuids: tuple[str, ...]) -> str:
    return "/".join(u.split("-")[0] for u in uuids)


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
        self.on_frame: Callable[[IdsCanWireFrame | bytes], None] | None = None
        # Data path (COBS): relay state keyed (table_id, device_id).
        self.relay_states: dict[tuple[int, int], bool] = {}
        # CAN path: DEVICE_STATUS relay state keyed by participant address.
        # The two families never share a dict — key types differ.
        self.can_relay_states: dict[int, bool] = {}
        self.local_host_address: int | None = None
        self.frames_received = 0
        self.gateway_info: GatewayInformation | None = None
        self._decoder = CobsByteDecoder()
        self._command_builder = CommandBuilder()
        self._keepalive_task: asyncio.Task[None] | None = None

    @property
    def client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise LinkError("gateway link is not connected")
        return self._client

    @property
    def family(self) -> str:
        """Detected gateway family from the discovered GATT characteristics."""
        chars = {c.uuid for s in self.client.services for c in s.characteristics}
        if all(u in chars for u in _DATA_REQUIRED_CHARS):
            return "data"
        if CAN_WRITE_CHAR_UUID in chars:
            return "can"
        raise LinkError(
            "gateway exposes neither the data-path characteristics "
            f"({_uuid_prefixes(_DATA_REQUIRED_CHARS)}) nor CAN_WRITE"
        )

    async def connect(self) -> None:
        """Connect with service-discovery retries (upstream parity: the panel's
        GATT sometimes reports an incomplete service set right after connect —
        HA's boot log showed the same "missing CAN_WRITE" quirk).

        BlueZ caches the GATT enumeration per device across clients, so a
        partial enumeration persists until the device link is dropped — we
        disconnect at the BlueZ level (devices auto-reconnect for HA) to force
        a fresh enumeration when the required chars are missing.

        The required character set is per detected family: the four data-path
        chars make a ``"data"`` (legacy COBS) gateway, CAN_READ/CAN_WRITE a
        ``"can"`` one. The data family wins when a gateway advertises both.
        """
        client: BleakClient | None = None
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                client = BleakClient(self.mac)
                await client.connect()
            except Exception as exc:  # e.g. BlueZ InProgress during HA churn
                last_error = exc
                await asyncio.sleep(1.5 + attempt)
                continue
            try:
                if hasattr(client, "request_mtu"):
                    await client.request_mtu(185)  # best-effort, as upstream does
            except Exception:
                pass
            chars = {c.uuid for s in client.services for c in s.characteristics}
            if all(u in chars for u in _DATA_REQUIRED_CHARS):
                self._client = client
                if SEED_CHAR_UUID not in chars:
                    _log.warning(
                        "SEED char (0x0011) missing — step-2/PIN auth cannot run"
                    )
                return
            if all(u in chars for u in _CAN_REQUIRED_CHARS):
                self._client = client
                await client.start_notify(
                    CAN_READ_CHAR_UUID,
                    lambda _c, data: self._on_notification(bytes(data)),
                )
                return
            try:
                await client.disconnect()
            except Exception:
                pass
            # Drop the device link at BlueZ level: discards its cached GATT db
            # (all clients re-enumerate); HA's coordinator auto-reconnects.
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "disconnect", self.mac,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            await asyncio.sleep(1.0)
        raise LinkError(
            f"could not obtain the full GATT service set after retries "
            f"(data family wants {_uuid_prefixes(_DATA_REQUIRED_CHARS)}; "
            f"CAN family wants {_uuid_prefixes(_CAN_REQUIRED_CHARS)}): {last_error}"
        )

    # ---- CAN path ----------------------------------------------------------

    def _on_notification(self, data: bytes) -> None:
        for frame in decode_notification(data):
            self.frames_received += 1
            if frame.message_type == 0x03:  # DEVICE_STATUS
                state = self.relay_state_from_status(frame.payload)
                if state is not None:
                    self.can_relay_states[frame.source_address] = state
            if self.on_frame is not None:
                self.on_frame(frame)

    @staticmethod
    def relay_state_from_status(payload: bytes) -> bool | None:
        """DEVICE_STATUS -> relay on/off; None when the mapping is unrecognized."""
        # DERIVED FROM CAPTURE <date>: <describe the byte/bit mapping in one line here>
        if not payload:
            return None
        return None  # ← replace with the derived mapping, or keep None if unresolvable

    async def write_can_frame(self, frame: bytes) -> None:
        """Write a raw IDS wire frame ([dlc][id][payload]) to CAN_WRITE."""
        if self._client is None:
            raise LinkError("gateway link is not connected")
        await self._client.write_gatt_char(CAN_WRITE_CHAR_UUID, frame, response=False)

    # ---- data path (legacy COBS gateways) -----------------------------------

    async def authenticate_data(self, pin: str | None) -> None:
        """Step-1 Data Service auth, then the DATA_READ/SEED subscriptions.

        Upstream verbatim: read UNLOCK_STATUS (an already-"unlocked" answer
        short-circuits); otherwise the challenge must be 4 bytes, an all-zero
        challenge means the gateway is not ready, and
        ``calculate_step1_key(challenge)`` goes to KEY unwritten
        (``response=False``); after UNLOCK_VERIFY_DELAY the status re-read must
        say "unlocked". Then, a NOTIFICATION_ENABLE_DELAY later, DATA_READ is
        subscribed BEFORE SEED so the COBS decoder is live before a SEED can
        trigger step-2. PIN gateways finish authentication in
        :meth:`_authenticate_step2` on the first SEED notification.
        """
        self.pin = pin
        client = self.client

        challenge = bytes(await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID))
        if "unlocked" in challenge.decode("utf-8", errors="replace").lower():
            _log.info("step-1: gateway already unlocked")
        else:
            if len(challenge) != 4:
                raise GatewayAuthError(
                    f"UNLOCK_STATUS returned {len(challenge)} bytes, "
                    f"want a 4-byte challenge or 'unlocked'"
                )
            if challenge == b"\x00\x00\x00\x00":
                raise GatewayAuthError(
                    "gateway not ready — all-zeros UNLOCK_STATUS challenge; "
                    "retry once the panel is up"
                )
            key = calculate_step1_key(challenge)
            await client.write_gatt_char(KEY_CHAR_UUID, key, response=False)
            await asyncio.sleep(UNLOCK_VERIFY_DELAY)
            verify = bytes(await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID))
            if "unlocked" not in verify.decode("utf-8", errors="replace").lower():
                if pin is None:
                    raise GatewayAuthError(
                        "gateway still locked after step-1 and no pin configured — "
                    "put gateway.pin in "
                        f"{config.gateway_path().as_posix()}"
                    )
                raise GatewayAuthError(
                    f"gateway auth step-1 verify failed — verify={verify.hex()}"
                )
            _log.info("step-1: gateway unlocked")

        await asyncio.sleep(NOTIFICATION_ENABLE_DELAY)
        self._decoder = CobsByteDecoder()  # fresh state for a new session
        # Upstream order: DATA_READ first, then SEED.
        await client.start_notify(DATA_READ_CHAR_UUID, self._on_data_read)
        await client.start_notify(SEED_CHAR_UUID, self._on_seed_notification)

    async def _authenticate_step2(self, seed: bytes) -> None:
        """SEED notification handler: step-2 auth (16-byte PIN key to KEY)."""
        if len(seed) != 4:
            _log.warning(
                "step-2: unexpected SEED size %d — ignoring the notification",
                len(seed),
            )
            return
        if self.pin is None:
            _log.warning(
                "step-2: SEED received but no PIN is configured — the gateway "
                "stays locked; put gateway.pin in "
                f"{config.gateway_path().as_posix()}"
            )
            return
        key = calculate_step2_key(seed, self.pin)
        client = self.client
        await client.write_gatt_char(KEY_CHAR_UUID, key, response=False)
        _log.info("step-2: auth key written — authentication complete")

    def _on_data_read(
        self, _characteristic, data: bytearray  # noqa: ANN001 (bleak callback)
    ) -> None:
        """Feed raw DATA_READ bytes through the COBS decoder, byte by byte."""
        for byte in bytes(data):
            frame = self._decoder.decode_byte(byte)
            if frame is not None:
                self._on_data_frame(frame)

    async def _on_seed_notification(
        self, _characteristic, data: bytearray  # noqa: ANN001 (bleak callback)
    ) -> None:
        await self._authenticate_step2(bytes(data))

    def _on_data_frame(self, frame: bytes) -> None:
        """Dispatch one decoded COBS frame."""
        if not frame:
            return
        kind = frame[0]
        if kind == EVENT_GATEWAY_INFORMATION:
            info = parse_gateway_information(frame)
            if info is not None:
                self.gateway_info = info
            return
        if kind in (EVENT_RELAY_BASIC_LATCHING_1, EVENT_RELAY_BASIC_LATCHING_2):
            event = parse_relay_status(frame)
            # device_id 0x00 is the "no device" sentinel (upstream
            # is_valid_device_id semantics) — never store phantom slots.
            if event is not None and event.device_id:
                self.relay_states[(event.table_id, event.device_id)] = event.is_on
            return
        # Any other event is the CLI's job to parse — never crash mid-stream.
        if self.on_frame is not None:
            self.on_frame(frame)

    async def send_command(self, raw: bytes) -> None:
        """COBS-encode and write a raw command to DATA_WRITE (response=False)."""
        client = self.client
        await client.write_gatt_char(DATA_WRITE_CHAR_UUID, cobs_encode(raw), response=False)

    async def wait_gateway_info(self, timeout: float = 10.0) -> int:
        """Block until a GatewayInformation (0x01) event names the device table.

        Returns the table_id (the addressing half of every data-path command).
        """
        deadline = time.monotonic() + timeout
        while self.gateway_info is None:
            if time.monotonic() >= deadline:
                raise LinkError(
                    "no gateway table announced within "
                    f"{timeout:.0f} s — retry when the panel announces"
                )
            await asyncio.sleep(WAIT_GATEWAY_INFO_POLL_S)
        return self.gateway_info.table_id

    async def start_keepalive(self) -> None:
        """Start the 5 s GetDevices loop (a no-op if it is already running)."""
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _stop_keepalive(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _keepalive_loop(self) -> None:
        # Upstream GetDevices keepalive: one broadcast every 5 s once the
        # gateway announced its table; ticks before that are skipped (never
        # send table=0 blindly).
        while self._client is not None and self._client.is_connected:
            await asyncio.sleep(KEEPALIVE_TICK_S)
            if self._client is None or not self._client.is_connected:
                return  # link lost
            info = self.gateway_info
            if info is None:
                continue
            try:
                await self.send_command(
                    self._command_builder.build_get_devices(info.table_id)
                )
            except Exception as exc:
                _log.warning("GetDevices keepalive stopped (%s)", exc)
                return

    # ---- teardown -----------------------------------------------------------

    async def close(self) -> None:
        self._stop_keepalive()
        if self._client is not None:
            self.on_frame = None
            try:
                await self._client.disconnect()
            finally:
                self._client = None

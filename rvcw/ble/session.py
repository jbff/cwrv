# rvcw/ble/session.py
"""Gateway unlock and per-device REMOTE_CONTROL sessions."""
from __future__ import annotations

import asyncio

from rvcw import config
from rvcw.ble.transport import GatewayLink
from rvcw.protocol import (
    CAN_BLE_KEY_SEED_CIPHER,
    KEY_CHAR_UUID,
    PASSWORD_UNLOCK_CHAR_UUID,
    RC_CYPHER,
    UNLOCK_STATUS_CHAR_UUID,
    IdsCanWireFrame,
    calculate_can_ble_key_seed_key,
    compose_ids_can_extended_wire_frame,
    tea_encrypt,
)
from rvcw.protocol.ids_can_wire import ids_can_response_name


class GatewayUnlockError(RuntimeError):
    pass


async def unlock_gateway(link: GatewayLink) -> None:
    """Key/seed unlock via UNLOCK_STATUS/KEY, then PASSWORD_UNLOCK PIN if gated."""
    client = link.client

    unlock = await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID)
    if bytes(unlock).lower() == b"unlocked":
        return
    if len(unlock) < 4:
        raise GatewayUnlockError(
            f"UNLOCK_STATUS returned {len(unlock)} bytes, want a 4-byte seed or 'unlocked'"
        )
    seed = bytes(unlock[:4])
    key = calculate_can_ble_key_seed_key(seed, CAN_BLE_KEY_SEED_CIPHER)
    await client.write_gatt_char(KEY_CHAR_UUID, key, response=True)
    await asyncio.sleep(0.5)
    verify = bytes(await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID))
    if not (verify.lower() == b"unlocked" or verify == b"\x01"):
        raise GatewayUnlockError(f"key/seed unlock failed — verify={verify.hex()}")

    try:
        lock = bytes(await client.read_gatt_char(PASSWORD_UNLOCK_CHAR_UUID))
    except Exception:
        return  # char not accessible on this gateway — proceed (as upstream does)
    if lock and lock[0] != 0x00:
        return  # gate already open
    if not link.pin:
        raise GatewayUnlockError(
            "password gate locked and no pin configured — put "
            f"gateway.pin in {config.gateway_path().as_posix()}"
        )
    for attempt in (1, 2):
        await client.write_gatt_char(
            PASSWORD_UNLOCK_CHAR_UUID, link.pin.encode("utf-8"), response=False
        )
        await asyncio.sleep(1.0)
        verify = bytes(await client.read_gatt_char(PASSWORD_UNLOCK_CHAR_UUID))
        if verify and verify[0] != 0x00:
            return
    raise GatewayUnlockError(f"PIN unlock failed — verify={verify.hex() or 'empty'}")


_RC_SID_BYTES = bytes([0x00, 0x04])
_RC_SESSION_HEARTBEAT_S = 0.5
_SEED_TIMEOUT_S = 0.45
_KEY_TIMEOUT_S = 3.0


class RemoteControlSessionError(RuntimeError):
    pass


class RemoteControl:
    """Per-device REMOTE_CONTROL session (id 0x0004) with a 0.5 s heartbeat.

    Responses (message_type 0x81) are correlated through per-request futures
    resolved by ``handle_frame`` — which the caller must register on
    ``link.on_frame`` BEFORE any request is written.
    """

    def __init__(self, link: GatewayLink) -> None:
        self.link = link
        self.open = False
        self.target: int | None = None
        self._seed_fut: asyncio.Future[int] | None = None
        self._key_fut: asyncio.Future[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    # ---- frame dispatch (registered as link.on_frame) --------------------

    def handle_frame(self, frame: IdsCanWireFrame) -> None:
        if frame.message_type != 0x81:
            return
        msg = frame.message_data or 0
        if msg in (0x42, 0x43) and len(frame.payload) == 1:
            # Single-byte IDS status response: reject into the pending future.
            code = frame.payload[0]
            err = RemoteControlSessionError(
                f"session request 0x{msg:02X} rejected: 0x{code:02X} "
                f"({ids_can_response_name(code)})"
            )
            if msg == 0x42 and self._seed_fut and not self._seed_fut.done():
                self._seed_fut.set_exception(err)
            elif msg == 0x43 and self._key_fut and not self._key_fut.done():
                self._key_fut.set_exception(err)
            return
        if msg == 0x42 and len(frame.payload) >= 6 and self._seed_fut and not self._seed_fut.done():
            self._seed_fut.set_result(int.from_bytes(frame.payload[2:6], "big"))
        elif msg == 0x43 and self._key_fut and not self._key_fut.done():
            self._key_fut.set_result(None)
        elif msg == 0x45:  # device closed the session (heartbeat ACK 0x44 is NOT a close)
            self.open = False
            self.target = None

    async def ensure(self, device_id: int) -> bool:
        if self.open and self.target == device_id:
            return True
        source = self.link.local_host_address
        if source is None:
            raise RemoteControlSessionError("LocalHost address not claimed yet")

        # Preflight: session list/status queries (50 ms apart, small settle window)
        for req_code, payload in ((0x40, b"\x00\x00"), (0x40, b"\x00\x01"), (0x41, _RC_SID_BYTES)):
            await self.link.write_can_frame(
                compose_ids_can_extended_wire_frame(0x80, source, device_id, req_code, payload)
            )
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.20)

        # Seed request: REQUEST(0x80)/0x42 payload [sid2] -> RESPONSE/0x42 [sid2, seed4BE]
        loop = asyncio.get_running_loop()
        self._seed_fut = loop.create_future()
        await self.link.write_can_frame(
            compose_ids_can_extended_wire_frame(0x80, source, device_id, 0x42, _RC_SID_BYTES)
        )
        try:
            seed = await asyncio.wait_for(self._seed_fut, timeout=_SEED_TIMEOUT_S)
        except TimeoutError:
            raise RemoteControlSessionError(
                f"SESSION_REQUEST_SEED timed out for device 0x{device_id:02X} "
                "(V1 gateway requires an explicit exchange — check source/target)"
            ) from None

        # Key transmit: REQUEST/0x43 payload [sid2, key4BE] -> RESPONSE/0x43 [sid2]
        key = tea_encrypt(RC_CYPHER, seed)
        self._key_fut = loop.create_future()
        await self.link.write_can_frame(
            compose_ids_can_extended_wire_frame(
                0x80, source, device_id, 0x43, _RC_SID_BYTES + key.to_bytes(4, "big")
            )
        )
        try:
            await asyncio.wait_for(self._key_fut, timeout=_KEY_TIMEOUT_S)
        except TimeoutError:
            raise RemoteControlSessionError(
                f"SESSION_TRANSMIT_KEY timed out for device 0x{device_id:02X}"
            ) from None

        self.open = True
        self.target = device_id
        self._heartbeat_task = asyncio.create_task(self._heartbeat(device_id))
        return True

    async def _heartbeat(self, device_id: int) -> None:
        source = self.link.local_host_address
        while self.open and self.target == device_id:
            await asyncio.sleep(_RC_SESSION_HEARTBEAT_S)
            if not (self.open and self.target == device_id):
                break
            try:
                await self.link.write_can_frame(
                    compose_ids_can_extended_wire_frame(
                        0x80, source, device_id, 0x44, _RC_SID_BYTES
                    )
                )
            except Exception:
                return  # link down; the gateway drops the session with it

    async def end(self, device_id: int | None = None) -> None:
        """Explicitly close the session (REQUEST/0x45 payload [sid2])."""
        target = device_id if device_id is not None else self.target
        await self._cancel_heartbeat()
        self.open = False
        self.target = None
        if target is None:
            return
        source = self.link.local_host_address
        if source is None:
            return
        try:
            await self.link.write_can_frame(
                compose_ids_can_extended_wire_frame(0x80, source, target, 0x45, _RC_SID_BYTES)
            )
        except Exception:
            pass  # link down; the gateway drops the session with the connection

    async def _cancel_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None

    async def aclose(self) -> None:
        """Cancel the heartbeat WITHOUT SESSION_END (the 1.0.48 relay gotcha).

        Also resets session state: the gateway drops the session when the BLE
        link closes (which is what follows ``aclose`` in every caller), so
        leaving ``open``/``target`` set would expose a stale keepalive-less
        session that ``ensure`` would early-return on. A later ``ensure``
        performs a full seed/key rebuild.
        """
        await self._cancel_heartbeat()
        self.open = False
        self.target = None

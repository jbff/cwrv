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
    UNLOCK_STATUS_CHAR_UUID,
    calculate_can_ble_key_seed_key,
)


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

# tests/test_unlock.py
import asyncio

import pytest

from rvcw.ble.session import GatewayUnlockError, unlock_gateway
from rvcw.protocol import calculate_can_ble_key_seed_key, CAN_BLE_KEY_SEED_CIPHER


class FakeChar:
    def __init__(self, value: bytes):
        self.value = bytearray(value)


class FakeClient:
    """Records writes; serves canned reads. Chars keyed by full UUID prefix."""

    def __init__(self) -> None:
        self.chars: dict[str, FakeChar] = {}
        self.is_connected = True
        self.written: list[tuple[str, bytes, bool]] = []

    def add(self, prefix: str, value: bytes) -> None:
        self.chars[f"{prefix}-0200-a58e-e411-afe28044e62c"] = FakeChar(value)

    async def read_gatt_char(self, uuid: str) -> bytearray:
        return self.chars[uuid].value

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        self.written.append((uuid, bytes(data), response))
        if uuid.startswith("00000013"):  # KEY write unlocks UNLOCK_STATUS
            self.chars["00000012-0200-a58e-e411-afe28044e62c"].value = bytearray(b"unlocked")
        if uuid.startswith("00000005"):  # PIN write unlocks the gate
            self.chars["00000005-0200-a58e-e411-afe28044e62c"].value = bytearray(b"\x01")


class FakeLink:
    def __init__(self, client: FakeClient, pin: str | None) -> None:
        self._client = client
        self.pin = pin

    @property
    def client(self):
        return self._client


@pytest.fixture()
def locked_gateway():
    client = FakeClient()
    seed = bytes.fromhex("1A2B3C4D")
    client.add("00000012", seed)          # UNLOCK_STATUS
    client.add("00000005", b"\x00")       # PASSWORD_UNLOCK (locked)
    client.written = []
    return client, seed


async def test_unlock_happy_path(locked_gateway):
    client, seed = locked_gateway
    expected_key = calculate_can_ble_key_seed_key(seed, CAN_BLE_KEY_SEED_CIPHER)
    await unlock_gateway(FakeLink(client, pin="123456"))
    key_writes = [w for w in client.written if w[0].startswith("00000013")]
    pin_writes = [w for w in client.written if w[0].startswith("00000005")]
    assert key_writes and key_writes[0][1] == expected_key and key_writes[0][2] is True
    assert pin_writes and pin_writes[0][1] == b"123456"


async def test_already_unlocked_is_idempotent(locked_gateway):
    client, _ = locked_gateway
    client.chars["00000012-0200-a58e-e411-afe28044e62c"].value = bytearray(b"unlocked")
    await unlock_gateway(FakeLink(client, pin=None))
    assert not [w for w in client.written if w[0].startswith("00000013")]


async def test_locked_gate_without_pin_raises(locked_gateway):
    client, _ = locked_gateway
    with pytest.raises(GatewayUnlockError, match="no pin configured"):
        await unlock_gateway(FakeLink(client, pin=None))


async def test_bad_key_verify_raises(locked_gateway):
    client, _ = locked_gateway

    # Sabotage: KEY writes do not unlock UNLOCK_STATUS
    async def noop(uuid, data, response=False):
        client.written.append((uuid, bytes(data), response))

    client.write_gatt_char = noop
    with pytest.raises(GatewayUnlockError):
        await unlock_gateway(FakeLink(client, pin="654321"))

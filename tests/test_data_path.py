# tests/test_data_path.py
"""Task 13: data-path transport (step-1/step-2 TEA auth, COBS events, keepalive)."""
import asyncio

import pytest

from rvcw import config
from rvcw.ble.session import GatewayUnlockError
from rvcw.ble.transport import GatewayAuthError, GatewayLink, LinkError
from rvcw.protocol import (
    CAN_READ_CHAR_UUID,
    CAN_WRITE_CHAR_UUID,
    CobsByteDecoder,
    DATA_READ_CHAR_UUID,
    DATA_WRITE_CHAR_UUID,
    KEY_CHAR_UUID,
    SEED_CHAR_UUID,
    UNLOCK_STATUS_CHAR_UUID,
    calculate_step1_key,
    calculate_step2_key,
    cobs_encode,
)
from rvcw.protocol.commands import CommandBuilder
from rvcw.protocol.const import NOTIFICATION_ENABLE_DELAY, UNLOCK_VERIFY_DELAY
from rvcw.protocol.events import GatewayInformation

BASE = "-0200-a58e-e411-afe28044e62c"


def uuid(prefix: str) -> str:
    return f"{prefix}{BASE}"


class _FakeChar:
    def __init__(self, uuid_str: str) -> None:
        self.uuid = uuid_str


class _FakeService:
    def __init__(self, uuids: list[str]) -> None:
        self.characteristics = [_FakeChar(u) for u in uuids]


class FakeBleakClient:
    """BleakClient double: discovered chars, canned reads, recorded writes/notify hooks."""

    def __init__(self, char_uuids: list[str], *, key_write_unlocks: bool = True) -> None:
        self._char_uuids = frozenset(char_uuids)
        self._values: dict[str, bytes] = {}
        self.is_connected = True
        self.written: list[tuple[str, bytes, bool]] = []
        self.notifies: dict[str, object] = {}
        self.key_write_unlocks = key_write_unlocks

    def set_value(self, uuid_str: str, value: bytes) -> None:
        self._values[uuid_str] = bytes(value)

    @property
    def services(self):
        return [_FakeService(sorted(self._char_uuids))]

    async def connect(self) -> None:
        self.is_connected = True

    async def request_mtu(self, n: int) -> None:
        pass

    async def disconnect(self) -> None:
        self.is_connected = False

    async def read_gatt_char(self, uuid_str: str) -> bytearray:
        return bytearray(self._values.get(uuid_str, b""))

    async def write_gatt_char(self, uuid_str: str, data: bytes, response: bool = False) -> None:
        self.written.append((uuid_str, bytes(data), response))
        if uuid_str == KEY_CHAR_UUID and self.key_write_unlocks:
            self._values[UNLOCK_STATUS_CHAR_UUID] = b"unlocked"

    async def start_notify(self, uuid_str: str, callback) -> None:
        self.notifies[uuid_str] = callback

    async def stop_notify(self, uuid_str: str) -> None:
        self.notifies.pop(uuid_str, None)

    async def notify(self, uuid_str: str, data: bytes) -> None:
        result = self.notifies[uuid_str](self, bytearray(data))
        if asyncio.iscoroutine(result):
            await result


def make_client(family: str = "data", **kw) -> FakeBleakClient:
    if family == "data":
        chars = [UNLOCK_STATUS_CHAR_UUID, KEY_CHAR_UUID, DATA_WRITE_CHAR_UUID, DATA_READ_CHAR_UUID]
    elif family == "can":
        chars = [CAN_READ_CHAR_UUID, CAN_WRITE_CHAR_UUID]
    else:
        chars = []
    return FakeBleakClient(chars, **kw)


def make_link(client: FakeBleakClient | None = None) -> GatewayLink:
    link = GatewayLink("10:06:1C:E7:B8:3E")
    if client is not None:
        link._client = client
    return link


@pytest.fixture()
def sleep_log(monkeypatch):
    """Replace asyncio.sleep inside the transport module with a recorder.

    Also keeps the auth/keepalive tests instant — the real delays (0.5 s,
    0.2 s, 5 s keepalive) are asserted via the log instead of awaited.
    """
    log: list[float] = []

    async def _sleep(delay, *_args, **_kwargs) -> None:
        log.append(delay)

    monkeypatch.setattr("rvcw.ble.transport.asyncio.sleep", _sleep)
    return log


# ---- auth error identity --------------------------------------------------


def test_gateway_auth_error_is_gateway_unlock_error():
    assert GatewayAuthError is GatewayUnlockError


# ---- step-1 ---------------------------------------------------------------


async def test_step1_happy_path_writes_vendored_key_with_no_response(sleep_log):
    client = make_client()
    challenge = bytes.fromhex("1A2B3C4D")
    client.set_value(UNLOCK_STATUS_CHAR_UUID, challenge)
    link = make_link(client)
    await link.authenticate_data("123456")
    # the ONLY write is the vendored step-1 key, unwritten (response=False)
    assert client.written == [(KEY_CHAR_UUID, calculate_step1_key(challenge), False)]
    # verify pause, then notification-enable pause, in that order
    assert sleep_log == [UNLOCK_VERIFY_DELAY, NOTIFICATION_ENABLE_DELAY]
    # DATA_READ subscribed BEFORE SEED (upstream order); SEED present
    assert list(client.notifies) == [DATA_READ_CHAR_UUID, SEED_CHAR_UUID]
    assert link.pin == "123456"


async def test_step1_already_unlocked_short_circuit_still_subscribes(sleep_log):
    client = make_client()
    client.set_value(UNLOCK_STATUS_CHAR_UUID, b"unlocked")
    link = make_link(client)
    await link.authenticate_data(None)
    assert not [w for w in client.written if w[0] == KEY_CHAR_UUID]
    # no verify pause on the short-circuit — straight to notifications
    assert sleep_log == [NOTIFICATION_ENABLE_DELAY]
    assert list(client.notifies) == [DATA_READ_CHAR_UUID, SEED_CHAR_UUID]


async def test_step1_all_zero_challenge_is_gateway_not_ready(sleep_log):
    client = make_client()
    client.set_value(UNLOCK_STATUS_CHAR_UUID, b"\x00\x00\x00\x00")
    link = make_link(client)
    with pytest.raises(GatewayAuthError, match="gateway not ready"):
        await link.authenticate_data("000000")
    assert client.written == []
    assert client.notifies == {}


async def test_step1_rejects_non_4_byte_challenge():
    client = make_client()
    client.set_value(UNLOCK_STATUS_CHAR_UUID, b"locked!")
    link = make_link(client)
    with pytest.raises(GatewayAuthError, match="4-byte"):
        await link.authenticate_data(None)


async def test_step1_verify_failure_without_pin_names_config_path(sleep_log):
    client = make_client(key_write_unlocks=False)
    client.set_value(UNLOCK_STATUS_CHAR_UUID, bytes.fromhex("1A2B3C4D"))
    link = make_link(client)
    with pytest.raises(GatewayAuthError, match="no pin") as excinfo:
        await link.authenticate_data(None)
    # the error names the config file (the session.py GatewayUnlockError pattern)
    assert config.gateway_path().as_posix() in str(excinfo.value)


async def test_step1_verify_failure_with_pin_reports_verify_bytes(sleep_log):
    client = make_client(key_write_unlocks=False)
    client.set_value(UNLOCK_STATUS_CHAR_UUID, bytes.fromhex("1A2B3C4D"))
    link = make_link(client)
    with pytest.raises(GatewayAuthError, match="verify="):
        await link.authenticate_data("654321")


# ---- step-2 (SEED notifications) ------------------------------------------


async def test_seed_notification_writes_16_byte_step2_key(sleep_log):
    client = make_client()
    client.set_value(UNLOCK_STATUS_CHAR_UUID, b"unlocked")
    link = make_link(client)
    await link.authenticate_data("654321")
    seed = bytes.fromhex("0BADF00D")
    await client.notify(SEED_CHAR_UUID, seed)
    assert client.written == [(KEY_CHAR_UUID, calculate_step2_key(seed, "654321"), False)]
    assert len(client.written[0][1]) == 16


async def test_step2_ignores_wrong_size_seed(sleep_log):
    client = make_client()
    client.set_value(UNLOCK_STATUS_CHAR_UUID, b"unlocked")
    link = make_link(client)
    await link.authenticate_data("654321")
    await client.notify(SEED_CHAR_UUID, b"\x01\x02\x03")  # 3-byte seed must be skipped
    assert client.written == []


# ---- data frames ----------------------------------------------------------


def test_on_data_frame_stores_tuple_keyed_relay_state():
    link = make_link()
    link._on_data_frame(bytes([0x05, 0x01, 0x2E, 0x01, 0x00]))
    assert link.relay_states == {(0x01, 0x2E): True}


def test_on_data_frame_not_on_for_off_status_byte():
    link = make_link()
    link._on_data_frame(bytes([0x05, 0x01, 0x2E, 0x00, 0x00]))
    assert link.relay_states == {(0x01, 0x2E): False}


def test_on_data_frame_skips_invalid_zero_device_id():
    # device_id 0x00 is the "no device" sentinel (upstream is_valid_device_id)
    link = make_link()
    link._on_data_frame(bytes([0x05, 0x01, 0x00, 0x01, 0x00]))
    assert link.relay_states == {}


def test_on_data_frame_handles_both_latching_event_types():
    link = make_link()
    link._on_data_frame(bytes([0x06, 0x01, 0x2E, 0x00, 0x00]))
    assert link.relay_states == {(0x01, 0x2E): False}


def test_on_data_frame_status_high_nibble_does_not_mask_the_low_nibble():
    # 0x11 = ON with a high-nibble flag: the vendored parse_relay_status rule
    # is is_on = (status & 0x0F) == 0x01 (only the LOW nibble carries the
    # state; faults ride in the separate dtc_code field of extended frames).
    link = make_link()
    link._on_data_frame(bytes([0x05, 0x01, 0x2E, 0x11, 0x00]))
    assert link.relay_states == {(0x01, 0x2E): True}


def test_on_data_frame_gateway_information_updates_table_id():
    link = make_link()
    frame = bytes([0x01, 0x02, 0x00, 0x05, 0x02]) + bytes(8)  # table_id=2 at data[4]
    link._on_data_frame(frame)
    assert link.gateway_info is not None
    assert link.gateway_info.table_id == 2


def test_other_data_frames_pass_to_on_frame():
    link = make_link()
    seen: list[bytes] = []
    link.on_frame = seen.append
    payload = bytes([0x07, 0x00, 0x0B])  # RV_STATUS etc. — the CLI parses these
    link._on_data_frame(payload)
    assert seen == [payload]


def test_data_read_stream_decodes_frames_byte_by_byte():
    client = make_client()
    link = make_link(client)
    wire = cobs_encode(bytes([0x05, 0x01, 0x2E, 0x01, 0x00]))
    mid = len(wire) // 2
    # feed the notification in two pieces — the decoder is stateful per byte
    link._on_data_read(None, bytearray(wire[:mid]))
    link._on_data_read(None, bytearray(wire[mid:]))
    assert link.relay_states == {(0x01, 0x2E): True}


# ---- commands / gateway info ----------------------------------------------


async def test_send_command_cobs_wraps_onto_data_write():
    client = make_client()
    link = make_link(client)
    raw = CommandBuilder().build_get_devices(2)
    await link.send_command(raw)
    assert client.written == [(DATA_WRITE_CHAR_UUID, cobs_encode(raw), False)]


async def test_send_command_unconnected_raises():
    link = make_link(None)
    with pytest.raises(LinkError):
        await link.send_command(b"\x01\x02")


async def test_wait_gateway_info_returns_table_id():
    link = make_link()
    link.gateway_info = GatewayInformation(table_id=7)
    assert await link.wait_gateway_info(timeout=0.1) == 7


async def test_wait_gateway_info_timeout_raises_link_error():
    link = make_link()
    with pytest.raises(LinkError, match="no gateway table"):
        await link.wait_gateway_info(timeout=0.1)


# ---- GetDevices keepalive (5 s loop) ---------------------------------------


def _decode_writes(client: FakeBleakClient) -> list[bytes]:
    decoder = CobsByteDecoder()
    frames: list[bytes] = []
    for chunk in client.written:
        for b in chunk[1]:
            frame = decoder.decode_byte(b)
            if frame is not None:
                frames.append(frame)
    return frames


async def test_keepalive_sends_get_devices_for_the_known_table(monkeypatch):
    client = make_client()
    link = make_link(client)
    link.gateway_info = GatewayInformation(table_id=2)
    monkeypatch.setattr("rvcw.ble.transport.KEEPALIVE_TICK_S", 0.05)
    await link.start_keepalive()
    try:
        await asyncio.sleep(0.16)
        frames = _decode_writes(client)
        assert len(frames) >= 2  # ticks twice within ~3 intervals
        for frame in frames:
            assert frame[2] == 0x01  # CMD_GET_DEVICES
            assert frame[3] == 0x02  # table_id
        assert all(w[2] is False for w in client.written)
    finally:
        await link.close()


async def test_keepalive_skips_ticks_before_gateway_info(monkeypatch):
    client = make_client()
    link = make_link(client)
    monkeypatch.setattr("rvcw.ble.transport.KEEPALIVE_TICK_S", 0.05)
    await link.start_keepalive()
    try:
        await asyncio.sleep(0.12)
        assert client.written == []
    finally:
        await link.close()


async def test_keepalive_stops_when_link_lost(monkeypatch):
    client = make_client()
    link = make_link(client)
    link.gateway_info = GatewayInformation(table_id=2)
    monkeypatch.setattr("rvcw.ble.transport.KEEPALIVE_TICK_S", 0.05)
    await link.start_keepalive()
    await asyncio.sleep(0.07)  # at least one tick
    assert client.written, "sanity: keepalive fired before link loss"
    client.is_connected = False
    await asyncio.sleep(0.12)
    task = link._keepalive_task
    assert task is not None and task.done()
    await link.close()


# ---- family detection / family-aware connect -------------------------------


def _client_factory(chars: list[str]):
    def factory(_mac: str) -> FakeBleakClient:
        return FakeBleakClient(chars)

    return factory


async def test_connect_data_family(monkeypatch):
    monkeypatch.setattr("rvcw.ble.transport.BleakClient", _client_factory(make_client()._char_uuids))
    link = make_link(None)
    await link.connect()
    assert link.family == "data"
    assert link._client is not None
    # data-family connect installs NO CAN subscription (auth does the data ones)
    assert link._client.notifies == {}


async def test_connect_data_family_succeeds_without_optional_seed_char(monkeypatch):
    chars = [UNLOCK_STATUS_CHAR_UUID, KEY_CHAR_UUID, DATA_WRITE_CHAR_UUID, DATA_READ_CHAR_UUID]
    monkeypatch.setattr("rvcw.ble.transport.BleakClient", _client_factory(chars))
    link = make_link(None)
    await link.connect()
    assert link.family == "data"


async def test_connect_can_family_keeps_can_read_subscription(monkeypatch):
    monkeypatch.setattr("rvcw.ble.transport.BleakClient", _client_factory(make_client("can")._char_uuids))
    link = make_link(None)
    await link.connect()
    assert link.family == "can"
    assert list(link._client.notifies) == [CAN_READ_CHAR_UUID]


async def test_connect_family_prefers_data_when_both_charsets_present(monkeypatch):
    chars = sorted(
            set(make_client()._char_uuids) | {CAN_READ_CHAR_UUID, CAN_WRITE_CHAR_UUID}
    )
    monkeypatch.setattr("rvcw.ble.transport.BleakClient", _client_factory(chars))
    link = make_link(None)
    await link.connect()
    assert link.family == "data"
    assert link._client.notifies == {}


async def test_connect_retries_with_bluetoothctl_recovery_then_raises(monkeypatch, sleep_log):
    monkeypatch.setattr("rvcw.ble.transport.BleakClient", _client_factory([]))
    called: list[tuple] = []

    class FakeProc:
        async def wait(self) -> int:
            return 0

    async def fake_subprocess(*args, **kwargs):
        called.append(args)
        return FakeProc()

    monkeypatch.setattr("rvcw.ble.transport.asyncio.create_subprocess_exec", fake_subprocess)
    link = make_link(None)
    with pytest.raises(LinkError):
        await link.connect()
    assert len(called) == 4  # bluetoothctl disconnect before every retry


def test_family_unconnected_raises():
    link = make_link(None)
    with pytest.raises(LinkError):
        link.family

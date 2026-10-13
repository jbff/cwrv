import asyncio

import pytest

from rvcw.ble.can import RelayConfirmError, confirm_relay, relay_command, set_relay
from rvcw.ble.session import RemoteControlSessionError
from rvcw.ble.transport import LinkError
from rvcw.protocol import parse_ids_can_wire_frame


def test_relay_command_frame():
    frame = relay_command(0x2D, 0x2E, True)
    wire = parse_ids_can_wire_frame(frame)
    assert wire.message_type == 0x82
    assert wire.source_address == 0x2D
    assert wire.target_address == 0x2E
    assert wire.message_data == 0x01
    assert wire.payload == b""
    off = parse_ids_can_wire_frame(relay_command(0x2D, 0x2E, False))
    assert off.message_data == 0x00


class FakeLink:
    """Minimal GatewayLink stand-in: local host + relay state + write recording."""

    def __init__(self, local_host: int | None = 0x2D):
        self.local_host_address = local_host
        self.relay_states: dict[int, bool] = {}
        self.written: list[bytes] = []
        self.on_frame = None

    async def write_can_frame(self, frame: bytes) -> None:
        self.written.append(frame)


class FakeRC:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.ensure_calls: list[int] = []

    async def ensure(self, device_id: int) -> bool:
        self.ensure_calls.append(device_id)
        if not self.ok:
            raise RemoteControlSessionError(f"no session for 0x{device_id:02X}")
        return True


@pytest.mark.parametrize(
    ("state", "on", "expected"),
    [(True, True, True), (False, True, False), (True, False, False), (False, False, True)],
)
async def test_confirm_relay_identity_comparison(state, on, expected):
    link = FakeLink()
    link.relay_states[0x2E] = state
    assert await confirm_relay(link, 0x2E, on, timeout=0.0) is expected


async def test_confirm_relay_true_when_state_appears_while_polling():
    link = FakeLink()

    async def appear() -> None:
        await asyncio.sleep(0.08)
        link.relay_states[0x2E] = True

    task = asyncio.create_task(appear())
    assert await confirm_relay(link, 0x2E, True, timeout=0.5) is True
    await task


async def test_confirm_relay_false_when_never_confirmed():
    link = FakeLink()
    assert await confirm_relay(link, 0x2E, True, timeout=0.12) is False


async def test_confirm_relay_ignores_other_devices():
    link = FakeLink()
    link.relay_states[0x41] = True
    assert await confirm_relay(link, 0x2E, True, timeout=0.12) is False


async def test_set_relay_ensure_fails_before_any_write():
    link = FakeLink()
    with pytest.raises(RemoteControlSessionError):
        await set_relay(link, FakeRC(ok=False), 0x2E, True)
    assert link.written == []


async def test_set_relay_requires_local_host():
    link = FakeLink(local_host=None)
    with pytest.raises(LinkError):
        await set_relay(link, FakeRC(), 0x2E, True)


async def test_set_relay_writes_command_and_confirms():
    link = FakeLink()
    rc = FakeRC()
    # the device confirms the relay on the command write itself
    link.write_can_frame = _confirming_write(link, {0x2E: True})
    await set_relay(link, rc, 0x2E, True, confirm_timeout=0.5)
    assert rc.ensure_calls == [0x2E]
    assert len(link.written) == 1
    wire = parse_ids_can_wire_frame(link.written[0])
    assert (wire.message_type, wire.source_address, wire.target_address, wire.message_data) == (
        0x82,
        0x2D,
        0x2E,
        0x01,
    )


async def test_set_relay_retries_once_then_raises():
    link = FakeLink()
    with pytest.raises(RelayConfirmError) as excinfo:
        await set_relay(link, FakeRC(), 0x2E, True, confirm_timeout=0.12)
    assert excinfo.value.device_id == 0x2E
    assert excinfo.value.on is True
    # one idempotent rewrite on the miss, then fail (not endless retries)
    assert len(link.written) == 2
    assert all(parse_ids_can_wire_frame(f).message_data == 0x01 for f in link.written)


async def test_set_relay_off_command_confirms_false():
    link = FakeLink()
    link.write_can_frame = _confirming_write(link, {0x2E: False})
    await set_relay(link, FakeRC(), 0x2E, False, confirm_timeout=0.5)
    wire = parse_ids_can_wire_frame(link.written[0])
    assert wire.message_data == 0x00


async def test_set_relay_succeeds_on_rewrite():
    link = FakeLink()

    async def write(frame: bytes) -> None:
        link.written.append(frame)
        if len(link.written) >= 2:  # only the rewrite reaches the device
            link.relay_states[0x2E] = True

    link.write_can_frame = write
    await set_relay(link, FakeRC(), 0x2E, True, confirm_timeout=0.5)
    assert len(link.written) == 2


def _confirming_write(link: FakeLink, states: dict[int, bool]):
    async def write(frame: bytes) -> None:
        link.written.append(frame)
        wire = parse_ids_can_wire_frame(frame)
        if wire is not None and wire.message_type == 0x82:
            link.relay_states.update(states)

    return write
# tests/test_controller.py
"""Task 14: RelayController protocol + per-family implementations."""
import asyncio

import pytest

from rvcw.ble.controller import (
    CanRelayLink,
    CobsRelayLink,
    link_family,
)
from rvcw.ble.transport import LinkError
from rvcw.lights import LightTarget
from rvcw.protocol import parse_ids_can_wire_frame
from rvcw.protocol.commands import CommandBuilder

T2 = LightTarget(0x02, 0x2E)


class FakeDataLink:
    """GatewayLink stand-in for the COBS data path."""

    def __init__(self) -> None:
        self.relay_states: dict[tuple[int, int], bool] = {}
        self.commands: list[bytes] = []
        self.closed = False

    async def send_command(self, raw: bytes) -> None:
        self.commands.append(raw)
        self._echo(raw)  # a write lands => the relay state follows (upstream echo)

    def _echo(self, raw: bytes) -> None:
        table, state = raw[3], raw[4] == 0x01
        for d in raw[5:]:
            self.relay_states[(table, d)] = state

    async def close(self) -> None:
        self.closed = True


class FakeRC:
    def __init__(self) -> None:
        self.ensured: list[int] = []
        self.closes = 0

    def handle_frame(self, frame) -> None:  # re-registered after a reconnect
        pass

    async def ensure(self, device_id: int) -> bool:
        self.ensured.append(device_id)
        return True

    async def aclose(self) -> None:
        self.closes += 1


class FakeCanLink:
    def __init__(self) -> None:
        self.local_host_address = 0x2D
        self.can_relay_states: dict[int, bool] = {}
        self.written: list[bytes] = []
        self.on_frame = None

    async def write_can_frame(self, frame: bytes) -> None:
        self.written.append(frame)


def test_controllers_expose_the_relay_controller_protocol():
    from rvcw.ble.controller import RelayController
    from rvcw.lights import LightEntry  # noqa: F401  (imports resolve)

    for obj in (CobsRelayLink(FakeDataLink()), CanRelayLink(FakeCanLink(), FakeRC())):
        assert isinstance(obj, RelayController)


def test_isinstance_check_uses_runtime_protocol():
    # Protocol shape pinned explicitly (structural typing without mypy):
    for obj in (CobsRelayLink(FakeDataLink()), CanRelayLink(FakeCanLink(), FakeRC())):
        assert hasattr(obj, "ensure") and hasattr(obj, "write")
        assert hasattr(obj, "confirm") and hasattr(obj, "aclose")
        assert hasattr(obj, "current_state")


# ---- CobsRelayLink (data family) -----------------------------------------


async def test_cobs_write_sends_one_action_switch_per_target():
    link = FakeDataLink()
    controller = CobsRelayLink(link)
    await controller.write(LightTarget(0x02, 0x2E), True)
    await controller.write(LightTarget(0x02, 0x2F), False)
    assert len(link.commands) == 2
    for raw, (tbl, dev, on) in zip(
        link.commands, [(0x02, 0x2E, True), (0x02, 0x2F, False)]
    ):
        # [cid2][0x40 ActionSwitch][table][state][device...]
        assert (raw[2], raw[3], raw[4]) == (CommandBuilder.CMD_ACTION_SWITCH, tbl, 0x01 if on else 0x00)
        assert list(raw[5:]) == [dev]  # exactly ONE device id per write


async def test_cobs_write_uses_one_shared_builder_instance():
    link = FakeDataLink()
    controller = CobsRelayLink(link)
    await controller.write(T2, True)
    await controller.write(T2, False)
    # command ids come from ONE shared builder: monotonic 0 then 1
    assert (link.commands[0][0], link.commands[0][1]) == (0x00, 0x00)
    assert (link.commands[1][0], link.commands[1][1]) == (0x01, 0x00)


async def test_cobs_ensure_reports_ready_and_aclose_is_passive():
    link = FakeDataLink()
    controller = CobsRelayLink(link)
    assert await controller.ensure(T2) is True  # auth is the link's job
    await controller.aclose()
    assert link.closed is False  # teardown stays with the link owner


async def test_cobs_confirm_polls_until_state_appears():
    link = FakeDataLink()
    controller = CobsRelayLink(link)

    async def appear() -> None:
        await asyncio.sleep(0.08)
        link.relay_states[(0x02, 0x2E)] = True

    task = asyncio.create_task(appear())
    assert await controller.confirm(T2, True, timeout=0.5) is True
    await task


async def test_cobs_confirm_uses_is_on_identity_not_truthiness():
    link = FakeDataLink()
    link.relay_states[(0x02, 0x2E)] = True
    controller = CobsRelayLink(link)
    assert await controller.confirm(T2, False, timeout=0.0) is False


async def test_cobs_confirm_ignores_unrelated_keys():
    link = FakeDataLink()
    link.relay_states[(0x02, 0x2F)] = True
    controller = CobsRelayLink(link)
    assert await controller.confirm(T2, True, timeout=0.12) is False


async def test_cobs_current_state_reads_the_tuple_keyed_view():
    link = FakeDataLink()
    controller = CobsRelayLink(link)
    assert await controller.current_state(T2) is None  # pre-state unknown
    link.relay_states[(0x02, 0x2E)] = True
    assert await controller.current_state(T2) is True


# ---- CanRelayLink (CAN family: adapt, don't delete) ------------------------


async def test_can_ensure_delegates_to_remote_control():
    link = FakeCanLink()
    rc = FakeRC()
    controller = CanRelayLink(link, rc)
    assert await controller.ensure(LightTarget(0x00, 0x2E)) is True
    assert rc.ensured == [0x2E]


async def test_can_write_sends_a_type2_relay_command():
    link = FakeCanLink()
    controller = CanRelayLink(link, FakeRC())
    await controller.write(LightTarget(0x00, 0x2E), True)
    wire = parse_ids_can_wire_frame(link.written[0])
    assert (
        wire.message_type,
        wire.source_address,
        wire.target_address,
        wire.message_data,
    ) == (0x82, 0x2D, 0x2E, 0x01)


async def test_can_write_requires_a_claimed_local_host():
    link = FakeCanLink()
    link.local_host_address = None
    with pytest.raises(LinkError):
        await CanRelayLink(link, FakeRC()).write(LightTarget(0x00, 0x2E), True)
    assert link.written == []


async def test_can_confirm_reads_the_int_keyed_can_view():
    link = FakeCanLink()
    controller = CanRelayLink(link, FakeRC())
    link.can_relay_states[0x2E] = True
    assert await controller.confirm(LightTarget(0x00, 0x2E), True, timeout=0.0) is True
    assert await controller.confirm(LightTarget(0x00, 0x2E), False, timeout=0.0) is False


async def test_can_current_state_reads_the_int_keyed_can_view():
    link = FakeCanLink()
    controller = CanRelayLink(link, FakeRC())
    assert await controller.current_state(LightTarget(0x00, 0x41)) is None
    link.can_relay_states[0x41] = False
    assert await controller.current_state(LightTarget(0x00, 0x41)) is False


async def test_can_aclose_cancels_the_rc_heartbeats():
    link = FakeCanLink()
    rc = FakeRC()
    controller = CanRelayLink(link, rc)
    await controller.aclose()
    assert rc.closes == 1


async def test_can_ensure_re_registers_frame_dispatch_after_link_loss():
    """link.close() (reconnect path) wipes on_frame; ensure restores rc dispatch
    BEFORE the session seed request needs the response hook."""
    link = FakeCanLink()
    rc = FakeRC()
    controller = CanRelayLink(link, rc)
    assert link.on_frame is None
    await controller.ensure(LightTarget(0x00, 0x2E))
    assert link.on_frame == rc.handle_frame


# ---- family detection ------------------------------------------------------


def test_link_family_defaults_to_can_for_links_without_the_attribute():
    assert link_family(FakeCanLink()) == "can"


def test_link_family_reads_the_real_attribute():
    link = FakeDataLink()
    link.family = "data"
    assert link_family(link) == "data"


def test_link_family_survives_a_raising_property():
    class Broken:
        @property
        def family(self):
            raise LinkError("not connected")

    assert link_family(Broken()) == "can"

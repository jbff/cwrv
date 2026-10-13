# tests/test_executor.py
import asyncio
import time

import pytest

from rvcw.protocol import parse_ids_can_wire_frame

from rvcw.ble.transport import LinkError
from rvcw.config import GatewayConfig
from rvcw.executor import SendAborted, SendStats, send_steps
from rvcw.morse import Step

CONFIG = GatewayConfig(mac="FA:KE")  # reconnect fake ignores it


class TimelineLink:
    """Records write times by parsing the actual relay command frames."""

    def __init__(self, confirm_on_write_no: int = 0, confirm_delay_s: float = 0.0):
        self.local_host_address = 0x2D
        self.relay_states: dict[int, bool] = {}
        self.writes: list[tuple[float, int, bool]] = []  # (t_s, target, on)
        self._t0 = None
        self.on_frame = None
        # Confirm simulation: mark relay_states once this device has received
        # this many TYPE-2 relay writes (0 = never confirms; 1 = immediate).
        self.confirm_on_write_no = confirm_on_write_no
        # DEVICE_STATUS latency: the confirmation lands this many seconds
        # AFTER the write (loop.call_later, like the real notification path).
        self.confirm_delay_s = confirm_delay_s
        self._device_writes: dict[int, int] = {}

    async def write_can_frame(self, frame: bytes) -> None:
        if self._t0 is None:
            self._t0 = time.monotonic()
        wire = parse_ids_can_wire_frame(frame)
        self.writes.append(
            (time.monotonic() - self._t0, wire.target_address, wire.message_data == 0x01)
        )
        if wire.message_type == 0x82 and wire.target_address is not None:
            count = self._device_writes.get(wire.target_address, 0) + 1
            self._device_writes[wire.target_address] = count
            if self.confirm_on_write_no and count >= self.confirm_on_write_no:
                self._confirm_later(wire.target_address, wire.message_data == 0x01)

    def _confirm_later(self, device_id: int, on: bool) -> None:
        if self.confirm_delay_s <= 0.0:
            self.relay_states[device_id] = on
        else:
            loop = asyncio.get_running_loop()
            loop.call_later(self.confirm_delay_s, self._mark_state, device_id, on)

    def _mark_state(self, device_id: int, on: bool) -> None:
        self.relay_states[device_id] = on

    def mark(self, device_id: int, on: bool) -> None:
        self.relay_states[device_id] = on


class FakeRC:
    def __init__(self):
        self.ensured: list[int] = []
        self.closes = 0

    def handle_frame(self, frame) -> None:  # re-registered after reconnect
        pass

    async def ensure(self, device_id: int) -> bool:
        self.ensured.append(device_id)
        return True

    async def aclose(self) -> None:
        self.closes += 1


async def test_schedule_write_times_match_elements():
    # A = [on 150, off 150, on 450]
    link = TimelineLink()
    rc = FakeRC()  # type: ignore[arg-type]
    # bypass confirm: confirm=False path
    stats = await send_steps(
        link, rc, [0x2E], [Step(True, 150), Step(False, 150), Step(True, 450)],
        config=CONFIG, confirm=False,
    )  # type: ignore[arg-type]
    assert isinstance(stats, SendStats)
    on_times = [w[0] for w in link.writes if w[2]]
    assert len(on_times) == 2
    # 3 element writes + the off-guarantee restore (pre-state unknown -> OFF)
    assert len(link.writes) == 4
    assert link.writes[-1][2] is False
    # first write fires immediately (~0 ms); second ~150 ms; third ~300 ms
    assert on_times[0] < 0.03 and 0.13 < link.writes[1][0] < 0.2
    assert 0.28 < link.writes[2][0] < 0.5


async def test_abort_on_two_consecutive_misses():
    # confirm requested but the fake link NEVER updates relay_states
    link = TimelineLink()
    rc = FakeRC()

    with pytest.raises(SendAborted):
        await send_steps(
            link, rc, [0x2E], [Step(True, 150), Step(False, 150)],
            config=CONFIG, confirm=True, confirm_timeout=0.05,
        )  # type: ignore[arg-type]


async def test_off_restore_when_pre_state_unknown():
    """No DEVICE_STATUS seen pre-send -> the off-guarantee forces OFF."""
    link = TimelineLink()
    stats = await send_steps(
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)],
        config=CONFIG, confirm=False,
    )
    on_writes = [w for w in link.writes if w[2]]
    off_writes = [w for w in link.writes if not w[2]]
    # writes: element on, element off, restore off (the final write)
    assert len(off_writes) == 2 and off_writes[-1] == link.writes[-1]
    assert len(on_writes) == 1
    assert stats.restore_failures == 0


async def test_restore_returns_to_known_pre_state():
    """The light was ON before the send -> the restore returns it to ON."""
    link = TimelineLink()
    link.mark(0x2E, True)  # pre-send DEVICE_STATUS says ON
    await send_steps(
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)],
        config=CONFIG, confirm=False,
    )
    restores = link.writes[-1]
    assert restores[2] is True  # final write is ON, matching the pre-state


async def test_rewrite_on_missed_confirm_counts_miss_and_completes():
    """The rewrite lands the confirmation (link marks from write #2 on):
    one miss counted, consecutive counter reset, send completes, restore off."""
    link = TimelineLink(confirm_on_write_no=2)
    stats = await send_steps(
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)],
        config=CONFIG, confirm=True, confirm_timeout=0.05,
    )
    assert stats.misses == 1
    # writes: on, on-rewrite (marks ON), off (marks OFF -> confirmed), restore
    assert len(link.writes) == 4
    assert not link.writes[-1][2]  # final restore is OFF
    assert stats.restore_failures == 0


async def test_rtt_measured_on_first_confirmed_element():
    link = TimelineLink(confirm_on_write_no=1)
    stats = await send_steps(
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)],
        config=CONFIG, confirm=True, confirm_timeout=0.5,
    )
    assert stats.rtt_ms is not None
    assert stats.rtt_ms < 50.0  # immediate confirmation ≈ no measurable rtt
    assert stats.misses == 0


async def test_restore_failure_is_counted_not_raised():
    """Link dies after the element writes: restore failures count, no raise."""

    class DyingLink(TimelineLink):
        def __init__(self):
            super().__init__()
            self.write_budget = 2  # both element writes, then down

        async def write_can_frame(self, frame: bytes) -> None:
            if self.write_budget <= 0:
                raise OSError("link gone")
            self.write_budget -= 1
            await super().write_can_frame(frame)

    link = DyingLink()
    stats = await send_steps(
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)],
        config=CONFIG, confirm=False,
    )
    assert stats.restore_failures == 1


async def test_confirm_latency_does_not_drift_next_write_boundary():
    """Confirm arrive-late must not stretch the next element's write time.

    Elements on/off at 0/150 ms with 100 ms confirm latency on the FIRST
    element: the second write must still land ≈150 ms (the old bug slept a
    delay computed BEFORE the confirm gather, pushing the second write to
    ≈250 ms).
    """
    link = TimelineLink(confirm_on_write_no=1, confirm_delay_s=0.10)
    rc = FakeRC()  # type: ignore[arg-type]
    await send_steps(
        link, rc, [0x2E], [Step(True, 150), Step(False, 150)],
        config=CONFIG, confirm=True, confirm_timeout=0.5,
    )  # type: ignore[arg-type]
    # PINNING-ASSERT (second write lands on its scheduled boundary):
    assert 0.145 <= link.writes[1][0] < 0.17, link.writes


async def test_empty_device_ids_rejected():
    link = TimelineLink()
    with pytest.raises(ValueError):
        await send_steps(link, FakeRC(), [], [Step(True, 100)], config=CONFIG)


# ---- spec §Safety "Reconnect" (mid-message BLE loss, resume once) --------


class FlakyLink(TimelineLink):
    """Drops link-level (`LinkError`) after `budget` successful writes until
    a successful reconnect has happened (`down` cleared by the fake)."""

    def __init__(self, budget: int):
        super().__init__()
        self.budget = budget
        self.down = True
        self.reconnects = 0

    async def write_can_frame(self, frame: bytes) -> None:
        if self.budget > 0:
            self.budget -= 1
        elif self.down:
            raise LinkError("mid-schedule BLE loss")
        await super().write_can_frame(frame)


async def test_link_loss_reconnects_once_and_resumes_remaining_schedule(monkeypatch):
    from rvcw import executor

    link = FlakyLink(budget=2)  # steps 1-2 write ok; step 3's write raises
    rc = FakeRC()
    steps = [Step(True, 100), Step(False, 100), Step(True, 100)]

    async def fake_reconnect(glink, config):
        assert config is CONFIG
        glink.reconnects += 1
        glink.down = False  # the "re-established" link works

    monkeypatch.setattr(executor, "reconnect_gateway", fake_reconnect)

    stats = await send_steps(
        link, rc, [0x2E], steps, config=CONFIG, confirm=False
    )
    assert link.reconnects == 1
    # resumed at the CURRENT position: no step restarted, so each element is
    # written exactly once (3 element writes) + the off-guarantee restore
    assert len(link.writes) == 4
    assert [w[2] for w in link.writes] == [True, False, True, False]
    assert link.writes[2][0] < 0.25  # the retried element is not re-sequenced
    assert stats.restore_failures == 0
    # the RC was re-seeded after the reconnect (session rebuild path)
    assert rc.closes == 1
    assert rc.ensured == [0x2E, 0x2E]  # initial ensure, then the re-ensure


async def test_second_link_failure_raises_link_error(monkeypatch):
    from rvcw import executor

    class NoHelpLink(FlakyLink):
        pass  # `down` never clears: reconnect does NOT fix the write path

    link = NoHelpLink(budget=0)

    async def fake_reconnect(glink, config):
        glink.reconnects += 1  # claims success, but the link stays dead

    monkeypatch.setattr(executor, "reconnect_gateway", fake_reconnect)

    with pytest.raises(LinkError):
        await send_steps(
            link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)],
            config=CONFIG, confirm=False,
        )
    assert link.reconnects == 1  # exactly one reconnect attempt, then abort


async def test_failed_reconnect_raises_immediately(monkeypatch):
    from rvcw import executor

    link = FlakyLink(budget=0)

    async def bad_reconnect(glink, config):
        raise LinkError("reconnect itself failed")

    monkeypatch.setattr(executor, "reconnect_gateway", bad_reconnect)

    with pytest.raises(LinkError, match="reconnect itself failed"):
        await send_steps(
            link, FakeRC(), [0x2E], [Step(True, 100)], config=CONFIG, confirm=False
        )

# tests/test_executor.py
import time

import pytest

from rvcw.protocol import parse_ids_can_wire_frame

from rvcw.executor import SendAborted, SendStats, send_steps
from rvcw.morse import Step


class TimelineLink:
    """Records write times by parsing the actual relay command frames."""

    def __init__(self, confirm_on_write_no: int = 0):
        self.local_host_address = 0x2D
        self.relay_states: dict[int, bool] = {}
        self.writes: list[tuple[float, int, bool]] = []  # (t_s, target, on)
        self._t0 = None
        self.on_frame = None
        # Confirm simulation: mark relay_states once this device has received
        # this many TYPE-2 relay writes (0 = never confirms; 1 = immediate).
        self.confirm_on_write_no = confirm_on_write_no
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
                self.relay_states[wire.target_address] = wire.message_data == 0x01

    def mark(self, device_id: int, on: bool) -> None:
        self.relay_states[device_id] = on


class FakeRC:
    open = True
    target = None

    async def ensure(self, device_id: int) -> bool:
        return True


async def test_schedule_write_times_match_elements():
    # A = [on 150, off 150, on 450]
    link = TimelineLink()
    rc = FakeRC()  # type: ignore[arg-type]
    # bypass confirm: confirm=False path
    stats = await send_steps(
        link, rc, [0x2E], [Step(True, 150), Step(False, 150), Step(True, 450)], confirm=False
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
            link, rc, [0x2E], [Step(True, 150), Step(False, 150)], confirm=True, confirm_timeout=0.05
        )  # type: ignore[arg-type]


async def test_off_restore_when_pre_state_unknown():
    """No DEVICE_STATUS seen pre-send -> the off-guarantee forces OFF."""
    link = TimelineLink()
    stats = await send_steps(
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)], confirm=False
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
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)], confirm=False
    )
    restores = link.writes[-1]
    assert restores[2] is True  # final write is ON, matching the pre-state


async def test_rewrite_on_missed_confirm_counts_miss_and_completes():
    """The rewrite lands the confirmation (link marks from write #2 on):
    one miss counted, consecutive counter reset, send completes, restore off."""
    link = TimelineLink(confirm_on_write_no=2)
    stats = await send_steps(
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)], confirm=True, confirm_timeout=0.05
    )
    assert stats.misses == 1
    # writes: on, on-rewrite (marks ON), off (marks OFF -> confirmed), restore
    assert len(link.writes) == 4
    assert not link.writes[-1][2]  # final restore is OFF
    assert stats.restore_failures == 0


async def test_rtt_measured_on_first_confirmed_element():
    link = TimelineLink(confirm_on_write_no=1)
    stats = await send_steps(
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)], confirm=True, confirm_timeout=0.5
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
        link, FakeRC(), [0x2E], [Step(True, 100), Step(False, 100)], confirm=False
    )
    assert stats.restore_failures == 1


async def test_empty_device_ids_rejected():
    link = TimelineLink()
    with pytest.raises(ValueError):
        await send_steps(link, FakeRC(), [], [Step(True, 100)])

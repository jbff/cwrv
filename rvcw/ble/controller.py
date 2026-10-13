"""Relay controllers: one ensure/write/confirm surface per gateway family.

The executor (pacing, supervision, off-guarantee) is family-agnostic; the
controller is where the two families diverge:

``CobsRelayLink``
    Legacy MyRvLink COBS gateways: auth lives on the link
    (:meth:`GatewayLink.authenticate_data`), writes are single-device
    ActionSwitch commands (0x40) sent to DATA_WRITE, and confirmations poll
    the tuple-keyed ``link.relay_states`` RelayStatus view.

``CanRelayLink``
    IDS-CAN gateways: delegates to the existing session machinery
    (:class:`~rvcw.ble.session.RemoteControl`) and the int-keyed
    ``link.can_relay_states`` DEVICE_STATUS view — the CAN code is wrapped,
    not rewritten.
"""
from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable

from rvcw.lights import LightTarget
from rvcw.protocol.commands import CommandBuilder

from .can import confirm_relay, relay_command
from .session import RemoteControl
from .transport import GatewayLink, LinkError

_CONFIRM_POLL_INTERVAL_S = 0.05


@runtime_checkable
class RelayController(Protocol):
    """Family-agnostic relay surface used by :func:`rvcw.executor.send_steps`.

    ``write`` is write-ONLY — the executor owns pacing. ``current_state``
    snapshots the family's relay-state view for the executor's pre-send
    capture (restores are keyed by ``target.key``). ``supervision`` says
    whether the executor confirms BLOCKING inside each element's window
    (CAN: the echo gates the next write) or ASYNCHRONOUS (data family:
    writes never wait on the — possibly slow — RelayStatus echo; misses are
    counted post-hoc and never abort).
    """

    supervision: str

    async def ensure(self, target: LightTarget) -> bool: ...

    async def write(self, target: LightTarget, on: bool) -> None: ...

    async def confirm(self, target: LightTarget, on: bool, timeout: float) -> bool: ...

    async def current_state(self, target: LightTarget) -> bool | None: ...

    async def aclose(self) -> None: ...


def link_family(link: object) -> str:
    """Family of ``link`` ("data"/"can"), tolerating doubles without the
    attribute or a property that needs a live client — those read as "can",
    the pre-COBS default."""
    try:
        family = getattr(link, "family", None)
    except Exception:
        return "can"
    return family if family in ("data", "can") else "can"


def make_controller(link: GatewayLink, rc: RemoteControl | None) -> RelayController:
    """Pick the family controller for a link (rc=None means the data family)."""
    if rc is None:
        return CobsRelayLink(link)
    return CanRelayLink(link, rc)


class CobsRelayLink:
    """RelayController for legacy COBS gateways (data family)."""

    # ASYNC supervision: the RelayStatus echo can lag ~0.5 s on real
    # hardware — awaiting it inside an element's window would abort every
    # fast send. The executor counts echoes post-hoc instead.
    supervision = "async"

    def __init__(self, link: GatewayLink, builder: CommandBuilder | None = None) -> None:
        self._link = link
        # ONE shared builder for every switch write: monotonically increasing
        # command ids across the session (upstream correlation semantics).
        self._builder = builder if builder is not None else CommandBuilder()

    async def ensure(self, target: LightTarget) -> bool:
        # Auth is the link's (authenticate_data ran / reruns on reconnect);
        # there is no per-device session to establish.
        return True

    async def write(self, target: LightTarget, on: bool) -> None:
        # One ActionSwitch per target — the executor does per-target
        # confirmation regardless; combos never collapse into a bulk write.
        raw = self._builder.build_action_switch(
            target.table_id, on, [target.device_id]
        )
        await self._link.send_command(raw)

    async def confirm(self, target: LightTarget, on: bool, timeout: float) -> bool:
        """Poll the RelayStatus view every 50 ms to the deadline, final check
        (`state is on` — identity, not truthiness) — mirrors confirm_relay."""
        deadline = time.monotonic() + timeout
        states = self._link.relay_states
        while time.monotonic() < deadline:
            state = states.get(target.key)
            if state is on:
                return True
            await asyncio.sleep(_CONFIRM_POLL_INTERVAL_S)
        return states.get(target.key) is on

    async def current_state(self, target: LightTarget) -> bool | None:
        return self._link.relay_states.get(target.key)

    async def aclose(self) -> None:
        # Nothing session-scoped to close: teardown (keepalive, BLE link)
        # stays the link owner's job.
        return None


class CanRelayLink:
    """RelayController wrapping the existing CAN machinery (IDS-CAN family)."""

    # BLOCKING supervision (unchanged): DEVICE_STATUS confirms inside each
    # element's window gate the next write, two consecutive misses abort.
    supervision = "blocking"

    def __init__(self, link: GatewayLink, rc: RemoteControl) -> None:
        self._link = link
        self._rc = rc

    async def ensure(self, target: LightTarget) -> bool:
        # A close()ed link (reconnect path) wipes on_frame; re-register the
        # session-response dispatch BEFORE the seed request needs it.
        if self._link.on_frame is None:
            self._link.on_frame = self._rc.handle_frame
        return await self._rc.ensure(target.device_id)

    async def write(self, target: LightTarget, on: bool) -> None:
        if self._link.local_host_address is None:
            raise LinkError("LocalHost address not claimed")
        await self._link.write_can_frame(
            relay_command(self._link.local_host_address, target.device_id, on)
        )

    async def confirm(self, target: LightTarget, on: bool, timeout: float) -> bool:
        return await confirm_relay(self._link, target.device_id, on, timeout=timeout)

    async def current_state(self, target: LightTarget) -> bool | None:
        return self._link.can_relay_states.get(target.device_id)

    async def aclose(self) -> None:
        # Cancels ALL heartbeats WITHOUT SESSION_END (the 1.0.48 relay gotcha).
        await self._rc.aclose()

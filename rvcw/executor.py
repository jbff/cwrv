"""Schedule execution against the live gateway."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from bleak import BleakError

from .ble.can import claim_local_host, confirm_relay, relay_command
from .ble.session import RemoteControl, RemoteControlSessionError, unlock_gateway
from .ble.transport import GatewayLink, LinkError
from .config import GatewayConfig
from .morse import Step

_log = logging.getLogger(__name__)

# Link-level failures that a single reconnect attempt may cover (spec §Safety).
_LINK_ERRORS = (LinkError, BleakError)


@asynccontextmanager
async def open_gateway(
    config: GatewayConfig, observed: set[int] | None = None
) -> AsyncIterator[tuple[GatewayLink, RemoteControl]]:
    """Connect + unlock + claim LocalHost; RC heartbeat cancelled on exit."""
    link = GatewayLink(config.mac, config.pin)
    rc: RemoteControl | None = None
    try:
        # inside try: close() still runs if a later step fails
        await _establish(link, observed if observed is not None else set())
        rc = RemoteControl(link)
        link.on_frame = rc.handle_frame
        yield link, rc
    finally:
        if rc is not None:
            await rc.aclose()  # cancel heartbeat; NOT end() (relay gotcha)
        await link.close()


async def _establish(link: GatewayLink, observed: set[int]) -> None:
    """Connect + key/seed-unlock + claim LocalHost, on this link object.

    Reused by :func:`open_gateway` and :func:`reconnect_gateway`; a reconnect
    re-uses the same GatewayLink so callers keep one link/RC identity.
    """
    await link.connect()
    await unlock_gateway(link)
    await claim_local_host(link, observed)


async def reconnect_gateway(link: GatewayLink, config: GatewayConfig) -> None:
    """Re-establish the BLE gateway link once (spec §Safety "Reconnect").

    Closes the dead link (if connected), then re-runs the full
    connect/unlock/claim flow via the same :class:`GatewayLink` object. The
    gateway drops REMOTE_CONTROL sessions with the BLE link, so callers must
    reset their :class:`RemoteControl` (``aclose``) and ``ensure`` again
    before further session-scoped writes.
    """
    try:
        await link.close()  # no-op when the client is already gone
    except Exception:
        pass  # the link is dead; dropping its state is what matters
    await _establish(link, set())


@dataclass
class SendStats:
    """Outcome of one send: confirm misses, restore failures, measured RTT."""

    rtt_ms: float | None = None
    misses: int = 0
    restore_failures: int = 0


class SendAborted(RuntimeError):
    pass


async def send_steps(
    link: GatewayLink,
    rc: RemoteControl,
    device_ids: list[int],
    steps: list[Step],
    *,
    config: GatewayConfig,
    confirm: bool = True,
    confirm_timeout: float = 2.0,
) -> SendStats:
    """Clock-paced writes, confirmation-supervised, with the off-guarantee.

    The first write fires as soon as possible (there is no scheduled start
    delay); later writes land at their scheduled wall-clock times or later
    (a confirmation window that outlives its element never makes a write
    early). DEVICE_STATUS confirmations are checked inside each element's
    window, with at most one idempotent rewrite per missed element; two
    consecutive missed elements abort. Mid-message link loss reconnects ONCE
    and resumes at the current schedule position; a second link failure (or a
    failed reconnect) aborts. EVERY exit path — including aborts and link
    death — restores the pre-send state of every touched device (forced OFF
    when the pre-state was unknown).
    """
    if not device_ids:
        raise ValueError("no devices to blink")
    for device_id in device_ids:
        if not await rc.ensure(device_id):
            raise RemoteControlSessionError(f"no session for 0x{device_id:02X}")

    pre = {d: link.can_relay_states.get(d) for d in device_ids}
    stats = SendStats()
    start = time.monotonic()
    pos = 0.0
    consecutive_misses = 0

    async def confirm_one(device_id: int, on: bool) -> bool:
        """DEVICE_STATUS check that also captures the first confirmed latency."""
        t0 = time.monotonic()
        ok = await confirm_relay(link, device_id, on, timeout=confirm_timeout)
        if ok and stats.rtt_ms is None:
            stats.rtt_ms = (time.monotonic() - t0) * 1000.0
        return ok

    async def resume_sessions() -> None:
        """After a reconnect: re-register frame dispatch and rebuild sessions.

        The gateway drops every REMOTE_CONTROL session with the BLE link, and
        latching-relay writes are idempotent, so re-ensuring every device is
        always safe.
        """
        link.on_frame = rc.handle_frame
        await rc.aclose()
        for device_id in device_ids:
            await rc.ensure(device_id)

    link_failures = 0
    index = 0
    try:
        while index < len(steps):
            step = steps[index]
            written = False  # did the element write itself land?
            try:
                await asyncio.gather(*(
                    link.write_can_frame(relay_command(link.local_host_address, d, step.on))
                    for d in device_ids
                ))
                written = True
                pos += step.ms  # AFTER the write: the first write is never delayed
                if confirm:
                    confirmed = await asyncio.gather(*(
                        confirm_one(d, step.on) for d in device_ids
                    ))
                    if all(confirmed):
                        consecutive_misses = 0
                    else:
                        # idempotent rewrite for unconfirmed devices (latching
                        # relay: rewriting the same state is a no-op on the light)
                        for d, ok in zip(device_ids, confirmed):
                            if not ok:
                                await link.write_can_frame(
                                    relay_command(link.local_host_address, d, step.on)
                                )
                                stats.misses += 1
                        consecutive_misses += 1
                        if consecutive_misses >= 2:
                            raise SendAborted(
                                "two consecutive steps unconfirmed; aborting"
                            )
            except _LINK_ERRORS as exc:
                link_failures += 1
                if link_failures > 1:
                    raise
                _log.warning("gateway link lost mid-schedule (%s); reconnecting", exc)
                await reconnect_gateway(link, config)
                await resume_sessions()
                if written:
                    index += 1  # element is on the bus; resume at the NEXT step
                # else: resume at THIS step (pos was not advanced past it)
                continue
            # Recompute AFTER any confirm work: a confirm window that consumed
            # part of the element must not stretch the next write past its
            # scheduled boundary; the clock stays absolute (start + pos).
            delay = start + pos / 1000.0 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            index += 1
    finally:
        for d in device_ids:
            state = pre.get(d)
            state = state if state is not None else False
            try:
                await link.write_can_frame(
                    relay_command(link.local_host_address, d, state)
                )
            except Exception:
                stats.restore_failures += 1
        if stats.restore_failures:
            _log.warning(
                "link lost during restore for %d device(s); lights may not be "
                "back at pre-send state",
                stats.restore_failures,
            )
    return stats

"""Schedule execution against the live gateway."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from bleak import BleakError

from .ble.can import claim_local_host
from .ble.controller import RelayController, link_family
from .ble.session import RemoteControl, RemoteControlSessionError, unlock_gateway
from .ble.transport import GatewayLink, LinkError
from .config import GatewayConfig
from .lights import LightTarget
from .morse import Step

_log = logging.getLogger(__name__)

# Link-level failures that a single reconnect attempt may cover (spec §Safety).
_LINK_ERRORS = (LinkError, BleakError)


@asynccontextmanager
async def open_gateway(
    config: GatewayConfig, observed: set[int] | None = None
) -> AsyncIterator[tuple[GatewayLink, RemoteControl | None]]:
    """Connect + family establishment; RC heartbeat cancelled on exit.

    CAN family: key/seed unlock + LocalHost claim, yields the GatewayLink
    with a fresh :class:`RemoteControl`. Data family (legacy COBS): step-1/2
    auth + the gateway-info wait + the GetDevices keepalive, and yields
    ``rc=None`` (no session machinery exists on that path).
    """
    link = GatewayLink(config.mac, config.pin)
    rc: RemoteControl | None = None
    try:
        # inside try: close() still runs if a later step fails
        await link.connect()
        if link_family(link) == "data":
            await link.authenticate_data(config.pin)
            await link.wait_gateway_info(timeout=10)
            await link.start_keepalive()  # stopped by close() at teardown
            yield link, None
            return
        rc = RemoteControl(link)
        link.on_frame = rc.handle_frame
        await unlock_gateway(link)
        await claim_local_host(link, observed if observed is not None else set())
        yield link, rc
    finally:
        if rc is not None:
            await rc.aclose()  # cancel heartbeat; NOT end() (relay gotcha)
        await link.close()


async def _establish(link: GatewayLink, observed: set[int]) -> None:
    """Establish the gateway link on this link object (family-aware).

    Reused by :func:`open_gateway` and :func:`reconnect_gateway`; a reconnect
    re-uses the same GatewayLink so callers keep one link/RC identity. For the
    data family the COBS auth is the re-establishment itself (keepalive
    restarts, skipped ticks until the gateway re-announces).
    """
    await link.connect()
    if link_family(link) == "data":
        await link.authenticate_data(link.pin)
        await link.start_keepalive()
        return
    await unlock_gateway(link)
    await claim_local_host(link, observed)


async def reconnect_gateway(link: GatewayLink, config: GatewayConfig) -> None:
    """Re-establish the BLE gateway link once (spec §Safety "Reconnect").

    Closes the dead link (if connected), then re-runs the family's
    connect/auth flow via the same :class:`GatewayLink` object. The gateway
    drops REMOTE_CONTROL sessions with the BLE link, so callers must reset
    their :class:`RemoteControl` (``aclose``) and ``ensure`` again before
    further session-scoped writes (the CAN controller does that in
    ``ensure``); the data family just re-authenticates via the link.
    """
    try:
        await link.close()  # no-op when the client is already gone
    except Exception:
        pass  # the link is dead; dropping its state is what matters
    await _establish(link, set())


@dataclass
class SendStats:
    """Outcome of one send: confirm misses, restore failures, measured RTT.

    ``rtt_ms`` is the FIRST confirmed echo's latency; ``echo_max_ms`` (data
    family / async supervision only) is the maximum latency across ALL
    confirmed echoes. On the data family the echo is supervisory only — it is
    not used for the WPM advisory.
    """

    rtt_ms: float | None = None
    misses: int = 0
    restore_failures: int = 0
    echo_max_ms: float | None = None


class SendAborted(RuntimeError):
    pass


async def send_steps(
    link: GatewayLink,
    controller: RelayController,
    targets: list[LightTarget],
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
    early). Relay-state confirmations are checked inside each element's
    window, with at most one idempotent rewrite per missed element; two
    consecutive missed elements abort. Mid-message link loss reconnects ONCE
    and resumes at the current schedule position; a second link failure (or a
    failed reconnect) aborts. EVERY exit path — including aborts and link
    death — restores the pre-send state of every touched target (forced OFF
    when the pre-state was unknown). Writes go through the family
    :class:`RelayController`, one write per target.

    Supervision mode comes from ``controller.supervision``:

    * ``"blocking"`` (CAN): confirms are awaited inside each element's
      window; two consecutive missed elements abort (off-guarantee).

    * ``"async"`` (data family / COBS): after each element's write the
      confirms are spawned as tasks and NOT awaited — the RelayStatus echo
      can lag ~0.5 s and never gates when the next write happens. Finished
      echo tasks are reaped non-blocking right before the next write and
      their misses counted POST-HOC; there is NO SendAborted from echo lag
      (a lagging echo cannot save the message, and the gateway may still
      apply pending writes — report instead). At teardown, still-outstanding
      echo tasks get one 0.5 s reap; unfinished ones at the deadline are
      cancelled and counted as misses.
    """
    if not targets:
        raise ValueError("no devices to blink")
    for target in targets:
        if not await controller.ensure(target):
            raise RemoteControlSessionError(f"no session for 0x{target.device_id:02X}")

    # Pre-send state keyed by (table, device); None = unknown -> forced OFF
    # at restore. The controller picks the family's state view.
    pre = {t.key: await controller.current_state(t) for t in targets}
    stats = SendStats()
    start = time.monotonic()
    pos = 0.0
    consecutive_misses = 0
    # Async supervision state (data family): one confirm task per
    # (element, target); reaped non-blocking, post-hoc.
    supervise_async = confirm and getattr(controller, "supervision", "blocking") == "async"
    pending: list[asyncio.Task[bool]] = []
    echo_latencies: list[float] = []

    async def confirm_one(target: LightTarget, on: bool) -> bool:
        """Relay-state check that also captures confirmed echo latencies."""
        t0 = time.monotonic()
        ok = await controller.confirm(target, on, timeout=confirm_timeout)
        if ok:
            latency = (time.monotonic() - t0) * 1000.0
            echo_latencies.append(latency)
            if stats.rtt_ms is None:
                stats.rtt_ms = latency
        return ok

    def reap_done() -> None:
        """Non-blocking reap of finished echo tasks; post-hoc miss counting.

        Blocking only for exceptions (e.g. link loss), which surface through
        the normal link-failure path. In async mode a reap NEVER aborts.
        """
        nonlocal consecutive_misses
        done = [t for t in pending if t.done()]
        pending[:] = [t for t in pending if not t.done()]
        if not done:
            return
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc  # link errors flow into the reconnect path
        oks = [task.result() for task in done]
        if all(oks):
            consecutive_misses = 0
        else:
            stats.misses += oks.count(False)
            # NOTE: no SendAborted branch here — async supervision reports,
            # it does not abort (echo lag cannot save the message anyway).
            consecutive_misses += 1

    async def resume_sessions() -> None:
        """After a reconnect: reset controller state and rebuild per target.

        The gateway drops every REMOTE_CONTROL session with the BLE link, and
        latching-relay writes are idempotent, so re-ensuring every target is
        always safe. (The CobsRelayLink re-auth happened inside
        reconnect_gateway via the link itself.)
        """
        await controller.aclose()
        for target in targets:
            await controller.ensure(target)

    link_failures = 0
    index = 0
    try:
        while index < len(steps):
            step = steps[index]
            written = False  # did the element write itself land?
            try:
                await asyncio.gather(*(
                    controller.write(t, step.on) for t in targets
                ))
                written = True
                pos += step.ms  # AFTER the write: the first write is never delayed
                if confirm and not supervise_async:
                    # BLOCKING supervision (CAN): the echo gates the rewrite
                    # decision inside the element's window.
                    confirmed = await asyncio.gather(*(
                        confirm_one(t, step.on) for t in targets
                    ))
                    if all(confirmed):
                        consecutive_misses = 0
                    else:
                        # idempotent rewrite for unconfirmed targets (latching
                        # relay: rewriting the same state is a no-op on the light)
                        for t, ok in zip(targets, confirmed):
                            if not ok:
                                await controller.write(t, step.on)
                                stats.misses += 1
                        consecutive_misses += 1
                        if consecutive_misses >= 2:
                            raise SendAborted(
                                "two consecutive steps unconfirmed; aborting"
                            )
                elif supervise_async:
                    # ASYNC supervision (data family): spawn the echoes, never
                    # wait — they are reaped post-hoc before the next write.
                    pending.extend(
                        asyncio.create_task(confirm_one(t, step.on))
                        for t in targets
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
            if supervise_async:
                reap_done()
            index += 1
    finally:
        # Reap the outstanding echoes (async mode) BEFORE the restore writes:
        # the restore must not race un-acked elements' state reporting.
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=0.5
                )
            except TimeoutError:
                # unfinished at the deadline: counted as misses, tasks cancelled
                for task in pending:
                    if not task.done():
                        task.cancel()
            for task in pending:
                ok = (
                    task.done()
                    and not task.cancelled()
                    and task.exception() is None
                    and task.result() is True
                )
                if not ok:
                    stats.misses += 1
            pending.clear()
        if supervise_async:
            stats.echo_max_ms = max(echo_latencies) if echo_latencies else None
        for t in targets:
            state = pre.get(t.key)
            state = state if state is not None else False
            try:
                await controller.write(t, state)
            except Exception:
                stats.restore_failures += 1
        if stats.restore_failures:
            _log.warning(
                "link lost during restore for %d target(s); lights may not be "
                "back at pre-send state",
                stats.restore_failures,
            )
    return stats

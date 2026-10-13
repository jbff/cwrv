"""rvcw command-line interface."""
from __future__ import annotations

import asyncio
import time as _time_mod

import click

from . import __version__, lights, morse
from .ble.controller import link_family, make_controller
from .ble.session import RemoteControlSessionError
from .config import lights_path, load_gateway
from .lights import LightEntry, LightTarget
from .protocol import FUNCTION_NAMES
from .protocol.events import GatewayInformation, RelayStatus, parse_event


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """Blink CW (Morse) messages on RV panel lights."""


def _resolve(
    text: str, light: str, wpm: int, repeat: int
) -> tuple[list[morse.Step], list[LightEntry]]:
    steps = morse.schedule(text, wpm=wpm, repeat=repeat)
    entries = lights.load_map(lights_path()).resolve(light)
    return steps, entries


def _targets(entries: list[LightEntry]) -> list[LightTarget]:
    return [LightTarget(e.table_id, e.device_id) for e in entries]


def _targets_label(entries: list[LightEntry]) -> str:
    return ", ".join(
        f"table=0x{e.table_id:02X} device=0x{e.device_id:04X}" for e in entries
    )


def _parse_device_id(_ctx, _param, value: str) -> int:
    """Accept decimal and 0x-prefixed hex device ids (plain `int` rejects hex)."""
    try:
        return int(value, 0)
    except ValueError:
        raise click.BadParameter(
            f"device id must be an integer like 0x2E41, got {value!r}"
        ) from None


@cli.command()
@click.argument("text")
@click.option("--light", required=True, help="light or combo name from lights.toml")
@click.option("--wpm", default=None, type=int, help="default from config.toml")
@click.option("--repeat", default=1, type=int)
@click.option("--dry-run", is_flag=True)
@click.option(
    "--no-confirm",
    is_flag=True,
    help="skip relay-state waiting (if status parsing failed)",
)
def send(text: str, light: str, wpm: int | None, repeat: int, dry_run: bool, no_confirm: bool) -> None:
    """Blink TEXT as CW on the requested light."""
    if wpm is None:
        wpm = load_gateway().wpm
    try:
        steps, entries = _resolve(text, light, wpm, repeat)
    except (KeyError, ValueError) as exc:
        # Fail fast with a visible message (unknown light = KeyError; bad
        # character/wpm/repeat or the removed device_id mapping = ValueError).
        # KeyError's str() reprs its arg, so re-raise the original message.
        msg = str(exc.args[0]) if exc.args else str(exc)
        raise click.ClickException(msg) from None
    if dry_run:
        click.echo(
            f"light={light} targets=[{_targets_label(entries)}] "
            f"wpm={wpm} steps={len(steps)} duration={sum(s.ms for s in steps)}ms"
        )
        for s in steps:
            click.echo(f"{'ON ' if s.on else 'off'}{s.ms:6d}ms")
        return

    from .ble.transport import LinkError
    from .executor import SendAborted, SendStats, open_gateway, send_steps

    confirm_timeout = max(0.05, (1200 / wpm) / 1000 * 0.8)

    async def run_live() -> SendStats:
        cfg = load_gateway()
        async with open_gateway(cfg) as (link, rc):
            controller = make_controller(link, rc)
            return await send_steps(
                link, controller, _targets(entries), steps,
                config=cfg,
                confirm=not no_confirm,
                confirm_timeout=confirm_timeout,
            )

    try:
        click.echo("connecting…")
        stats = asyncio.run(run_live())
    except (SendAborted, RemoteControlSessionError, LinkError) as exc:
        click.echo(f"aborted: {exc}", err=True)
        raise SystemExit(1) from None
    if stats.rtt_ms is not None and stats.rtt_ms > 0:
        max_wpm = int(1200 / max(stats.rtt_ms / 0.60, 1.0))
        if wpm > max_wpm:
            click.echo(
                f"warning: requested {wpm} WPM, measured RTT {stats.rtt_ms:.0f}ms "
                f"suggests ~{max_wpm} WPM max — reduce --wpm for crisp morse"
            )
    click.echo("sent")


@cli.command()
@click.option("--light", required=True, help="light or combo name from lights.toml")
def selftest(light: str) -> None:
    """Blink 'R' at 5 WPM; verify off-restore and report confirm misses."""
    try:
        steps, entries = _resolve("R", light, 5, 1)
    except (KeyError, ValueError) as exc:
        msg = str(exc.args[0]) if exc.args else str(exc)
        raise click.ClickException(msg) from None

    from .executor import SendAborted, SendStats, open_gateway, send_steps

    async def run() -> SendStats:
        cfg = load_gateway()
        async with open_gateway(cfg) as (link, rc):
            controller = make_controller(link, rc)
            return await send_steps(
                link, controller, _targets(entries), steps, config=cfg, confirm=True
            )

    try:
        stats = asyncio.run(run())
    except (SendAborted, RemoteControlSessionError) as exc:
        click.echo(f"selftest FAILED: {exc}", err=True)
        raise SystemExit(1) from None
    click.echo(f"selftest OK: misses={stats.misses} rtt_ms={stats.rtt_ms}")
    click.echo("check the physical light returned to its prior state now")


@cli.command("session")
@click.option(
    "--end", "end_session", is_flag=True, help="END the REMOTE_CONTROL session"
)
@click.option(
    "--device",
    required=True,
    callback=_parse_device_id,
    help="target relay device id (e.g. 0x2E41)",
)
def session_cmd(end_session: bool, device: int) -> None:
    """Explicitly END the REMOTE_CONTROL session with --device (rarely needed)."""
    if not end_session:
        raise click.ClickException("nothing to do — use session --end --device …")

    from .executor import open_gateway

    async def run() -> None:
        async with open_gateway(load_gateway()) as (link, rc):
            if rc is None:
                click.echo("legacy gateway — COBS path, no sessions needed")
                return
            await rc.ensure(device)
            await rc.end(device)
        click.echo(f"session ended with 0x{device:02X} (local controls regain the device)")

    try:
        asyncio.run(run())
    except RemoteControlSessionError as exc:
        raise click.ClickException(str(exc)) from None


@cli.command("lights")
def lights_cmd() -> None:
    """List mapped lights and combos."""
    m = lights.load_map(lights_path())
    for name in sorted(m.lights):
        entry = m.lights[name]
        click.echo(
            f"light {name} table=0x{entry.table_id:02X} "
            f"device=0x{entry.device_id:04X}"
        )
    for name in sorted(m.combos):
        click.echo(f"combo {name} = {'+'.join(m.combos[name])}")


def _describe_data_event(frame: bytes) -> str:
    """One decoded COBS frame -> a human row (relay/gateway-info highlighted)."""
    raw = f"raw=0x{frame.hex()}"
    try:
        event = parse_event(frame)
    except Exception:
        event = None
    if isinstance(event, GatewayInformation):
        return (
            f"GATEWAY-INFO table=0x{event.table_id:02X} "
            f"devices={event.device_count} {raw}"
        )
    if isinstance(event, RelayStatus):
        return (
            f"RELAY table=0x{event.table_id:02X} device=0x{event.device_id:02X} "
            f"on={event.is_on} {raw}"
        )
    if event is None:
        return f"event heartbeat raw=0x{frame.hex()}"
    if isinstance(event, bytes):  # unknown to the vendored parser
        return f"event unknown {raw}"
    return f"event {type(event).__name__} {raw}"


@cli.command()
def sniff() -> None:
    """Stream decoded gateway events (for deriving status formats)."""

    async def run() -> None:
        cfg = load_gateway()
        from .ble.transport import GatewayLink

        link = GatewayLink(cfg.mac, cfg.pin)
        await link.connect()
        try:
            if link_family(link) == "data":
                await _sniff_data(link, cfg)
                return
            await _sniff_can(link, cfg)
        finally:
            await link.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


async def _sniff_data(link, cfg) -> None:
    """Decode the COBS event stream; relay/gateway-info rows highlighted."""
    start = _time_mod.monotonic()
    count = 0

    def show(frame: bytes) -> None:
        nonlocal count
        count += 1
        click.echo(
            f"#{count:05d} t=+{_time_mod.monotonic() - start:8.3f}s "
            f"{_describe_data_event(frame)}"
        )

    link.on_data_event = show
    await link.authenticate_data(link.pin)
    click.echo(f"sniffing {cfg.mac} (data family, COBS events)… Ctrl-C to stop")
    while True:
        await asyncio.sleep(0.5)


async def _sniff_can(link, cfg) -> None:
    """Stream decoded CAN frames (IDS-CAN family; unchanged from Task 9)."""
    from .ble.session import RemoteControl, unlock_gateway
    from .ble.transport import GatewayLink

    rc = RemoteControl(link)
    loop = asyncio.get_running_loop()
    start = loop.time()
    count = 0

    def show(frame) -> None:
        nonlocal count
        count += 1
        dst = frame.target_address
        data = frame.message_data
        line = (
            f"#{count:05d} t=+{loop.time() - start:8.3f}s "
            f"type=0x{frame.message_type:02X} src=0x{frame.source_address:02X} "
            f"dst={'-' if dst is None else f'0x{dst:02X}'} "
            f"data={'-' if data is None else f'0x{data:02X}'} "
            f"dlc={frame.dlc} payload={frame.payload.hex()}"
        )
        if frame.message_type == 0x03:  # DEVICE_STATUS: help the derivation loop
            line += f" relay_state={GatewayLink.relay_state_from_status(frame.payload)}"
        click.echo(line)

    # rc.handle_frame must be registered before any session request is written
    link.on_frame = lambda frame: (rc.handle_frame(frame), show(frame))
    try:
        await unlock_gateway(link)
        click.echo(f"sniffing {cfg.mac}… Ctrl-C to stop")
        while True:
            await asyncio.sleep(0.5)
    finally:
        await rc.aclose()  # cancels the heartbeat WITHOUT SESSION_END


@cli.command("probe-session")
@click.option(
    "--device",
    required=True,
    callback=_parse_device_id,
    help="target relay device id (e.g. 0x2E41)",
)
def probe_session(device: int) -> None:
    """Report gateway family and, for CAN, the multiplexing verdict. Exits."""

    async def run() -> None:
        from .executor import open_gateway

        cfg = load_gateway()
        async with open_gateway(cfg) as (link, rc):
            click.echo(f"connected mac={cfg.mac}")
            if rc is None:
                click.echo("legacy gateway — COBS path, no sessions needed")
                return
            click.echo(f"frames_received={link.frames_received}")
            try:
                await rc.ensure(device)
            except RemoteControlSessionError as exc:
                click.echo(f"RESULT: REFUSED — {exc}")
                return
            click.echo(f"RESULT: SESSION OPEN with 0x{device:02X} (multiplexing works)")
            click.echo("no command frames sent; leaving bus as found")

    asyncio.run(run())


def _function_label(function_name: int) -> str:
    return FUNCTION_NAMES.get(function_name, f"fn 0x{function_name:04X}")


@cli.command()
def discover() -> None:
    """List relay devices/relays found on the gateway."""

    async def run():
        from .ble.can import claim_local_host
        from .ble.session import unlock_gateway
        from .ble.transport import GatewayLink
        from .discovery import collect_devices, collect_relay_states

        link = GatewayLink(load_gateway().mac, load_gateway().pin)
        await link.connect()
        try:
            if link_family(link) == "data":
                await link.authenticate_data(link.pin)
                table = await link.wait_gateway_info(timeout=10)
                await link.start_keepalive()  # stopped by close() at teardown
                states = await collect_relay_states(link, table, window_s=3.0)
                if not states:
                    click.echo("no relays reported; try again with HA idle", err=True)
                    raise SystemExit(1)
                for (table_id, device_id), is_on in sorted(states.items()):
                    click.echo(
                        f"relay table=0x{table_id:02X} device=0x{device_id:02X} "
                        f"on={is_on}"
                    )
                return
            # CAN family: existing DEVICE_ID-advertisement discovery
            await unlock_gateway(link)
            await claim_local_host(link, set())
            devices = await collect_devices(link, window_s=3.0)
            for d in devices:
                click.echo(
                    f"device 0x{d.address:02X} product={d.product_id} "
                    f"type={d.device_type} {_function_label(d.function_name)}"
                )
        finally:
            await link.close()

    asyncio.run(run())


@cli.command()
@click.argument("name")
@click.option(
    "--yes",
    is_flag=True,
    help="save the first matching candidate without confirming",
)
def learn(name: str, yes: bool) -> None:
    """Guided toggle-and-observe mapping: NAME gets a relay from candidates."""

    async def run():
        from .discovery import collect_relay_states
        from .executor import open_gateway

        async with open_gateway(load_gateway()) as (link, rc):
            if rc is not None:
                await _learn_can(link, rc, name, yes)
                return
            # Data family (legacy COBS): candidates come from the RelayStatus
            # listing; the keepalive/startup lived in open_gateway.
            from .ble.controller import CobsRelayLink

            controller = CobsRelayLink(link)
            table = link.gateway_info.table_id  # open_gateway waited for it
            states = await collect_relay_states(link, table, window_s=3.0)
            candidates = sorted(link.relay_states) or sorted(states)
            if not candidates:
                click.echo("no relays reported; try again with HA idle", err=True)
                raise SystemExit(1)
            for table_id, device_id in candidates:
                target = LightTarget(table_id, device_id)
                click.echo(f"toggling table 0x{table_id:02X} device 0x{device_id:02X} …")
                # OFF (defensive), ON, dwell — ALL wrapped in try/finally so
                # EVERY exit — including KeyboardInterrupt (Ctrl-C), which
                # asyncio surfaces as CancelledError that `except Exception`
                # does NOT catch — commands the best-effort OFF and never
                # latches the light ON. Writes are idempotent (latching relay).
                try:
                    await controller.write(target, False)
                    await asyncio.sleep(0.1)
                    await controller.write(target, True)
                    await asyncio.sleep(0.6)
                except Exception as exc:
                    click.echo(f"  toggle failed: {exc}", err=True)
                    continue
                finally:
                    try:
                        await controller.write(target, False)
                    except Exception:
                        pass  # link is gone; nothing to command anymore
                if yes or click.confirm(f"Was that {name}?"):
                    lights.save_light(lights_path(), name, table_id, device_id)
                    click.echo(
                        f"saved: {name} = table 0x{table_id:02X} device "
                        f"0x{device_id:02X}"
                    )
                    return
        click.echo("no device matched", err=True)
        raise SystemExit(1)

    asyncio.run(run())


async def _learn_can(link, rc, name: str, yes: bool) -> None:
    """CAN-family guided learn (unchanged from the IDS-CAN phase)."""
    from .ble.can import relay_command, set_relay
    from .discovery import DiscoveredDevice, collect_devices

    candidates: list[DiscoveredDevice] = await collect_devices(link, window_s=3.0)
    if not candidates:
        click.echo("no devices found; try again with HA idle", err=True)
        raise SystemExit(1)
    for dev in candidates:
        click.echo(f"toggling device 0x{dev.address:02X} …")
        # ON → 0.6 s dwell → OFF is wrapped in try/finally so EVERY
        # exit — including KeyboardInterrupt (Ctrl-C), which asyncio
        # surfaces as CancelledError that `except Exception` does NOT
        # catch — commands the best-effort OFF and never latches the
        # light ON. The OFF write is idempotent (latching relay), so
        # also sending it after a normal confirmed OFF is harmless.
        try:
            await set_relay(link, rc, dev.address, True)
            await asyncio.sleep(0.6)
            await set_relay(link, rc, dev.address, False)
        except Exception as exc:
            click.echo(f"  toggle failed: {exc}", err=True)
            continue
        finally:
            if link.local_host_address is not None:
                try:
                    await link.write_can_frame(
                        relay_command(link.local_host_address, dev.address, False)
                    )
                except Exception:
                    pass  # link is gone; the gateway drops the session with it
        if yes or click.confirm(f"Was that {name}?"):
            # the CAN family has no COBS device table; 0x00 says "unknown"
            lights.save_light(lights_path(), name, 0x00, dev.address)
            click.echo(f"saved: {name} = 0x{dev.address:02X}")
            return
    click.echo("no device matched", err=True)
    raise SystemExit(1)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

"""rvcw command-line interface."""
from __future__ import annotations

import asyncio

import click

from . import __version__, lights, morse
from .ble.session import RemoteControlSessionError
from .config import lights_path, load_gateway
from .protocol import FUNCTION_NAMES


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """Blink CW (Morse) messages on RV panel lights."""


def _resolve(text: str, light: str, wpm: int, repeat: int) -> tuple[
    list[morse.Step], list[int]
]:
    steps = morse.schedule(text, wpm=wpm, repeat=repeat)
    entries = lights.load_map(lights_path()).resolve(light)
    return steps, [e.device_id for e in entries]


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
    help="skip DEVICE_STATUS waiting (if status parsing failed)",
)
def send(text: str, light: str, wpm: int | None, repeat: int, dry_run: bool, no_confirm: bool) -> None:
    """Blink TEXT as CW on the requested light."""
    if wpm is None:
        wpm = load_gateway().wpm
    try:
        steps, device_ids = _resolve(text, light, wpm, repeat)
    except (KeyError, ValueError) as exc:
        # Fail fast with a visible message (unknown light = KeyError; bad
        # character/wpm/repeat = ValueError). KeyError's str() reprs its arg,
        # so re-raise the original message instead.
        msg = str(exc.args[0]) if exc.args else str(exc)
        raise click.ClickException(msg) from None
    if dry_run:
        click.echo(
            f"light={light} devices={[f'0x{d:04X}' for d in device_ids]} "
            f"wpm={wpm} steps={len(steps)} duration={sum(s.ms for s in steps)}ms"
        )
        for s in steps:
            click.echo(f"{'ON ' if s.on else 'off'}{s.ms:6d}ms")
        return

    from .ble.transport import LinkError
    from .executor import SendAborted, SendStats, open_gateway, send_steps

    confirm_timeout = max(0.05, (1200 / wpm) / 1000 * 0.8)

    async def run_live() -> SendStats:
        async with open_gateway(load_gateway()) as (link, rc):
            return await send_steps(
                link, rc, device_ids, steps,
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
        steps, device_ids = _resolve("R", light, 5, 1)
    except (KeyError, ValueError) as exc:
        msg = str(exc.args[0]) if exc.args else str(exc)
        raise click.ClickException(msg) from None

    from .executor import SendAborted, SendStats, open_gateway, send_steps

    async def run() -> SendStats:
        async with open_gateway(load_gateway()) as (link, rc):
            return await send_steps(link, rc, device_ids, steps, confirm=True)

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
        click.echo(f"light {name} 0x{m.lights[name]:04X}")
    for name in sorted(m.combos):
        click.echo(f"combo {name} = {'+'.join(m.combos[name])}")


@cli.command()
def sniff() -> None:
    """Stream decoded CAN frames (for deriving DEVICE_STATUS relay formats)."""

    async def run() -> None:
        cfg = load_gateway()
        from .ble.session import RemoteControl, unlock_gateway
        from .ble.transport import GatewayLink

        link = GatewayLink(cfg.mac, cfg.pin)
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
        await link.connect()
        try:
            await unlock_gateway(link)
            click.echo(f"sniffing {cfg.mac}… Ctrl-C to stop")
            while True:
                await asyncio.sleep(0.5)
        finally:
            await rc.aclose()  # cancels the heartbeat WITHOUT SESSION_END
            await link.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


@cli.command("probe-session")
@click.option(
    "--device",
    required=True,
    callback=_parse_device_id,
    help="target relay device id (e.g. 0x2E41)",
)
def probe_session(device: int) -> None:
    """Phase-1 gate: second IDS-CAN participant alongside HA. Reports and exits."""

    async def run() -> None:
        from .executor import open_gateway

        cfg = load_gateway()
        async with open_gateway(cfg) as (link, rc):
            click.echo(f"connected mac={cfg.mac} local_host=0x{link.local_host_address:02X}")
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
    """List relay devices found on the IDS-CAN bus."""

    async def run():
        from .ble.can import claim_local_host
        from .ble.session import unlock_gateway
        from .ble.transport import GatewayLink
        from .discovery import collect_devices

        link = GatewayLink(load_gateway().mac, load_gateway().pin)
        await link.connect()
        try:
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
@click.option("--yes", is_flag=True, help="skip confirmation (save all offered)")
def learn(name: str, yes: bool) -> None:
    """Guided toggle-and-observe mapping: NAME gets a device from candidates."""

    async def run():
        from .ble.can import relay_command, set_relay
        from .discovery import DiscoveredDevice, collect_devices
        from .executor import open_gateway

        async with open_gateway(load_gateway()) as (link, rc):
            candidates: list[DiscoveredDevice] = await collect_devices(
                link, window_s=3.0
            )
            if not candidates:
                click.echo("no devices found; try again with HA idle", err=True)
                raise SystemExit(1)
            for dev in candidates:
                click.echo(f"toggling device 0x{dev.address:02X} …")
                try:
                    await set_relay(link, rc, dev.address, True)
                    await asyncio.sleep(0.6)
                    await set_relay(link, rc, dev.address, False)
                except Exception as exc:
                    # With the DEVICE_STATUS parser not yet captured (Task 9
                    # Step 5), confirm misses raise before the OFF half; send a
                    # best-effort OFF so the walk does not leave lights latched.
                    click.echo(f"  toggle failed: {exc}", err=True)
                    if link.local_host_address is not None:
                        try:
                            await link.write_can_frame(
                                relay_command(link.local_host_address, dev.address, False)
                            )
                        except Exception:
                            pass  # link is gone; the gateway drops the session with it
                    continue
                if yes or click.confirm(f"Was that {name}?"):
                    lights.save_light(lights_path(), name, dev.address)
                    click.echo(f"saved: {name} = 0x{dev.address:02X}")
                    return
        click.echo("no device matched", err=True)
        raise SystemExit(1)

    asyncio.run(run())


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

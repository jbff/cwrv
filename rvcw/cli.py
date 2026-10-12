"""rvcw command-line interface."""
from __future__ import annotations

import asyncio

import click

from . import __version__, lights, morse
from .ble.session import RemoteControlSessionError
from .config import lights_path, load_gateway


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """Blink CW (Morse) messages on RV panel lights."""


def _resolve(
    text: str, light: str, wpm: int, repeat: int, dry_run: bool
) -> tuple[list[morse.Step], list[int]]:
    steps = morse.schedule(text, wpm=wpm, repeat=repeat)
    entries = lights.load_map(lights_path()).resolve(light)
    return steps, [e.device_id for e in entries]


@cli.command()
@click.argument("text")
@click.option("--light", required=True, help="light or combo name from lights.toml")
@click.option("--wpm", default=None, type=int, help="default from config.toml")
@click.option("--repeat", default=1, type=int)
@click.option("--dry-run", is_flag=True)
def send(text: str, light: str, wpm: int | None, repeat: int, dry_run: bool) -> None:
    """Blink TEXT as CW on the requested light."""
    if wpm is None:
        wpm = load_gateway().wpm
    try:
        steps, device_ids = _resolve(text, light, wpm, repeat, dry_run)
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
    raise click.ClickException("live send arrives in Task 11")


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


def _parse_device_id(_ctx, _param, value: str) -> int:
    """Accept decimal and 0x-prefixed hex device ids (plain `int` rejects hex)."""
    try:
        return int(value, 0)
    except ValueError:
        raise click.BadParameter(
            f"device id must be an integer like 0x2E41, got {value!r}"
        ) from None


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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

"""rvcw command-line interface."""
from __future__ import annotations

import asyncio  # noqa: F401  (placeholder; see Task 11 for live send)

import click

from . import __version__, lights, morse
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


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

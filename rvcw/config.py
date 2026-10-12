"""Config discovery: ~/.config/rvcw (override with RVCW_HOME)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_GATEWAY_MAC = "10:06:1C:E7:B8:3E"


def home() -> Path:
    return Path(os.environ.get("RVCW_HOME", str(Path.home() / ".config" / "rvcw"))).expanduser()


def lights_path() -> Path:
    return home() / "lights.toml"


def gateway_path() -> Path:
    return home() / "config.toml"


@dataclass(frozen=True)
class GatewayConfig:
    mac: str = DEFAULT_GATEWAY_MAC
    pin: str | None = None
    wpm: int = 8


def load_gateway() -> GatewayConfig:
    p = gateway_path()
    if not p.exists():
        return GatewayConfig()
    with p.open("rb") as f:
        data = tomllib.load(f)
    g = data.get("gateway", {})
    return GatewayConfig(
        mac=g.get("mac", DEFAULT_GATEWAY_MAC),
        pin=g.get("pin"),
        wpm=g.get("wpm", 8),
    )

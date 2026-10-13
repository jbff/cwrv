"""Named lights/combos -> IDS-CAN device addresses, persisted as TOML."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class LightEntry:
    name: str
    device_id: int


@dataclass
class LightMap:
    lights: dict[str, int]
    combos: dict[str, list[str]]
    path: Path

    def _resolve_one(self, name: str) -> LightEntry:
        if name not in self.lights:
            raise KeyError(f"{name!r} not in {self.path}")
        return LightEntry(name, self.lights[name])

    def resolve(self, name: str) -> list[LightEntry]:
        """Resolve a light or combo name to member light entries."""
        if name in self.combos:
            for member in self.combos[name]:
                if member not in self.lights:
                    raise KeyError(
                        f"member {member!r} of combo {name!r} not in {self.path}"
                    )
            return [self._resolve_one(m) for m in self.combos[name]]
        return [self._resolve_one(name)]


def _as_path(arg: Path | LightMap) -> Path:
    # save_light/save_combo hand back a LightMap whose .path points at the
    # file; accept either form wherever a path is expected.
    return arg.path if isinstance(arg, LightMap) else arg


def load_map(path: Path) -> LightMap:
    path = _as_path(path)
    if not path.exists():
        return LightMap({}, {}, path)
    with path.open("rb") as f:
        data = tomllib.load(f)
    # TOML shape is [lights.NAME] device_id = 0x... / [combos.NAME] members = [...],
    # i.e. each entry is a sub-table, not a flat scalar.
    lights = {}
    for k, v in data.get("lights", {}).items():
        lights[str(k)] = int(v["device_id"]) if isinstance(v, dict) else int(v)
    combos = {}
    for k, v in data.get("combos", {}).items():
        members = v["members"] if isinstance(v, dict) else v
        combos[str(k)] = list(members)
    return LightMap(lights, combos, path)


def _check_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid light name {name!r} (want [a-z][a-z0-9_]*)")


def _write(m: LightMap, path: Path) -> None:
    # tomli_w has no unsigned-int hex form: a "0x2E41" value is emitted as a
    # quoted string, which breaks the device_id = 0x... integer round-trip.
    # All names and members passed here are regex-validated, so the document
    # is emitted as raw text with bare keys and simple quoted strings.
    lines: list[str] = []
    for k, v in sorted(m.lights.items()):
        lines.append(f"[lights.{k}]\ndevice_id = 0x{v:04X}\n")
    for k, members in sorted(m.combos.items()):
        quoted = ", ".join(f'"{member}"' for member in members)
        lines.append(f"[combos.{k}]\nmembers = [{quoted}]\n")
    path.write_text("\n".join(lines))


def save_light(path: Path, name: str, device_id: int) -> LightMap:
    path = _as_path(path)
    _check_name(name)
    m = load_map(path)
    m.lights[name] = device_id
    _write(m, path)
    return load_map(path)


def save_combo(path: Path, name: str, members: list[str]) -> None:
    path = _as_path(path)
    _check_name(name)
    for member in members:
        _check_name(member)
    m = load_map(path)
    m.combos[name] = list(members)
    _write(m, path)

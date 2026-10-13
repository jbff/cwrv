"""Named lights/combos -> (table, device) relay addresses, persisted as TOML."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class LightTarget:
    """One addressable data-path relay: table_id + device_id (COBS path)."""

    table_id: int
    device_id: int

    @property
    def key(self) -> tuple[int, int]:
        """The (table_id, device_id) pair keying GatewayLink.relay_states."""
        return (self.table_id, self.device_id)


@dataclass(frozen=True)
class LightEntry:
    name: str
    table_id: int
    device_id: int


@dataclass
class LightMap:
    lights: dict[str, LightEntry]
    combos: dict[str, list[str]]
    path: Path

    def _resolve_one(self, name: str) -> LightEntry:
        if name not in self.lights:
            raise KeyError(f"{name!r} not in {self.path}")
        return self.lights[name]

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


def _removed_key_error(name: str) -> ValueError:
    return ValueError(
        f"light '{name}' uses the removed device_id key — re-run learn for "
        "the COBS path"
    )


def load_map(path: Path) -> LightMap:
    path = _as_path(path)
    if not path.exists():
        return LightMap({}, {}, path)
    with path.open("rb") as f:
        data = tomllib.load(f)
    # TOML shape is [lights.NAME] table = 0x… / device = 0x… and
    # [combos.NAME] members = [...], i.e. each entry is a sub-table.
    # The pre-COBS device_id key is REMOVED: load raises instead of remapping.
    lights: dict[str, LightEntry] = {}
    for k, v in data.get("lights", {}).items():
        name = str(k)
        if not isinstance(v, dict) or "device_id" in v:
            raise _removed_key_error(name)
        if "table" not in v or "device" not in v:
            raise ValueError(
                f"light '{name}' needs both a 'table' and a 'device' entry "
                f"in {path}"
            )
        _check_device_id(name, int(v["device"]))
        lights[name] = LightEntry(name, int(v["table"]), int(v["device"]))
    combos = {}
    for k, v in data.get("combos", {}).items():
        members = v["members"] if isinstance(v, dict) else v
        combos[str(k)] = list(members)
    return LightMap(lights, combos, path)


def _check_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid light name {name!r} (want [a-z][a-z0-9_]*)")


# The vendored build_action_switch (and the wire) carry a device id in ONE
# byte (`d & 0xFF`), and RelayStatus confirms arrive keyed by that byte: a
# mapping stored with device_id > 0xFF would silently command the WRONG
# relay instead of failing. Reject at load and at save.
_MAX_DEVICE_ID = 0xFF


def _check_device_id(name: str, device_id: int) -> None:
    if not 0 <= device_id <= _MAX_DEVICE_ID:
        raise ValueError(
            f"light '{name}': device id 0x{device_id:X} exceeds one byte "
            f"(max 0x{_MAX_DEVICE_ID:02X}) — check the light's mapping"
        )


def _write(m: LightMap, path: Path) -> None:
    # tomli_w has no unsigned-int hex form: a "0x2E" value is emitted as a
    # quoted string, which breaks the table/device = 0x... integer round-trip.
    # All names and members passed here are regex-validated, so the document
    # is emitted as raw text with bare keys and simple quoted strings.
    lines: list[str] = []
    for k, entry in sorted(m.lights.items()):
        lines.append(
            f"[lights.{k}]\ntable = 0x{entry.table_id:02X}\n"
            f"device = 0x{entry.device_id:04X}\n"
        )
    for k, members in sorted(m.combos.items()):
        quoted = ", ".join(f'"{member}"' for member in members)
        lines.append(f"[combos.{k}]\nmembers = [{quoted}]\n")
    path.write_text("\n".join(lines))


def save_light(path: Path, name: str, table_id: int, device_id: int) -> LightMap:
    path = _as_path(path)
    _check_name(name)
    _check_device_id(name, device_id)
    m = load_map(path)
    m.lights[name] = LightEntry(name, table_id, device_id)
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

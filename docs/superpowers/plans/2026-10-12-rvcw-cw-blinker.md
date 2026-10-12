# rvcw CW Blinker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `rvcw`, a standalone CLI that blinks CW/Morse messages on RV panel relay lights over IDS-CAN BLE, multiplexing the connection with HA's integration.

**Architecture:** Five-layer Python package — pure Morse engine and TOML light mapping (no I/O), a Bleak-based gateway link, key/seed + LocalHost + REMOTE_CONTROL session layer (adapted verbatim API from ha-onecontrol extracts), an executor that walks clock-scheduled steps supervised by DEVICE_STATUS confirmations, and a click CLI. Vendored `protocol/` files come straight from the installed integration (MIT).

**Tech Stack:** Python ≥3.12, uv, bleak (BlueZ D-Bus), click, pytest, tomllib + tomli-w.

**Spec:** `docs/superpowers/specs/2026-10-12-cw-blinker-design.md` (read it — this plan argues from it; the extracts that informed it are summarized in its "Reality notes" section).

## Global Constraints

- Python `>=3.12`; project managed with `uv`; entry point `rvcw = "rvcw.cli:cli"`.
- Dependencies exactly: `bleak`, `click`, `tomli-w`; dev: `pytest`. No HA imports anywhere.
- Vendored protocol files are byte-identical to the container source except import/attribute lines needed to live in `rvcw/protocol/`; every vendored file keeps upstream's MIT credit via `rvcw/protocol/NOTICE.md` and never carries secrets.
- BLE writes to CAN_WRITE use `response=False`; MTU 185 best-effort (`hasattr(client, "request_mtu")` guard, as upstream does).
- REMOTE_CONTROL session heartbeat: 0.5 s while a session is open. Never close an RC session immediately after relay-command bursts (upstream 1.0.48 gotcha: devices later reject commands, RESPONSE 0x0E) — close only via `rvcw session --end` or link teardown.
- Our gateway (V1/24955) requires the explicit REMOTE_CONTROL seed/key exchange; treat seed-request timeout as an error, not a silent fallback.
- No secrets in the repo: gateway MAC + PIN live in `~/.config/rvcw/config.toml` (mode 600). Default MAC override: `10:06:1C:E7:B8:3E`.
- Commits: one or more per task, message imperative, ending `Co-Authored-By: Claude Code <noreply@anthropic.com>`.
- Tests run with `uv run pytest`. Hardware steps (marked **[HW]**) require the panel reachable; never run hardware steps without the user present.

## Review Focus

Inputs/failures the spec implies but no task's unit tests fully exercise — the executor's tests cover 2–5 synthetically; 1 needs the hardware checklist:

1. **SIGINT/connection loss mid-message** → every touched light restored to pre-send state in all termination paths. [HW check in Task 11: interrupt `selftest` with Ctrl-C, verify light returns to prior state.]
2. **Two consecutive missed state confirmations** → abort + restore, with exactly one retry per element. (Executor test.)
3. **WPM above the measured RTT envelope** → downscale with a warning, never garble timing. (Executor test on the RTT math.)
4. **Unknown `--light` name or unresolvable combo member** → fail fast naming the key and the TOML path. (Lights/CLI tests.)
5. **Empty/whitespace text or characters with no Morse** → `ValueError` naming the character; empty text refused with "nothing to send". (Morse tests.)
6. **Missing gateway config (no PIN) when the gate reports locked** → fail fast with the config path in the message. (Task 6 test.)

---

### Task 1: Scaffold + vendored protocol layer

**Files:**
- Create: `pyproject.toml`, `rvcw/__init__.py`, `rvcw/protocol/__init__.py`, `rvcw/protocol/NOTICE.md`
- Create (copied from container): `rvcw/protocol/commands.py`, `events.py`, `cobs.py`, `crc8.py`, `tea.py`, `ids_can_wire.py`, `function_names.py`, `const.py`
- Test: `tests/test_vendor.py`

**Interfaces:**
- Consumes: nothing.
- Produces: importable `rvcw.protocol` exposing `cobs_encode`, `CobsByteDecoder`, `crc8`, `tea_encrypt`, `calculate_step1_key`, `calculate_step2_key`, `calculate_can_ble_key_seed_key`, `CAN_BLE_KEY_SEED_CIPHER`, `X180T_KEY_SEED_CIPHER`, `compose_ids_can_extended_wire_frame`, `compose_ids_can_standard_wire_frame`, `parse_ids_can_wire_frame`, `IdsCanWireFrame`, `FUNCTION_NAMES`, and UUID constants (`CAN_WRITE_CHAR_UUID` = `00000001-0200-a58e-e411-afe28044e62c`, `CAN_READ_CHAR_UUID` = `00000002-…`, `CAN_VERSION_CHAR_UUID` = `00000004-…`, `PASSWORD_UNLOCK_CHAR_UUID` = `00000005-…`, `UNLOCK_STATUS_CHAR_UUID` = `00000012-…`, `KEY_CHAR_UUID` = `00000013-…`, `SEED_CHAR_UUID` = `00000011-…`, `DATA_WRITE_CHAR_UUID` = `00000033-…`, `DATA_READ_CHAR_UUID` = `00000034-…`).

- [ ] **Step 1: Write pyproject and package skeleton**

```toml
# pyproject.toml
[project]
name = "rvcw"
version = "0.1.0"
description = "CW (Morse) blinker for RV panel lights over IDS-CAN BLE"
requires-python = ">=3.12"
dependencies = ["bleak>=0.22,<2", "click>=8.1,<9", "tomli-w>=1,<2"]

[dependency-groups]
dev = ["pytest>=8"]

[project.scripts]
rvcw = "rvcw.cli:cli"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["rvcw"]
```

```python
# rvcw/__init__.py
"""CW (Morse) blinker for RV panel lights over IDS-CAN BLE."""
__version__ = "0.1.0"
```

```markdown
# rvcw/protocol/NOTICE.md
Files in this directory are vendored from https://github.com/phurth/ha-onecontrol
(v1.0.49), MIT License — Copyright (c) phurth and contributors.
Modifications limited to import/attribute lines needed outside Home Assistant.
```

- [ ] **Step 2: Create `rvcw/protocol/__init__.py` re-exporting the API**

```python
# rvcw/protocol/__init__.py
"""Vendored pure-Python protocol layer from phurth/ha-onecontrol (MIT)."""
from .cobs import CobsByteDecoder, cobs_encode
from .crc8 import crc8
from .tea import (
    X180T_KEY_SEED_CIPHER,
    calculate_can_ble_key_seed_key,
    calculate_step1_key,
    calculate_step2_key,
    tea_encrypt,
)
from .ids_can_wire import (
    IdsCanWireFrame,
    compose_ids_can_extended_wire_frame,
    compose_ids_can_standard_wire_frame,
    parse_ids_can_wire_frame,
)
from .function_names import FUNCTION_NAMES
from .const import (  # noqa: F401
    CAN_READ_CHAR_UUID,
    CAN_VERSION_CHAR_UUID,
    CAN_WRITE_CHAR_UUID,
    DATA_READ_CHAR_UUID,
    DATA_WRITE_CHAR_UUID,
    KEY_CHAR_UUID,
    PASSWORD_UNLOCK_CHAR_UUID,
    SEED_CHAR_UUID,
    UNLOCK_STATUS_CHAR_UUID,
)
```

- [ ] **Step 3: Copy the vendored files from the container**

```bash
mkdir -p rvcw/protocol
for f in commands.py events.py cobs.py crc8.py tea.py ids_can_wire.py function_names.py const.py; do
  incus exec has -- cat /var/lib/homeassistant/custom_components/ha_onecontrol/protocol/$f > rvcw/protocol/$f
done
```

- [ ] **Step 4: Write the vendor smoke test**

```python
# tests/test_vendor.py
import pytest

from rvcw.protocol import (
    CAN_WRITE_CHAR_UUID,
    compose_ids_can_extended_wire_frame,
    crc8,
    cobs_encode,
    parse_ids_can_wire_frame,
    tea_encrypt,
)


def test_crc8_init_value():
    assert crc8(b"") == 0x55


def test_cobs_encode_wraps_payload():
    encoded = cobs_encode(b"\x01\x02")
    assert encoded[0] == 0x00 and encoded[-1] == 0x00  # frame delimiters
    assert len(encoded) > 2


def test_extended_frame_roundtrip():
    frame = compose_ids_can_extended_wire_frame(0x82, 0x2E, 0x41, 0x01, b"")
    assert frame[0] == 0  # dlc
    wire = parse_ids_can_wire_frame(frame)
    assert wire.message_type == 0x82
    assert wire.source_address == 0x2E
    assert wire.target_address == 0x41
    assert wire.message_data == 0x01


def test_tea_encrypt_is_deterministic():
    assert tea_encrypt(0x9E3779B9, 0) == tea_encrypt(0x9E3779B9, 0)


def test_can_write_uuid():
    assert CAN_WRITE_CHAR_UUID == "00000001-0200-a58e-e411-afe28044e62c"
```

- [ ] **Step 5: Create the package and run the test**

Run: `uv sync && uv run pytest tests/test_vendor.py -v`
Expected: PASS (5 tests). If imports fail on HA-coupled lines, adjust only imports in the vendored file and note it in the commit message.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock rvcw/ tests/
git commit -m "feat: scaffold rvcw and vendor ha-onecontrol protocol layer

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

---

### Task 2: Morse engine

**Files:**
- Create: `rvcw/morse.py`
- Test: `tests/test_morse.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Step(on: bool, ms: int)` (frozen dataclass); `dit_ms(wpm: int) -> int` (`round(1200 / wpm)`); `schedule(text: str, wpm: int = 8, repeat: int = 1) -> list[Step]`. Invariants: steps alternate polarity; no zero-length steps; no trailing off-step (executor guarantees restore); adjacent same-polarity steps merged; `repeat >= 1` else `ValueError`; prosigns as `<AR>`, `<SK>`, `<BT>`, `<AS>` tokens; unknown characters and empty/whitespace text raise `ValueError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_morse.py
import pytest

from rvcw.morse import Step, schedule


def test_single_dit_e():
    assert schedule("E", 8) == [Step(True, 150)]


def test_letter_s():
    assert schedule("S", 8) == [
        Step(True, 150), Step(False, 150), Step(True, 150),
        Step(False, 150), Step(True, 150),
    ]


def test_letter_a():
    assert schedule("A", 8) == [Step(True, 150), Step(False, 150), Step(True, 450)]


def test_word_gap():
    assert schedule("A B", 8) == [
        Step(True, 150), Step(False, 150), Step(True, 450),
        Step(False, 1050),
        Step(True, 450), Step(False, 150), Step(True, 150),
        Step(False, 150), Step(True, 150),
    ]


def test_prosign_brackets():
    assert schedule("<AR>", 8) == [
        Step(True, 150), Step(False, 150), Step(True, 450),
        Step(False, 150), Step(True, 450), Step(False, 150), Step(True, 150),
    ]


def test_lowercase_normalized():
    assert schedule("e", 8) == schedule("E", 8)


def test_repeat_merged():
    assert schedule("E", 8, repeat=2) == [
        Step(True, 150), Step(False, 1050), Step(True, 150),
    ]


def test_unknown_char_raises():
    with pytest.raises(ValueError, match="no Morse"):
        schedule("H!")


def test_empty_raises():
    with pytest.raises(ValueError, match="nothing to send"):
        schedule("   ")


def test_bad_wpm_raises():
    with pytest.raises(ValueError):
        schedule("E", 0)
    with pytest.raises(ValueError):
        schedule("E", 8, repeat=0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_morse.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rvcw.morse'`

- [ ] **Step 3: Implement `rvcw/morse.py`**

```python
# rvcw/morse.py
"""Pure Morse engine: text -> on/off schedule in milliseconds. No I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Standard ITU: dit=1, dah=3, intra-char gap=1, inter-char gap=3, word gap=7.
# Unit length = 1200 / WPM ms (PARIS standard).

@dataclass(frozen=True)
class Step:
    on: bool
    ms: int


MORSE: dict[str, str] = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "-": "-....-",
    "=": "-...-", ":": "---...", "'": ".----.", '"': ".-..-.",
    "(": "-.--.", ")": "-.--.-", ";": "-.-.-.", "_": "..--.-", "+": ".-.-.",
}

PROSIGNS: dict[str, str] = {
    "AR": ".-.-.", "SK": "...-.-", "BT": "-...-", "AS": ".-...",
}

_PROSIGN_RE = re.compile(r"<(AR|SK|BT|AS)>")


def dit_ms(wpm: int) -> int:
    if wpm <= 0:
        raise ValueError(f"wpm must be positive, got {wpm}")
    return round(1200 / wpm)


def _pattern_for(token: str) -> str:
    """Morse pattern for one word token; whole-word '<XX>' is a prosign."""
    t = token.upper()
    m = _PROSIGN_RE.fullmatch(t)
    if m:
        return PROSIGNS[m.group(1)]
    out: list[str] = []
    for ch in t:
        if ch in MORSE:
            out.append(MORSE[ch])
        else:
            raise ValueError(f"no Morse for character {ch!r}")
    return "".join(out)


def _units(text: str) -> list[tuple[bool, int]]:
    """Convert text to raw (on, units) steps; no trailing word gap."""
    units: list[tuple[bool, int]] = []
    for wi, word in enumerate(text.split()):
        for ci, char in enumerate(word):
            pattern = _pattern_for(char)
            for si, sym in enumerate(pattern):
                units.append((True, 3 if sym == "-" else 1))
                if si < len(pattern) - 1:
                    units.append((False, 1))
            if ci < len(word) - 1:
                units.append((False, 3))
        units.append((False, 7))  # word gap (removed after the final word)
    if units and units[-1] == (False, 7):
        units.pop()
    return units


def schedule(text: str, wpm: int = 8, repeat: int = 1) -> list[Step]:
    if not text.split():
        raise ValueError("nothing to send (empty/whitespace text)")
    if repeat < 1:
        raise ValueError(f"repeat must be >= 1, got {repeat}")
    dit = dit_ms(wpm)
    words_units = _units(text)
    steps: list[Step] = []
    for r in range(repeat):
        if r:
            steps.append(Step(False, 7 * dit))
        steps.extend(Step(on, ms * dit) for (on, ms) in words_units)
    merged: list[Step] = []
    for s in steps:
        if merged and merged[-1].on == s.on:
            merged[-1] = Step(s.on, merged[-1].ms + s.ms)
        else:
            merged.append(Step(s.on, s.ms))
    if merged and not merged[-1].on:
        merged.pop()  # no trailing off-step; the executor guarantees restore
    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_morse.py -v`
Expected: PASS (10 tests). If `_units` needs the trailing-pop adjusted for a multi-word case, re-check the invariants in the docstring before changing tests.

- [ ] **Step 5: Commit**

```bash
git add rvcw/morse.py tests/test_morse.py
git commit -m "feat: pure morse engine with golden-timing tests

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

### Task 3: Light mapping (TOML)

**Files:**
- Create: `rvcw/lights.py`
- Test: `tests/test_lights.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `LightEntry(name: str, device_id: int)`; `LightMap(lights: dict[str, int], combos: dict[str, list[str]], path: Path)` with `resolve(name: str) -> list[LightEntry]` (combos resolve recursively one level: members are light names; unknown name/member raises `KeyError` naming key AND file path); `load_map(path: Path) -> LightMap` (missing file → empty map); `save_light(path: Path, name: str, device_id: int) -> LightMap`; `save_combo(path: Path, name: str, members: list[str]) -> None`. TOML shape: `[lights.NAME] device_id = 0x…` / `[combos.NAME] members = […, …]`; names match `[a-z][a-z0-9_]*` else `ValueError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lights.py
from pathlib import Path

import pytest

from rvcw.lights import LightMap, load_map, save_combo, save_light


def write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "lights.toml"
    p.write_text(content)
    return p


def test_load_and_resolve_light(tmp_path):
    from rvcw.lights import LightEntry

    p = write(tmp_path, "[lights.awning]\ndevice_id = 0x2E41\n")
    m = load_map(p)
    assert m.resolve("awning") == [LightEntry("awning", 0x2E41)]


def test_missing_file_is_empty(tmp_path):
    m = load_map(tmp_path / "nope.toml")
    with pytest.raises(KeyError, match="awning"):
        m.resolve("awning")


def test_combo_resolution(tmp_path):
    p = write(
        tmp_path,
        "[lights.awning]\ndevice_id = 0x2E41\n[lights.spots]\ndevice_id = 0x2E42\n"
        "[combos.night_mode]\nmembers = [\"awning\", \"spots\"]\n",
    )
    m = load_map(p)
    assert [e.device_id for e in m.resolve("night_mode")] == [0x2E41, 0x2E42]


def test_combo_missing_member_raises(tmp_path):
    p = write(tmp_path, "[combos.bad]\nmembers = [\"ghost\"]\n")
    m = load_map(p)
    with pytest.raises(KeyError, match="ghost"):
        m.resolve("bad")


def test_save_light_roundtrip(tmp_path):
    p = save_light(tmp_path / "lights.toml", "awning", 0x2E41)
    reloaded = load_map(p)
    assert reloaded.resolve("awning")[0].device_id == 0x2E41


def test_save_combo_roundtrip(tmp_path):
    p = save_light(tmp_path / "lights.toml", "awning", 0x2E41)
    p = save_light(p, "spots", 0x2E42)
    save_combo(p, "night_mode", ["awning", "spots"])
    m = load_map(p)
    assert [e.name for e in m.resolve("night_mode")] == ["awning", "spots"]


def test_bad_name_raises(tmp_path):
    with pytest.raises(ValueError, match="name"):
        save_light(tmp_path / "l.toml", "Bad Name", 0x2E41)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_lights.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rvcw.lights'`

- [ ] **Step 3: Implement `rvcw/lights.py`**

```python
# rvcw/lights.py
"""Named lights/combos -> IDS-CAN device addresses, persisted as TOML."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

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


def load_map(path: Path) -> LightMap:
    if not path.exists():
        return LightMap({}, {}, path)
    with path.open("rb") as f:
        data = tomllib.load(f)
    lights = {str(k): int(v) for k, v in data.get("lights", {}).items()}
    combos = {str(k): list(v) for k, v in data.get("combos", {}).items()}
    return LightMap(lights, combos, path)


def _check_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid light name {name!r} (want [a-z][a-z0-9_]*)")


def _write(m: LightMap, path: Path) -> None:
    doc: dict = {}
    if m.lights:
        doc["lights"] = {k: f"0x{v:04X}" for k, v in sorted(m.lights.items())}
    if m.combos:
        doc["combos"] = {k: list(v) for k, v in sorted(m.combos.items())}
    path.write_text(tomli_w.dumps(doc))


def save_light(path: Path, name: str, device_id: int) -> LightMap:
    _check_name(name)
    m = load_map(path)
    m.lights[name] = device_id
    _write(m, path)
    return load_map(path)


def save_combo(path: Path, name: str, members: list[str]) -> None:
    _check_name(name)
    for member in members:
        _check_name(member)
    m = load_map(path)
    m.combos[name] = list(members)
    _write(m, path)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_lights.py -v`
Expected: PASS (7 tests). `tomli_w.dumps` renders dict values as typed TOML — if the hex form comes out as a string on save and round-trip tests fail, format the mapping section as raw text inside `_write` (keeping `0x…` integer form) and say so in the commit.

- [ ] **Step 5: Commit**

```bash
git add rvcw/lights.py tests/test_lights.py
git commit -m "feat: TOML light/combo mapping with fail-fast resolution

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

---

### Task 4: Config + CLI skeleton with dry-run send

**Files:**
- Create: `rvcw/config.py`, `rvcw/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `morse.schedule`, `lights.load_map/resolve`, `Step`.
- Produces: `config.home() -> Path` (env `RVCW_HOME` overrides, default `~/.config/rvcw`); `config.lights_path() -> Path`; `config.gateway_path() -> Path`; `config.GatewayConfig(mac: str, pin: str | None, wpm: int)`; `config.load_gateway() -> GatewayConfig` (defaults mac `10:06:1C:E7:B8:3E`, wpm 8, pin None). CLI group `cli` with `send`, `lights`, `discover`, `learn`, `probe-session`, `selftest`, `session` — in this task only `send` (with `--dry-run`) and `lights` are functional; the rest are added by later tasks.

- [ ] **Step 1: Write `rvcw/config.py`**

```python
# rvcw/config.py
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
```

- [ ] **Step 2: Write the failing CLI tests**

```python
# tests/test_cli.py
import pytest
from click.testing import CliRunner


@pytest.fixture()
def rvcw_home(tmp_path, monkeypatch):
    home = tmp_path / "rvcw"
    home.mkdir()
    monkeypatch.setenv("RVCW_HOME", str(home))
    (home / "lights.toml").write_text("[lights.awning]\ndevice_id = 0x2E41\n")
    return home


def test_send_dry_run_lists_schedule(rvcw_home):
    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["send", "E", "--light", "awning", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "0x2E41" in result.output
    assert "ON    150ms" in result.output


def test_send_unknown_light_fails_fast(rvcw_home):
    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["send", "E", "--light", "ghost", "--dry-run"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_lights_list(rvcw_home):
    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["lights"])
    assert result.exit_code == 0
    assert "awning" in result.output and "0x2E41" in result.output
```

- [ ] **Step 3: Run to verify failure, then implement `rvcw/cli.py`**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — no module `rvcw.cli`.

```python
# rvcw/cli.py
"""rvcw command-line interface."""
from __future__ import annotations

import asyncio

import click

from . import (__version__,  # noqa: F401  (placeholder; see Task 11 for live send)
               lights, morse)
from .config import load_gateway, lights_path


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """Blink CW (Morse) messages on RV panel lights."""


def _resolve(text: str, light: str, wpm: int, repeat: int, dry_run: bool) -> tuple[
    list[morse.Step], list[int]
]:
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
    steps, device_ids = _resolve(text, light, wpm, repeat, dry_run)
    if dry_run:
        click.echo(
            f"light={light} devices={[f'0x{d:04X}' for d in device_ids]} "
            f"wpm={wpm} steps={len(steps)} duration={sum(s.ms for s in steps)}ms"
        )
        for s in steps:
            click.echo(f"{'ON ' if s.on else 'off'} {s.ms:6d}ms")
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
```

Fix the import line to plain imports:

```python
from . import __version__, lights, morse
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv sync && uv run pytest tests/ -v`
Expected: PASS (all previous + 3). The `ON    150ms` format comes from `f"{'ON ' if s.on else 'off'} {s.ms:6d}ms"`.

- [ ] **Step 5: Smoke the console script**

Run: `uv run rvcw --help && uv run rvcw send E --light awning --dry-run` (only if `~/.config/rvcw/lights.toml` exists; otherwise expect the fail-fast KeyError naming the file — either is correct)
Expected: help shows `send`, `lights`; dry-run prints devices/steps.

- [ ] **Step 6: Commit**

```bash
git add rvcw/config.py rvcw/cli.py tests/test_cli.py
git commit -m "feat: config paths and CLI skeleton (dry-run send, lights list)

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

### Task 5: GatewayLink transport (connect, write, notifications)

**Files:**
- Create: `rvcw/ble/__init__.py`, `rvcw/ble/transport.py`
- Test: `tests/test_transport.py`

**Interfaces:**
- Consumes: vendored `protocol` (UUIDs, `parse_ids_can_wire_frame`, `IdsCanWireFrame`).
- Produces: `decode_notification(raw: bytes) -> list[IdsCanWireFrame]` (V2 BLE notification decode — Packed/ElevenBit/TwentyNineBit — with non-V2 bytes passed to the wire parser); `class GatewayLink`:
  - `GatewayLink(mac: str, pin: str | None = None)`
  - `client` property → wrapped `BleakClient` (raises `LinkError` if not connected)
  - `connect() -> None` (connect, MTU 185 best-effort, subscribe CAN_READ notify)
  - `write_can_frame(frame: bytes) -> None` (raw to CAN_WRITE `00000001-…`, `response=False`)
  - `relay_states: dict[int, bool]`, `local_host_address: int | None`, `frames_received: int`
  - `on_frame: Callable[[IdsCanWireFrame], None] | None` — single subscriber hook used by the session layer (Task 8); DEVICE_STATUS (0x03) frames update `relay_states` via `relay_state_from_status` (Task 9 provides it; until then returns None and states are untouched)
  - `close() -> None`
  - `LinkError(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_transport.py
import pytest

from rvcw.ble.transport import GatewayLink, LinkError, decode_notification


def test_v2_packed_notification_yields_four_frames():
    # 1 byte type + 18 header bytes + status data, per the V2 Packed layout
    raw = bytes([0x01, 0x41]) + b"\x00" + b"\x12" + b"\x00\x2E" + b"\x00\x00" * 5
    raw = raw[:19] + b"\x01"  # status_len=1 placeholder at index 18
    frames = decode_notification(raw)
    assert len(frames) == 4


def test_v2_twentynine_bit_notification_yields_one_frame():
    id4 = bytes.fromhex("80BA4101")  # 29-bit COMMAND frame id (0x82, 0x2E, 0x41, 0x01)
    raw = bytes([0x03]) + id4 + bytes([0x00])
    frames = decode_notification(raw)
    assert len(frames) == 1
    assert frames[0].message_type == 0x82
    assert frames[0].source_address == 0x2E
    assert frames[0].target_address == 0x41


def test_non_v2_bytes_fall_back_to_wire_parse():
    raw = bytes([0x0F, 0x00, 0x01, 0x00, 0x12, 0x00, 0x00, 0x00, 0x00])
    frames = decode_notification(raw)
    assert len(frames) == 1


def test_unconnected_write_raises():
    link = GatewayLink("10:06:1C:E7:B8:3E")
    with pytest.raises(LinkError):
        link.client
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_transport.py -v`
Expected: FAIL — no module `rvcw.ble.transport`

- [ ] **Step 3: Implement transport**

```python
# rvcw/ble/__init__.py
"""BLE layer: link, unlock, CAN identity, REMOTE_CONTROL sessions."""
```

```python
# rvcw/ble/transport.py
"""Bleak-based gateway link over the host's BlueZ (D-Bus multiplexed)."""
from __future__ import annotations

import asyncio
from typing import Callable

from bleak import BleakClient

from rvcw.protocol import (
    CAN_READ_CHAR_UUID,
    CAN_WRITE_CHAR_UUID,
    IdsCanWireFrame,
    parse_ids_can_wire_frame,
)


class LinkError(RuntimeError):
    pass


def decode_notification(raw: bytes) -> list[IdsCanWireFrame]:
    """Decode one CAN_READ notification into wire frames.

    V2 BLE notification types (byte 0): 1=Packed, 2=ElevenBit, 3=TwentyNineBit.
    Anything else is treated as a raw IDS wire frame.
    """
    raw = bytes(raw)
    if not raw or raw[0] not in (0x01, 0x02, 0x03):
        frame = parse_ids_can_wire_frame(raw)
        return [frame] if frame else []
    v2 = raw[0]
    if v2 == 0x01:  # Packed: one device advertisement -> 4 synthetic frames
        if len(raw) < 19:
            return []
        (device_addr, network_status, ids_can_version) = raw[1], raw[2], raw[3]
        mac = bytes(raw[4:10])
        product_id = bytes(raw[10:12])
        product_instance, device_type = raw[12], raw[13]
        function_name = bytes(raw[14:16])
        instance_byte, device_caps = raw[16], raw[17]
        status_len = raw[18]
        status_data = bytes(raw[19 : 19 + status_len])
        candidates: list[bytes] = [
            bytes([8, 0x00, device_addr, network_status, ids_can_version]) + mac,
            bytes([8, 0x02, device_addr]) + product_id
            + bytes([product_instance, device_type])
            + function_name
            + bytes([instance_byte, device_caps]),
            bytes([status_len, 0x03, device_addr]) + status_data,
            bytes([4, 0x01, device_addr, 0, 0, 0, 0]),
        ]
        out = [f for f in map(parse_ids_can_wire_frame, candidates) if f]
        return out
    if v2 == 0x02:  # ElevenBit
        if len(raw) < 6:
            return []
        msg_type_byte, device_addr, dlc = raw[3], raw[4], raw[5]
        data = bytes(raw[6 : 6 + dlc])
        frame = parse_ids_can_wire_frame(bytes([dlc, msg_type_byte, device_addr]) + data)
        return [frame] if frame else []
    # v2 == 0x03: TwentyNineBit
    if len(raw) < 6:
        return []
    dlc, id4 = raw[5], bytes(raw[1:5])
    data = bytes(raw[6 : 6 + dlc])
    frame = parse_ids_can_wire_frame(bytes([dlc]) + id4 + data)
    return [frame] if frame else []


class GatewayLink:
    """One multiplexed participant on the gateway's GATT connection."""

    def __init__(self, mac: str, pin: str | None = None) -> None:
        self.mac = mac
        self.pin = pin
        self._client: BleakClient | None = None
        self.on_frame: Callable[[IdsCanWireFrame], None] | None = None
        self.relay_states: dict[int, bool] = {}
        self.local_host_address: int | None = None
        self.frames_received = 0

    @property
    def client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise LinkError("gateway link is not connected")
        return self._client

    async def connect(self) -> None:
        client = BleakClient(self.mac)
        await client.connect()
        try:
            if hasattr(client, "request_mtu"):
                await client.request_mtu(185)  # best-effort, as upstream does
        except Exception:
            pass
        self._client = client
        await client.start_notify(
            CAN_READ_CHAR_UUID,
            lambda _c, data: self._on_notification(bytes(data)),
        )

    def _on_notification(self, data: bytes) -> None:
        for frame in decode_notification(data):
            self.frames_received += 1
            if frame.message_type == 0x03:  # DEVICE_STATUS
                state = self.relay_state_from_status(frame.payload)
                if state is not None:
                    self.relay_states[frame.source_address] = state
            if self.on_frame is not None:
                self.on_frame(frame)

    @staticmethod
    def relay_state_from_status(payload: bytes) -> bool | None:
        """DEVICE_STATUS -> relay on/off. Derived from live data (Task 9); None until then."""
        return None

    async def write_can_frame(self, frame: bytes) -> None:
        """Write a raw IDS wire frame ([dlc][id][payload]) to CAN_WRITE."""
        if self._client is None:
            raise LinkError("gateway link is not connected")
        await self._client.write_gatt_char(CAN_WRITE_CHAR_UUID, frame, response=False)

    async def close(self) -> None:
        if self._client is not None:
            self.on_frame = None
            try:
                await self._client.disconnect()
            finally:
                self._client = None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_transport.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add rvcw/ble/ tests/test_transport.py
git commit -m "feat: GatewayLink transport with V2 notification decoding

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

---

### Task 6: Gateway unlock (key/seed + PIN gate)

**Files:**
- Create: `rvcw/ble/session.py`
- Test: `tests/test_unlock.py`

**Interfaces:**
- Consumes: `GatewayLink` (client, pin), vendored `calculate_can_ble_key_seed_key`, `CAN_BLE_KEY_SEED_CIPHER`, UUIDs.
- Produces: `class GatewayUnlockError(RuntimeError)`; `async def unlock_gateway(link: GatewayLink) -> None` — idempotent; raises `GatewayUnlockError(f"password gate locked and no pin configured — put gateway.pin in {config.gateway_path().as_posix()}")` when the gate is locked and `link.pin` is None. `link` must also surface the raw client via `.client`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_unlock.py
import asyncio

import pytest

from rvcw.ble.session import GatewayUnlockError, unlock_gateway
from rvcw.protocol import calculate_can_ble_key_seed_key, CAN_BLE_KEY_SEED_CIPHER


class FakeChar:
    def __init__(self, value: bytes):
        self.value = bytearray(value)


class FakeClient:
    """Records writes; serves canned reads. Chars keyed by full UUID prefix."""

    def __init__(self) -> None:
        self.chars: dict[str, FakeChar] = {}
        self.is_connected = True
        self.written: list[tuple[str, bytes, bool]] = []

    def add(self, prefix: str, value: bytes) -> None:
        self.chars[f"{prefix}-0200-a58e-e411-afe28044e62c"] = FakeChar(value)

    async def read_gatt_char(self, uuid: str) -> bytearray:
        return self.chars[uuid].value

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        self.written.append((uuid, bytes(data), response))
        if uuid.startswith("00000013"):  # KEY write unlocks UNLOCK_STATUS
            self.chars["00000012-0200-a58e-e411-afe28044e62c"].value = bytearray(b"unlocked")
        if uuid.startswith("00000005"):  # PIN write unlocks the gate
            self.chars["00000005-0200-a58e-e411-afe28044e62c"].value = bytearray(b"\x01")


class FakeLink:
    def __init__(self, client: FakeClient, pin: str | None) -> None:
        self._client = client
        self.pin = pin

    @property
    def client(self):
        return self._client


@pytest.fixture()
def locked_gateway():
    client = FakeClient()
    seed = bytes.fromhex("1A2B3C4D")
    client.add("00000012", seed)          # UNLOCK_STATUS
    client.add("00000005", b"\x00")       # PASSWORD_UNLOCK (locked)
    client.written = []
    return client, seed


async def test_unlock_happy_path(locked_gateway):
    client, seed = locked_gateway
    expected_key = calculate_can_ble_key_seed_key(seed, CAN_BLE_KEY_SEED_CIPHER)
    await unlock_gateway(FakeLink(client, pin="123456"))
    key_writes = [w for w in client.written if w[0].startswith("00000013")]
    pin_writes = [w for w in client.written if w[0].startswith("00000005")]
    assert key_writes and key_writes[0][1] == expected_key and key_writes[0][2] is True
    assert pin_writes and pin_writes[0][1] == b"123456"


async def test_already_unlocked_is_idempotent(locked_gateway):
    client, _ = locked_gateway
    client.chars["00000012-0200-a58e-e411-afe28044e62c"].value = bytearray(b"unlocked")
    await unlock_gateway(FakeLink(client, pin=None))
    assert not [w for w in client.written if w[0].startswith("00000013")]


async def test_locked_gate_without_pin_raises(locked_gateway):
    client, _ = locked_gateway
    with pytest.raises(GatewayUnlockError, match="no pin configured"):
        await unlock_gateway(FakeLink(client, pin=None))


async def test_bad_key_verify_raises(locked_gateway):
    client, _ = locked_gateway

    # Sabotage: KEY writes do not unlock UNLOCK_STATUS
    async def noop(uuid, data, response=False):
        client.written.append((uuid, bytes(data), response))

    client.write_gatt_char = noop
    with pytest.raises(GatewayUnlockError):
        await unlock_gateway(FakeLink(client, pin="654321"))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_unlock.py -v`
Expected: FAIL — no module `rvcw.ble.session`

- [ ] **Step 3: Implement `unlock_gateway` in `rvcw/ble/session.py`**

```python
# rvcw/ble/session.py
"""Gateway unlock and per-device REMOTE_CONTROL sessions."""
from __future__ import annotations

import asyncio

from rvcw import config
from rvcw.ble.transport import GatewayLink
from rvcw.protocol import (
    CAN_BLE_KEY_SEED_CIPHER,
    KEY_CHAR_UUID,
    PASSWORD_UNLOCK_CHAR_UUID,
    UNLOCK_STATUS_CHAR_UUID,
    calculate_can_ble_key_seed_key,
)


class GatewayUnlockError(RuntimeError):
    pass


async def unlock_gateway(link: GatewayLink) -> None:
    """Key/seed unlock via UNLOCK_STATUS/KEY, then PASSWORD_UNLOCK PIN if gated."""
    client = link.client

    unlock = await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID)
    if bytes(unlock).lower() == b"unlocked":
        return
    if len(unlock) < 4:
        raise GatewayUnlockError(
            f"UNLOCK_STATUS returned {len(unlock)} bytes, want a 4-byte seed or 'unlocked'"
        )
    seed = bytes(unlock[:4])
    key = calculate_can_ble_key_seed_key(seed, CAN_BLE_KEY_SEED_CIPHER)
    await client.write_gatt_char(KEY_CHAR_UUID, key, response=True)
    await asyncio.sleep(0.5)
    verify = bytes(await client.read_gatt_char(UNLOCK_STATUS_CHAR_UUID))
    if not (verify.lower() == b"unlocked" or verify == b"\x01"):
        raise GatewayUnlockError(f"key/seed unlock failed — verify={verify.hex()}")

    try:
        lock = bytes(await client.read_gatt_char(PASSWORD_UNLOCK_CHAR_UUID))
    except Exception:
        return  # char not accessible on this gateway — proceed (as upstream does)
    if lock and lock[0] != 0x00:
        return  # gate already open
    if not link.pin:
        raise GatewayUnlockError(
            "password gate locked and no pin configured — put "
            f"gateway.pin in {config.gateway_path().as_posix()}"
        )
    for attempt in (1, 2):
        await client.write_gatt_char(
            PASSWORD_UNLOCK_CHAR_UUID, link.pin.encode("utf-8"), response=False
        )
        await asyncio.sleep(1.0)
        verify = bytes(await client.read_gatt_char(PASSWORD_UNLOCK_CHAR_UUID))
        if verify and verify[0] != 0x00:
            return
    raise GatewayUnlockError(f"PIN unlock failed — verify={verify.hex() or 'empty'}")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_unlock.py -v`
Expected: PASS (4 tests). Async tests need `asyncio`: add `pytest.ini`-free marker with `pytest_plugins`? Simplest: run with `-p anyio`? No — use `pytest-asyncio`. Add `pytest-asyncio>=0.24` to the dev group in `pyproject.toml`, set `asyncio_mode = "auto"` in `[tool.pytest.ini_options]`, and `uv sync`. Apply this SAME setup now if earlier task tests used bare `async def` tests (none did so far — Task 8's will rely on it).

- [ ] **Step 5: Commit**

```bash
git add rvcw/ble/session.py tests/test_unlock.py pyproject.toml uv.lock
git commit -m "feat: key/seed unlock and PIN gate with pytest-asyncio

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

### Task 7: LocalHost claim + identity advertisement

**Files:**
- Modify: `rvcw/ble/can.py` (create; `rvcw/ble/can.py` module)
- Test: `tests/test_local_host.py`

**Interfaces:**
- Consumes: `GatewayLink`, vendored `compose_ids_can_standard_wire_frame`/`_extended_…`, `parse_ids_can_wire_frame`.
- Produces: `pseudo_mac(key: str) -> bytes` (6-byte sha256-derived, nonzero); `choose_local_host_address(observed: set[int], mac: bytes, seed_text: str = "rvcw") -> int` (never 0x00/0xFF/observed; deterministic from mac+seed_text; 0xFA only as last resort); `identity_frames(source: int, mac: bytes) -> list[bytes]` (5 frames: NETWORK, PRODUCT_STATUS, DEVICE_ID — product 46, device type 22, function 2 — CIRCUIT_ID, DEVICE_STATUS, ~40 ms apart at send time); `async def claim_local_host(link: GatewayLink, observed: set[int]) -> int` (sets `link.local_host_address`, writes claim from src 0xFF, waits 1.05 s, writes NETWORK online frame, writes identity frames, returns the claimed address).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_local_host.py
import re

import pytest

from rvcw.ble.can import (
    choose_local_host_address,
    identity_frames,
    pseudo_mac,
)
from rvcw.protocol import parse_ids_can_wire_frame


def test_pseudo_mac_is_stable_and_nonzero():
    a, b = pseudo_mac("rvcw:X"), pseudo_mac("rvcw:X")
    assert a == b and len(a) == 6 and a != bytes(6)
    assert pseudo_mac("other") != a


def test_choose_avoids_observed_and_reserved():
    observed = {1, 2, 3}
    addr = choose_local_host_address(observed, pseudo_mac("rvcw:X"))
    assert addr not in observed and addr not in (0x00, 0xFF)
    assert 0x01 <= addr <= 0xFE


def test_choose_deterministic_for_same_mac():
    assert choose_local_host_address(set(), pseudo_mac("a")) == choose_local_host_address(
        set(), pseudo_mac("a")
    )


def test_identity_frames_parse_back():
    mac = pseudo_mac("rvcw:X")
    frames = identity_frames(0x2D, mac)
    kinds = [parse_ids_can_wire_frame(f) for f in frames]
    assert [w.message_type for w in kinds] == [0x00, 0x06, 0x02, 0x01, 0x03]
    # DEVICE_ID frame: product_id=46 big-endian, device_type=22, function_name=2
    dev = kinds[2]
    assert int.from_bytes(dev.payload[0:2], "big") == 46
    assert dev.payload[3] == 22
    assert int.from_bytes(dev.payload[4:6], "big") == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_local_host.py -v`
Expected: FAIL — no module `rvcw.ble.can`

- [ ] **Step 3: Implement `rvcw/ble/can.py`**

```python
# rvcw/ble/can.py
"""IDS-CAN participant identity: LocalHost claim, identity frames, commands."""
from __future__ import annotations

import asyncio
import hashlib

from rvcw.ble.transport import GatewayLink
from rvcw.protocol import (
    IdsCanWireFrame,
    compose_ids_can_extended_wire_frame,
    compose_ids_can_standard_wire_frame,
    parse_ids_can_wire_frame,
)

PRODUCT_ID_ANDROID_APP = 46     # OneControl Android Mobile App
DEVICE_TYPE_ANDROID = 22        # ANDROID_MOBILE_DEVICE
FUNCTION_NAME_MYRV_TABLET = 2


def pseudo_mac(key: str) -> bytes:
    """Stable 6-byte pseudo-MAC for this rvcw LocalHost (mirrors upstream CanAdapterFactory)."""
    mac = bytearray(hashlib.sha256(key.encode("utf-8")).digest()[:6])
    if all(b == 0x00 for b in mac):
        mac[0] = 0x01
    return bytes(mac)


def choose_local_host_address(observed: set[int], mac: bytes, seed_text: str = "rvcw") -> int:
    """Deterministically pick an unclaimed participant address (never 0x00/0xFF)."""
    candidates = [c for c in range(0x01, 0xFF) if c not in observed]
    if not candidates:
        return 0xFA
    seed = hashlib.sha256(mac + seed_text.encode("utf-8")).digest()
    start = int.from_bytes(seed[:2], "big") % len(candidates)
    for offset in range(len(candidates)):
        candidate = candidates[(start + offset) % len(candidates)]
        return candidate
    return 0xFA


def identity_frames(source: int, mac: bytes) -> list[bytes]:
    """The five LocalHost identity frames, in official LocalDevice order."""
    s = source & 0xFF
    return [
        compose_ids_can_standard_wire_frame(0x00, s, bytes([0x00, 0x12]) + mac),  # NETWORK online
        compose_ids_can_standard_wire_frame(0x06, s, b"\x00"),                    # PRODUCT_STATUS
        compose_ids_can_standard_wire_frame(                                      # DEVICE_ID
            0x02, s,
            bytes([
                0x00, 0x2E,            # product_id 46 (0x002E)
                s,                     # product instance == this address
                DEVICE_TYPE_ANDROID,   # 22
                0x00, 0x02,            # function_name 2 (MYRV_TABLET), big-endian
                0x00,                  # device_instance=0, function_instance=0
                0x00,                  # capabilities=0
            ]),
        ),
        compose_ids_can_standard_wire_frame(0x01, s, b"\x00\x00\x00\x00"),        # CIRCUIT_ID
        compose_ids_can_standard_wire_frame(0x03, s, b""),                        # DEVICE_STATUS
    ]


async def claim_local_host(link: GatewayLink, observed: set[int]) -> int:
    """Claim and advertise a LocalHost address; returns the claimed address."""
    mac = pseudo_mac(f"rvcw:{link.mac}")
    claimed = choose_local_host_address(observed, mac)
    await asyncio.sleep(1.05)  # official one-second address-claim listen window
    claim = compose_ids_can_standard_wire_frame(  # src 0xFF broadcast during claim
        0x00, 0xFF, bytes([claimed, 0x12]) + mac
    )
    await link.write_can_frame(claim)
    await asyncio.sleep(1.05)  # official claim window
    link.local_host_address = claimed
    for frame in identity_frames(claimed, mac):
        await link.write_can_frame(frame)
        await asyncio.sleep(0.04)
    return claimed
```

- [ ] **Step 4: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: PASS (all). Note `choose_local_host_address` returns inside the loop unconditionally (deterministic first pick after rotation); the loop exists to mirror upstream structure — simplify to a single `return candidates[start % len(candidates)]` if the loop reads as dead code on review.

- [ ] **Step 5: Commit**

```bash
git add rvcw/ble/can.py tests/test_local_host.py
git commit -m "feat: LocalHost address claim and identity advertisement

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

---

### Task 8: REMOTE_CONTROL session (seed/key, heartbeat, close)

**Files:**
- Modify: `rvcw/ble/session.py` (append)
- Modify: `rvcw/ble/transport.py` (frame dispatch for awaiting futures)
- Test: `tests/test_remote_control.py`

**Interfaces:**
- Consumes: `GatewayLink`, vendored `compose_ids_can_extended_wire_frame`, `parse_ids_can_wire_frame`, `tea_encrypt`, `RC_CYPHER` (new re-export: add `from .tea import RC_CYPHER` to `rvcw/protocol/__init__.py`).
- Produces: `class RemoteControlSessionError(RuntimeError)`;
  `class RemoteControl`:
  - `RemoteControl(link: GatewayLink)` — caller registers `link.on_frame = rc.handle_frame` (see Task 10/11)
  - `open: bool`, `target: int | None`
  - `async ensure(device_id: int) -> bool` — preflight (0x40 `[0,0]`, `[0,1]`, 0x41 `[0,4]` REQUESTs, 50 ms apart), then REQUEST 0x42 `[0x00,0x04]` → RESPONSE 0x81/0x42 payload `[sid2, seed4BE]` → `key = tea_encrypt(RC_CYPHER, seed)` → REQUEST 0x43 `[sid2, key4BE]` → RESPONSE 0x81/0x43 → `self.open = True`, heartbeat task every 0.5 s; seed-key timeout 0.45 s, key-wait 3.0 s; on seed timeout → `RemoteControlSessionError` (our V1 gateway requires explicit exchange — no silent fallback)
  - `async end(device_id: int | None = None) -> None` — REQUEST 0x45 `[sid2]`, cancels heartbeat, `open = False`
  - `async aclose() -> None` — cancels heartbeat task only.
- `rvcw/ble/transport.py` is unchanged in this task (frame dispatch rides the existing `on_frame` hook).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_remote_control.py
import pytest

from rvcw.ble.session import RemoteControl, RemoteControlSessionError
from rvcw.protocol import (
    compose_ids_can_standard_wire_frame,
    parse_ids_can_wire_frame,
    tea_encrypt,
    RC_CYPHER,
)


class RecordingLink:
    """Minimal GatewayLink stand-in: records writes, answers session requests."""

    def __init__(self, responses: list[bytes]):
        self.written: list[bytes] = []
        self.local_host_address = 0x2D
        self._responses = list(responses)

    on_frame = None

    async def write_can_frame(self, frame: bytes) -> None:
        self.written.append(frame)
        wire = parse_ids_can_wire_frame(frame)
        if wire is None or wire.message_type != 0x80:
            return
        if wire.message_data not in (0x42, 0x43):
            return  # preflight/heartbeat/other requests get no canned answer
        if self._responses:
            response = self._responses.pop(0)
            wire_resp = parse_ids_can_wire_frame(response)
            if wire_resp is not None and self.on_frame is not None:
                self.on_frame(wire_resp)


def make_seed_response(seed: int) -> bytes:
    # RESPONSE (0x81) from device 0x2E to us (0x2D), msg_data 0x42, payload [sid2][seed4BE]
    return compose_ids_can_extended_wire_frame(
        0x81, 0x2E, 0x2D, 0x42, bytes([0x00, 0x04]) + seed.to_bytes(4, "big")
    )


def make_key_ack() -> bytes:
    return compose_ids_can_extended_wire_frame(0x81, 0x2E, 0x2D, 0x43, b"\x00\x04")


def key_for(seed: int) -> int:
    return tea_encrypt(RC_CYPHER, seed)


async def test_ensure_opens_session():
    seed = 0x0BADF00D
    link = RecordingLink([make_seed_response(seed), make_key_ack()])
    rc = RemoteControl(link)  # type: ignore[arg-type]
    # wire the response dispatch the way the executor does (Task 11)
    link.on_frame = rc.handle_frame
    assert await rc.ensure(0x2E) is True
    assert rc.open and rc.target == 0x2E
    # seed request, then key request written with the TEA-encrypted seed
    kinds = [parse_ids_can_wire_frame(f) for f in link.written]
    seed_reqs = [w for w in kinds if w and w.message_type == 0x80 and w.message_data == 0x42]
    key_reqs = [w for w in kinds if w and w.message_type == 0x80 and w.message_data == 0x43]
    assert seed_reqs and key_reqs
    key_req = key_reqs[-1]
    assert key_req.payload[0:2] == b"\x00\x04"
    assert int.from_bytes(key_req.payload[2:6], "big") == key_for(seed)
    await rc.aclose()


async def test_ensure_seed_timeout_raises():
    link = RecordingLink([])  # no responses at all
    rc = RemoteControl(link)  # type: ignore[arg-type]
    with pytest.raises(RemoteControlSessionError):
        await rc.ensure(0x2E)
    assert not rc.open


async def test_status_response_for_seed_raises():
    # gateway answers the seed request with a single-byte status RESPONSE, 0x0B = BUSY
    status_frame = compose_ids_can_extended_wire_frame(0x81, 0x2E, 0x2D, 0x42, b"\x0B")
    link = RecordingLink([status_frame])
    rc = RemoteControl(link)  # type: ignore[arg-type]
    link.on_frame = rc.handle_frame
    with pytest.raises(RemoteControlSessionError, match="BUSY|0x0B"):
        await rc.ensure(0x2E)
```

Also add to `tests/test_transport.py` (same PR as this task, keeps transport coverage honest):

```python
def test_relay_states_updated_from_device_status(monkeypatch):
    from rvcw.ble import transport as T

    # Fake a derived status parser (real one arrives in Task 9)
    monkeypatch.setattr(GatewayLink, "relay_state_from_status", staticmethod(lambda payload: (payload[0] & 1) == 1 if payload else None))
    link = GatewayLink("10:06:1C:E7:B8:3E")
    status_frame = bytes([0x02, 0x03, 0x41, 0x01])  # [dlc=2][type=3][addr=0x41] status=0x01
    link._on_notification(bytes(status_frame))
    assert link.relay_states[0x41] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_remote_control.py tests/test_transport.py -v`
Expected: FAIL — `RemoteControl` missing; `test_relay_states_updated_from_device_status` passes only via monkeypatched parser (acceptable — it pins the dispatch plumbing).

- [ ] **Step 3: Implement** — append to `rvcw/ble/session.py`, re-export `RC_CYPHER`:

`rvcw/protocol/__init__.py` gains `RC_CYPHER` in its `from .tea import (...)` list.

Append to `rvcw/ble/session.py` (extending its imports):

```python
import asyncio

from rvcw.protocol import (
    RC_CYPHER,
    IdsCanWireFrame,
    compose_ids_can_extended_wire_frame,
)
from rvcw.protocol.ids_can_wire import ids_can_response_name

_RC_SID_BYTES = bytes([0x00, 0x04])
_RC_SESSION_HEARTBEAT_S = 0.5
_SEED_TIMEOUT_S = 0.45
_KEY_TIMEOUT_S = 3.0


class RemoteControlSessionError(RuntimeError):
    pass


class RemoteControl:
    """Per-device REMOTE_CONTROL session (id 0x0004) with a 0.5 s heartbeat.

    Responses (message_type 0x81) are correlated through per-request futures
    resolved by ``handle_frame`` — which the caller must register on
    ``link.on_frame`` BEFORE any request is written.
    """

    def __init__(self, link: GatewayLink) -> None:
        self.link = link
        self.open = False
        self.target: int | None = None
        self._seed_fut: asyncio.Future[int] | None = None
        self._key_fut: asyncio.Future[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    # ---- frame dispatch (registered as link.on_frame) --------------------

    def handle_frame(self, frame: IdsCanWireFrame) -> None:
        if frame.message_type != 0x81:
            return
        msg = frame.message_data or 0
        if msg in (0x42, 0x43) and len(frame.payload) == 1:
            # Single-byte IDS status response: reject into the pending future.
            code = frame.payload[0]
            err = RemoteControlSessionError(
                f"session request 0x{msg:02X} rejected: 0x{code:02X} "
                f"({ids_can_response_name(code)})"
            )
            if msg == 0x42 and self._seed_fut and not self._seed_fut.done():
                self._seed_fut.set_exception(err)
            elif msg == 0x43 and self._key_fut and not self._key_fut.done():
                self._key_fut.set_exception(err)
            return
        if msg == 0x42 and len(frame.payload) >= 6 and self._seed_fut and not self._seed_fut.done():
            self._seed_fut.set_result(int.from_bytes(frame.payload[2:6], "big"))
        elif msg == 0x43 and self._key_fut and not self._key_fut.done():
            self._key_fut.set_result(None)
        elif msg == 0x45:  # device closed the session (heartbeat ACK 0x44 is NOT a close)
            self.open = False
            self.target = None

    async def ensure(self, device_id: int) -> bool:
        if self.open and self.target == device_id:
            return True
        source = self.link.local_host_address
        if source is None:
            raise RemoteControlSessionError("LocalHost address not claimed yet")

        # Preflight: session list/status queries (50 ms apart, small settle window)
        for req_code, payload in ((0x40, b"\x00\x00"), (0x40, b"\x00\x01"), (0x41, _RC_SID_BYTES)):
            await self.link.write_can_frame(
                compose_ids_can_extended_wire_frame(0x80, source, device_id, req_code, payload)
            )
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.20)

        # Seed request: REQUEST(0x80)/0x42 payload [sid2] -> RESPONSE/0x42 [sid2, seed4BE]
        loop = asyncio.get_running_loop()
        self._seed_fut = loop.create_future()
        await self.link.write_can_frame(
            compose_ids_can_extended_wire_frame(0x80, source, device_id, 0x42, _RC_SID_BYTES)
        )
        try:
            seed = await asyncio.wait_for(self._seed_fut, timeout=_SEED_TIMEOUT_S)
        except TimeoutError:
            raise RemoteControlSessionError(
                f"SESSION_REQUEST_SEED timed out for device 0x{device_id:02X} "
                "(V1 gateway requires an explicit exchange — check source/target)"
            ) from None

        # Key transmit: REQUEST/0x43 payload [sid2, key4BE] -> RESPONSE/0x43 [sid2]
        key = tea_encrypt(RC_CYPHER, seed)
        self._key_fut = loop.create_future()
        await self.link.write_can_frame(
            compose_ids_can_extended_wire_frame(
                0x80, source, device_id, 0x43, _RC_SID_BYTES + key.to_bytes(4, "big")
            )
        )
        try:
            await asyncio.wait_for(self._key_fut, timeout=_KEY_TIMEOUT_S)
        except TimeoutError:
            raise RemoteControlSessionError(
                f"SESSION_TRANSMIT_KEY timed out for device 0x{device_id:02X}"
            ) from None

        self.open = True
        self.target = device_id
        self._heartbeat_task = asyncio.create_task(self._heartbeat(device_id))
        return True

    async def _heartbeat(self, device_id: int) -> None:
        source = self.link.local_host_address
        while self.open and self.target == device_id:
            await asyncio.sleep(_RC_SESSION_HEARTBEAT_S)
            if not (self.open and self.target == device_id):
                break
            try:
                await self.link.write_can_frame(
                    compose_ids_can_extended_wire_frame(
                        0x80, source, device_id, 0x44, _RC_SID_BYTES
                    )
                )
            except Exception:
                return  # link down; the gateway drops the session with it

    async def end(self, device_id: int | None = None) -> None:
        """Explicitly close the session (REQUEST/0x45 payload [sid2])."""
        target = device_id if device_id is not None else self.target
        await self._cancel_heartbeat()
        self.open = False
        self.target = None
        if target is None:
            return
        source = self.link.local_host_address
        if source is None:
            return
        try:
            await self.link.write_can_frame(
                compose_ids_can_extended_wire_frame(0x80, source, target, 0x45, _RC_SID_BYTES)
            )
        except Exception:
            pass  # link down; the gateway drops the session with the connection

    async def _cancel_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None

    async def aclose(self) -> None:
        """Cancel the heartbeat WITHOUT SESSION_END (the 1.0.48 relay gotcha)."""
        await self._cancel_heartbeat()
```

The executor (Task 11) chains the dispatch as `link.on_frame = rc.handle_frame` (composed over any prior handler) immediately after constructing `RemoteControl`, before `ensure` is called.

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_remote_control.py -v`
Expected: PASS (3 tests). Note `RecordingLink.write_can_frame` answers seed/key
requests synchronously *during* the write, which is why `handle_frame` must be
registered on `link.on_frame` before `ensure()` — the futures design exists
for exactly this.

- [ ] **Step 5: Commit**

```bash
git add rvcw/ble/session.py rvcw/ble/transport.py rvcw/protocol/__init__.py tests/
git commit -m "feat: REMOTE_CONTROL session with seed/key exchange and heartbeat

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

### Task 9: Relay command, `sniff`, and DEVICE_STATUS derivation

**Files:**
- Modify: `rvcw/ble/can.py` (append)
- Modify: `rvcw/cli.py` (add `sniff`)
- Test: `tests/test_relay.py`

**Interfaces:**
- Consumes: `GatewayLink`, `RemoteControl`, vendored frame composers.
- Produces: `relay_command(source: int, target: int, on: bool) -> bytes`; `class RelayConfirmError(RuntimeError)` (fields `device_id: int`, `on: bool`); `async def confirm_relay(link, device_id: int, on: bool, timeout: float) -> bool` (polls `link.relay_states` every 50 ms until deadline, final dict check); `async def set_relay(link, rc: RemoteControl, device_id: int, on: bool, confirm_timeout: float = 2.0) -> None` (ensure session → write → confirm; on miss, one rewrite + reconfirm; then `RelayConfirmError`). `GatewayLink.relay_state_from_status(payload: bytes) -> bool | None` gets its REAL implementation here (derived from live capture; `None` = unrecognized). CLI gains `rvcw sniff` streaming decoded frames.

- [ ] **Step 1: Write the frame-composition and retry tests** (the state parser test is data-dependent — Step 4)

```python
# tests/test_relay.py
import pytest

from rvcw.ble.can import RelayConfirmError, relay_command
from rvcw.protocol import parse_ids_can_wire_frame


def test_relay_command_frame():
    frame = relay_command(0x2D, 0x2E, True)
    wire = parse_ids_can_wire_frame(frame)
    assert wire.message_type == 0x82
    assert wire.source_address == 0x2D
    assert wire.target_address == 0x2E
    assert wire.message_data == 0x01
    assert wire.payload == b""
    off = parse_ids_can_wire_frame(relay_command(0x2D, 0x2E, False))
    assert off.message_data == 0x00
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_relay.py -v`
Expected: FAIL — `relay_command` missing

- [ ] **Step 3: Implement `relay_command`, `confirm_relay`, `set_relay`; add `sniff` CLI**

Append to `rvcw/ble/can.py`:

```python
def relay_command(source: int, target: int, on: bool) -> bytes:
    """RelayBasicLatching Type2 COMMAND: 0x01=on / 0x00=off in message_data."""
    return compose_ids_can_extended_wire_frame(
        0x82, source, target, 0x01 if on else 0x00, b""
    )


class RelayConfirmError(RuntimeError):
    def __init__(self, device_id: int, on: bool) -> None:
        self.device_id = device_id
        self.on = on
        super().__init__(
            f"relay 0x{device_id:02X} did not confirm {'ON' if on else 'OFF'}"
        )


async def confirm_relay(link: GatewayLink, device_id: int, on: bool, timeout: float) -> bool:
    """Poll DEVICE_STATUS-derived relay state until the deadline."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = link.relay_states.get(device_id)
        if state is on:
            return True
        await asyncio.sleep(0.05)
    return link.relay_states.get(device_id) is on


async def set_relay(
    link: GatewayLink,
    rc: "object",  # RemoteControl (import avoided to keep can.py below session)
    device_id: int,
    on: bool,
    confirm_timeout: float = 2.0,
) -> None:
    from rvcw.ble.session import RemoteControl, RemoteControlSessionError  # noqa: F401

    if link.local_host_address is None:
        raise LinkError("LocalHost address not claimed")
    if not await rc.ensure(device_id):
        raise RemoteControlSessionError(f"session failed for device 0x{device_id:02X}")
    await link.write_can_frame(relay_command(link.local_host_address, device_id, on))
    if not await confirm_relay(link, device_id, on, timeout=confirm_timeout):
        # one rewrite (idempotent for a latching relay), then fail
        await link.write_can_frame(relay_command(link.local_host_address, device_id, on))
        if not await confirm_relay(link, device_id, on, timeout=confirm_timeout):
            raise RelayConfirmError(device_id, on)
```

Add to `rvcw/cli.py`:

```python
@cli.command()
def sniff() -> None:
    """Stream decoded CAN frames (for deriving DEVICE_STATUS relay formats)."""

    async def run() -> None:
        cfg = load_gateway()
        from .ble.session import RemoteControl
        from .ble.transport import GatewayLink

        link = GatewayLink(cfg.mac, cfg.pin)
        rc = RemoteControl(link)

        def show(frame) -> None:
            click.echo(
                f"type=0x{frame.message_type:02X} src=0x{frame.source_address:02X} "
                f"dst={frame.target_address if frame.target_address is None else f'0x{frame.target_address:02X}'} "
                f"data={'0x%02X' % frame.message_data if frame.message_data is not None else ''} "
                f"payload={frame.payload.hex()}"
            )

        link.on_frame = lambda frame: (rc.handle_frame(frame), show(frame))
        await link.connect()
        try:
            from .ble.session import unlock_gateway

            await unlock_gateway(link)
            click.echo("sniffing… Ctrl-C to stop")
            while True:
                await asyncio.sleep(0.5)
        finally:
            await rc.aclose()
            await link.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
```

- [ ] **Step 4: Run, and run the unit test**

Run: `uv run pytest tests/test_relay.py -v`
Expected: PASS (1 test)

- [ ] **Step 5: [HW] Capture DEVICE_STATUS frames and derive the parser**

This is the hardware-informed derivation loop. With the user present:

1. Ensure HA has the panel connected (`bluetoothctl info 10:06:1C:E7:B8:3E` shows `Connected: yes`).
2. Run `uv run rvcw sniff` (multiplexed with HA). Expected: a stream of decoded frames, including `type=0x03` DEVICE_STATUS frames and V2 Packed-synthesized ones.
3. Ask the user to toggle one known panel light in HA twice (on → off). Record the DEVICE_STATUS frames whose `src` matches that device in the meantime.
4. From the capture, identify the payload byte/bit that changes with `is_on` (candidate: first status byte state bit). Implement:

```python
    @staticmethod
    def relay_state_from_status(payload: bytes) -> bool | None:
        # DERIVED FROM CAPTURE <date>: <describe the byte/bit mapping in one line here>
        if not payload:
            return None
        return None  # ← replace with the derived mapping, or keep None if unresolvable
```

5. Add the captured frame as a regression test:

```python
def test_relay_state_from_captured_status():
    from rvcw.ble.transport import GatewayLink

    captured = bytes.fromhex("PASTE_CAPTURED_PAYLOAD")  # from step 3
    assert GatewayLink.relay_state_from_status(captured) is True
    captured_off = bytes.fromhex("PASTE_CAPTURED_PAYLOAD_OFF")
    assert GatewayLink.relay_state_from_status(captured_off) is False
```

If the derivation is unresolvable from the capture, keep the parser returning `None` (confirm_relay then always misses → set_relay raises) — record that outcome in the commit message and set the executor's default `--no-confirm` equivalent (Task 11 wires `pacing` accordingly); do NOT invent a format.

- [ ] **Step 6: Commit**

```bash
git add rvcw/ble/can.py rvcw/cli.py tests/test_relay.py tests/test_transport.py
git commit -m "feat: relay command, state confirmation, sniff; derive status parser

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

---

### Task 10: Phase-1 gate — `probe-session` [HW]

**Files:**
- Modify: `rvcw/cli.py` (add `probe-session`)
- Modify: `rvcw/executor.py` (create shared `open_gateway` helper used by all live commands)
- Test: `tests/test_open_gateway.py`

**Interfaces:**
- Consumes: `GatewayLink`, `unlock_gateway`, `claim_local_host`, `RemoteControl`, `load_gateway`.
- Produces: `@asynccontextmanager async def open_gateway(config: GatewayConfig, *, observed: set[int] | None = None) -> AsyncIterator[tuple[GatewayLink, RemoteControl]]` — connect, unlock, claim LocalHost (observed defaults to `set()`), register `rc.handle_frame` on the link, yield `(link, rc)`; on exit cancels the RC heartbeat with `rc.aclose()` (NOT `end` — the 1.0.48 gotcha) and closes the link. CLI gains `rvcw probe-session [--device 0xHH]`.

- [ ] **Step 1: Implement `rvcw/executor.py`**

```python
# rvcw/executor.py
"""Schedule execution against the live gateway."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from .ble.can import claim_local_host
from .ble.session import RemoteControl, unlock_gateway
from .ble.transport import GatewayLink
from .config import GatewayConfig


@asynccontextmanager
async def open_gateway(
    config: GatewayConfig, observed: set[int] | None = None
) -> AsyncIterator[tuple[GatewayLink, RemoteControl]]:
    """Connect + unlock + claim LocalHost; RC heartbeat cancelled on exit."""
    link = GatewayLink(config.mac, config.pin)
    await link.connect()
    rc: RemoteControl | None = None
    try:
        await unlock_gateway(link)
        await claim_local_host(link, observed if observed is not None else set())
        rc = RemoteControl(link)
        link.on_frame = rc.handle_frame
        yield link, rc
    finally:
        if rc is not None:
            await rc.aclose()  # cancel heartbeat; NOT end() (relay gotcha)
        await link.close()
```

- [ ] **Step 2: Add the probe test with fakes**

```python
# tests/test_open_gateway.py
import pytest


@pytest.mark.asyncio
async def test_open_gateway_yields_link_and_closes(monkeypatch):
    from rvcw import executor
    from rvcw.ble.session import RemoteControl

    calls: list[str] = []

    class FakeLink:
        mac = "fake"
        pin = None
        local_host_address = 0x2D

        async def connect(self):
            calls.append("connect")

        async def close(self):
            calls.append("close")

    async def fake_unlock(link):
        calls.append("unlock")

    async def fake_claim(link, observed):
        calls.append(f"claim:{observed == set()!r}")
        link.local_host_address = 0x2D

    monkeypatch.setattr(executor, "GatewayLink", lambda mac, pin: FakeLink())
    monkeypatch.setattr(executor, "unlock_gateway", fake_unlock)
    monkeypatch.setattr(executor, "claim_local_host", fake_claim)

    async with executor.open_gateway(config=None) as (link, rc):  # type: ignore[arg-type]
        assert isinstance(rc, RemoteControl)
        assert link.local_host_address == 0x2D
    assert calls[0] == "connect" and calls[-1] == "close"
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_open_gateway.py -v`
Expected: PASS (1 test)

- [ ] **Step 4: Add the CLI command**

```python
@cli.command("probe-session")
@click.option("--device", type=int, required=True, help="target relay device id (e.g. 0x2E41)")
def probe_session(device: int) -> None:
    """Phase-1 gate: second IDS-CAN participant alongside HA. Reports and exits."""

    async def run() -> None:
        from .ble.can import relay_command
        from .ble.session import RemoteControlSessionError
        from .executor import open_gateway

        cfg = load_gateway()
        async with open_gateway(cfg) as (link, rc):
            click.echo(f"connected mac={cfg.mac} local_host=0x{link.local_host_address:02X}")
            click.echo(f"frames_received={link.frames_received}")
            try:
                ok = await rc.ensure(device)
            except RemoteControlSessionError as exc:
                click.echo(f"RESULT: REFUSED — {exc}")
                return
            click.echo(f"RESULT: SESSION OPEN with 0x{device:02X} (multiplexing works)")
            click.echo("no command frames sent; leaving bus as found")

    asyncio.run(run())
```

- [ ] **Step 5: [HW] Run the probe and record the outcome**

```bash
uv run rvcw probe-session --device <first-device-id>
```

Expected outcomes (record verbatim in `docs/protocol-notes.md` § Phase-1 probe):
- `SESSION OPEN …` → multiplexing works; proceed to Task 11.
- `RESULT: REFUSED — …` or no `SESSION OPEN` → **STOP at this gate.** Report the captured output to the user before continuing to Task 11; the fallback (coordinating with HA's coordinator) is designed only after seeing the real refusal message.
- If `rvcw probe-session` never connects (BLE refused at GATT level), also STOP and report — that is a link-level multiplexing failure, different from a session refusal.

- [ ] **Step 6: Commit**

```bash
git add rvcw/executor.py rvcw/cli.py tests/test_open_gateway.py
git commit -m "feat: open_gateway context and phase-1 session probe

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

### Task 11: Live executor — `send`, `selftest`, `session --end`

**Files:**
- Modify: `rvcw/executor.py` (append)
- Modify: `rvcw/cli.py` (wire `send` live path; add `selftest`, `session`)
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: everything from Tasks 2–10.
- Produces: `async def send_steps(link, rc, device_ids: list[int], steps: list[Step], *, confirm: bool = True, confirm_timeout: float = 2.0) -> None` (clock-paced writes to CAN_WRITE at each step boundary, gathered across members; confirmation supervised per window: one idempotent rewrite on a missed confirm, `SendAborted` on two consecutive misses); `class SendAborted(RuntimeError)`; `measured_rtt(...)` helper? — **no**: RTT measurement lives in the executor as a return value `send_steps` yields via a small `SendStats` dataclass (`rtt_ms: float | None`, `misses: int`) so the CLI can print/downscale. Exact: `@dataclass class SendStats: rtt_ms: float | None = None; misses: int = 0`; `send_steps` returns `SendStats`. CLI `send` gains `--no-confirm`; `selftest` blinks "R" at 5 WPM and prints measured RTT + max-safe WPM; `session --end` explicitly closes RC sessions (rarely needed).

- [ ] **Step 1: Write the executor test with a fake link**

```python
# tests/test_executor.py
import pytest

from rvcw.executor import SendStats, send_steps
from rvcw.morse import Step


class TimelineLink:
    """Records write times by parsing the actual relay command frames."""

    def __init__(self):
        self.local_host_address = 0x2D
        self.relay_states: dict[int, bool] = {}
        self.writes: list[tuple[float, int, bool]] = []  # (t_s, target, on)
        self._t0 = None
        self.on_frame = None

    async def write_can_frame(self, frame: bytes) -> None:
        import time

        from rvcw.protocol import parse_ids_can_wire_frame

        if self._t0 is None:
            self._t0 = time.monotonic()
        wire = parse_ids_can_wire_frame(frame)
        self.writes.append(
            (time.monotonic() - self._t0, wire.target_address, wire.message_data == 0x01)
        )

    def mark(self, device_id: int, on: bool) -> None:
        self.relay_states[device_id] = on


class FakeRC:
    open = True
    target = None

    async def ensure(self, device_id: int) -> bool:
        return True


async def test_schedule_write_times_match_elements():
    # A = [on 150, off 150, on 450]
    link = TimelineLink()
    rc = FakeRC()  # type: ignore[arg-type]
    # bypass confirm: confirm=False path
    stats = await send_steps(link, rc, [0x2E], [Step(True, 150), Step(False, 150), Step(True, 450)], confirm=False)  # type: ignore[arg-type]
    assert isinstance(stats, SendStats)
    on_times = [w[0] for w in link.writes if w[2]]
    assert len(on_times) == 2 and len(link.writes) == 3
    # first write ~0 ms, second ~150 ms, third ~300 ms
    assert on_times[0] < 0.03 and 0.13 < [w[0] for w in link.writes][1] < 0.2
    assert 0.28 < link.writes[2][0] < 0.5


async def test_abort_on_two_consecutive_misses():
    # confirm requested but the fake link NEVER updates relay_states
    link = TimelineLink()
    rc = FakeRC()
    from rvcw.executor import SendAborted

    with pytest.raises(SendAborted):
        await send_steps(
            link, rc, [0x2E], [Step(True, 150), Step(False, 150)], confirm=True,
            confirm_timeout=0.05,
        )  # type: ignore[arg-type]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_executor.py -v`
Expected: FAIL — `send_steps` missing

- [ ] **Step 3: Implement the executor core**

```python
# rvcw/executor.py — appended after open_gateway
import asyncio
import time
from dataclasses import dataclass

from .ble.can import confirm_relay, relay_command
from .ble.session import RemoteControl, RemoteControlSessionError
from .ble.transport import GatewayLink
from .morse import Step


@dataclass
class SendStats:
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
    confirm: bool = True,
    confirm_timeout: float = 2.0,
) -> SendStats:
    """Clock-paced writes, confirmation-supervised, with the off-guarantee.

    Writes land at their scheduled wall-clock times; DEVICE_STATUS
    confirmations are checked inside each element's window (at most one
    idempotent rewrite per element). Two consecutive missed elements abort;
    EVERY exit path restores the pre-send state of every touched device
    (restores are forced OFF when the pre-state was unknown — with a
    warning surfaced via SendStats.restore_failures by the caller).
    """
    if not device_ids:
        raise ValueError("no devices to blink")
    for device_id in device_ids:
        if not await rc.ensure(device_id):
            raise RemoteControlSessionError(f"no session for 0x{device_id:02X}")

    pre = {d: link.relay_states.get(d) for d in device_ids}
    stats = SendStats()
    start = time.monotonic()
    pos = 0.0
    consecutive_misses = 0
    restore_failures = 0
    try:
        for step in steps:
            pos += step.ms
            target_time = start + pos / 1000.0
            delay = target_time - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            await asyncio.gather(*(
                link.write_can_frame(
                    relay_command(link.local_host_address, d, step.on)
                )
                for d in device_ids
            ))
            if confirm:
                confirmed = await asyncio.gather(*(
                    confirm_relay(link, d, step.on, timeout=confirm_timeout)
                    for d in device_ids
                ))
                if all(confirmed):
                    consecutive_misses = 0
                else:
                    # idempotent rewrite for unconfirmed devices (latching relay:
                    # rewriting the same state is a no-op on the light)
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
    finally:
        for d in device_ids:
            state = pre.get(d)
            state = state if state is not None else False
            try:
                await link.write_can_frame(
                    relay_command(link.local_host_address, d, state)
                )
            except Exception:
                restore_failures += 1
        stats.restore_failures = restore_failures
    if restore_failures:
        import logging
        logging.getLogger(__name__).warning(
            "link lost during restore for %d device(s); lights may not be "
            "back at pre-send state",
            restore_failures,
        )
    return stats
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_executor.py -v`
Expected: PASS (2 tests). If `test_schedule_write_times_match_elements` fails
on timing bounds, check your machine's sleep jitter — widen bounds slightly
in the TEST, never accept late writes in the implementation.

- [ ] **Step 5: Wire the CLI live path + `selftest` + `session`**

Replace the Task-4 `send` command with the live-capable version (dry-run body identical; `_resolve` loses its `dry_run` argument everywhere, including this call site):

```python
@cli.command()
@click.argument("text")
@click.option("--light", required=True)
@click.option("--wpm", default=None, type=int)
@click.option("--repeat", default=1, type=int)
@click.option("--dry-run", is_flag=True)
@click.option("--no-confirm", is_flag=True, help="skip DEVICE_STATUS waiting (if status parsing failed)")
def send(text, light, wpm, repeat, dry_run, no_confirm):
    """Blink TEXT as CW on the requested light."""
    if wpm is None:
        wpm = load_gateway().wpm
    try:
        steps, device_ids = _resolve(text, light, wpm, repeat)
    except (ValueError, KeyError) as exc:
        raise click.ClickException(str(exc))
    if dry_run:
        click.echo(
            f"light={light} devices={[f'0x{d:04X}' for d in device_ids]} "
            f"wpm={wpm} steps={len(steps)} duration={sum(s.ms for s in steps)}ms"
        )
        for s in steps:
            click.echo(f"{'ON ' if s.on else 'off'} {s.ms:6d}ms")
        return

    from .ble.session import RemoteControlSessionError
    from .executor import SendAborted, open_gateway, send_steps

    async def run_live():
        async with open_gateway(load_gateway()) as (link, rc):
            return await send_steps(
                link, rc, device_ids, steps,
                confirm=not no_confirm,
                confirm_timeout=max(0.05, (1200 / wpm) / 1000 * 0.8),
            )

    try:
        click.echo("connecting…")
        stats = asyncio.run(run_live())
    except (SendAborted, RemoteControlSessionError) as exc:
        click.echo(f"aborted: {exc}", err=True)
        raise SystemExit(1)
    if stats.rtt_ms is not None and stats.rtt_ms > 0:
        max_wpm = int(1200 / max(stats.rtt_ms / 0.60, 1.0))
        if wpm > max_wpm:
            click.echo(
                f"warning: requested {wpm} WPM, measured RTT {stats.rtt_ms:.0f}ms "
                f"suggests ~{max_wpm} WPM max — reduce --wpm for crisp morse"
            )
    click.echo("sent")
```

Update Task 4's `_resolve` accordingly:

```python
def _resolve(text: str, light: str, wpm: int, repeat: int) -> tuple[
    list[morse.Step], list[int]
]:
    steps = morse.schedule(text, wpm=wpm, repeat=repeat)
    entries = lights.load_map(lights_path()).resolve(light)
    return steps, [e.device_id for e in entries]
```

Add `selftest` and `session`:

```python
@cli.command()
@click.option("--light", required=True)
def selftest(light: str) -> None:
    """Blink 'R' at 5 WPM; verify off-restore and report confirm misses."""
    steps, device_ids = _resolve("R", light, 5, 1)
    from .ble.session import RemoteControlSessionError
    from .executor import SendAborted, open_gateway, send_steps

    async def run():
        async with open_gateway(load_gateway()) as (link, rc):
            return await send_steps(link, rc, device_ids, steps, confirm=True)

    try:
        stats = asyncio.run(run())
    except (SendAborted, RemoteControlSessionError) as exc:
        click.echo(f"selftest FAILED: {exc}", err=True)
        raise SystemExit(1)
    click.echo(f"selftest OK: misses={stats.misses} rtt_ms={stats.rtt_ms}")
    click.echo("check the physical light returned to its prior state now")


@cli.command("session")
@click.option("--end", "end_session", is_flag=True)
@click.option("--device", type=int, required=True)
def session_cmd(end_session: bool, device: int) -> None:
    """Explicitly END the REMOTE_CONTROL session with a device (rarely needed)."""
    from .executor import open_gateway

    async def run():
        async with open_gateway(load_gateway()) as (link, rc):
            await rc.ensure(device)
            await rc.end(device)
            click.echo(f"session ended with 0x{device:02X} (local controls regain the device)")

    asyncio.run(run())
```

(`selftest` RTT reporting via `stats.rtt_ms`: Task 9's parser must be live for a
meaningful number; without it, `misses` will count up and selftest still
validates writes + restore.)

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: PASS (all suites)

- [ ] **Step 7: [HW] Run the live selftest with the user present**

1. Confirm multiplexing survived Task 10's probe.
2. `uv run rvcw selftest --light <mapping-name>`; expected: the light blinks "R" (·−·) at 5 WPM twice-ish per retry rules and returns to its prior state.
3. Ctrl-C mid-send in a second run; verify the off-restore ran (light returns to prior state) — this is Review Focus item 1's hardware check.
4. Record RTT/miss numbers in `docs/protocol-notes.md`.

- [ ] **Step 8: Commit**

```bash
git add rvcw/executor.py rvcw/cli.py tests/test_executor.py
git commit -m "feat: live send with confirmation supervision and off-guarantee

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git push
```

---

### Task 12: Discovery, guided learning, protocol notes, README

**Files:**
- Create: `rvcw/discovery.py`
- Modify: `rvcw/ble/can.py` (device discovery request broadcast)
- Modify: `rvcw/cli.py` (add `discover`, `learn`)
- Create: `docs/protocol-notes.md`
- Modify: `README.md`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Consumes: `GatewayLink`, `compose_ids_can_extended_wire_frame`, `FUNCTION_NAMES`, `set_relay`, `lights.save_light/save_combo`.
- Produces: `@dataclass class DiscoveredDevice: address: int; product_id: int; device_type: int; function_name: int; function_instance: int`; `async def device_discovery_request(link) -> None` (REQUEST 0x80/0x02 broadcast to 0xFF from the claimed LocalHost); `async def collect_devices(link: GatewayLink, window_s: float = 3.0) -> list[DiscoveredDevice]` — subscribes a collector on `link.on_frame` for DEVICE_ID frames (V2 Packed frame index 2: payload product_id `payload[0:2]` BE, product_instance `payload[2]`, device_type `payload[3]`, function_name `payload[4:6]` BE, instances/caps `payload[6]`) for `window_s`, deduplicated by address; CLI `rvcw discover` lists them; CLI `rvcw learn NAME` walks candidates toggling ~0.6 s with `set_relay`, `click.confirm` after each, saving on yes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_discovery.py
import pytest

from rvcw.discovery import DiscoveredDevice, parse_device_id_frame


def test_parse_device_id_frame():
    # DEVICE_ID (11-bit) wire frame for address 0x2E: product 46, type 22, function 2
    frame_bytes = bytes([8, 0x02, 0x2E, 0x00, 0x2E, 0x2E, 22, 0x00, 0x02, 0x00, 0x00])
    dev = parse_device_id_frame(frame_bytes)
    assert dev is not None
    assert dev.address == 0x2E
    assert dev.product_id == 46
    assert dev.device_type == 22
    assert dev.function_name == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_discovery.py -v`
Expected: FAIL — no module `rvcw.discovery`

- [ ] **Step 3: Implement `rvcw/discovery.py`**

```python
# rvcw/discovery.py
"""Enumerate relay devices on the IDS-CAN bus; guided name learning."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rvcw.ble.transport import GatewayLink
from rvcw.protocol import (
    FUNCTION_NAMES,
    compose_ids_can_extended_wire_frame,
    compose_ids_can_standard_wire_frame,
    parse_ids_can_wire_frame,
)


@dataclass(frozen=True)
class DiscoveredDevice:
    address: int
    product_id: int
    device_type: int
    function_name: int
    function_instance: int


def parse_device_id_frame(frame_bytes: bytes) -> DiscoveredDevice | None:
    """Parse an 11-bit DEVICE_ID wire frame ([dlc][2][addr][8 payload bytes])."""
    if len(frame_bytes) < 2 or frame_bytes[1] != 0x02:
        return None
    wire = parse_ids_can_wire_frame(frame_bytes)
    if wire is None or wire.message_type != 0x02 or len(wire.payload) < 7:
        return None
    return DiscoveredDevice(
        address=wire.source_address,
        product_id=int.from_bytes(wire.payload[0:2], "big"),
        device_type=wire.payload[3],
        function_name=int.from_bytes(wire.payload[4:6], "big"),
        function_instance=wire.payload[6] & 0x0F,
    )


async def device_discovery_request(link: GatewayLink) -> None:
    """Broadcast a DEVICE_ID request (0x80/0x02 -> 0xFF) from the LocalHost."""
    source = link.local_host_address
    if source is None:
        from rvcw.ble.session import RemoteControlSessionError

        raise RemoteControlSessionError("LocalHost address not claimed")
    await link.write_can_frame(
        compose_ids_can_extended_wire_frame(0x80, source, 0xFF, 0x02, b"")
    )


async def collect_devices(link: GatewayLink, window_s: float = 3.0) -> list[DiscoveredDevice]:
    """Listen for DEVICE_ID frames for a window; dedupe by address."""
    found: dict[int, DiscoveredDevice] = {}
    previous = link.on_frame

    def hook(frame) -> None:
        # DEVICE_ID frames also arrive via notifications handled by decode_notification,
        # which hands us IdsCanWireFrame objects; wrap raw wire bytes back for parsing.
        if frame.message_type == 0x02:
            wire = frame
            rebuilt = bytes([len(wire.payload), 0x02, wire.source_address]) + wire.payload
            dev = parse_device_id_frame(rebuilt)
            if dev is not None:
                found[dev.address] = dev
        if previous is not None:
            previous(frame)

    link.on_frame = hook
    await device_discovery_request(link)
    await asyncio.sleep(window_s)
    link.on_frame = previous
    return sorted(found.values(), key=lambda d: d.address)
```

- [ ] **Step 4: Add the CLI commands**

```python
@cli.command()
def discover() -> None:
    """List relay devices found on the IDS-CAN bus."""

    async def run():
        from .ble.transport import GatewayLink
        from .ble.session import unlock_gateway
        from .ble.can import claim_local_host
        from .discovery import collect_devices

        link = GatewayLink(load_gateway().mac, load_gateway().pin)
        await link.connect()
        try:
            await unlock_gateway(link)
            await claim_local_host(link, set())
            devices = await collect_devices(link, window_s=3.0)
            for d in devices:
                name = FUNCTION_NAMES.get(d.function_name, f"fn 0x{d.function_name:04X}")
                click.echo(f"device 0x{d.address:02X} product={d.product_id} type={d.device_type} {name}")
        finally:
            await link.close()

    asyncio.run(run())


@cli.command()
@click.argument("name")
@click.option("--yes", is_flag=True, help="skip confirmation (save all offered)")
def learn(name: str, yes: bool) -> None:
    """Guided toggle-and-observe mapping: NAME gets a device from candidates."""

    async def run():
        from .ble.can import set_relay
        from .ble.session import RemoteControl
        from .discovery import collect_devices
        from .executor import open_gateway
        from .config import lights_path
        from . import lights as lights_mod

        candidates: list[DiscoveredDevice] = []
        async with open_gateway(load_gateway()) as (link, rc):
            candidates = await collect_devices(link, window_s=3.0)
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
                    click.echo(f"  toggle failed: {exc}", err=True)
                    continue
                if yes or click.confirm(f"Was that {name}?"):
                    lights_mod.save_light(lights_path(), name, dev.address)
                    click.echo(f"saved: {name} = 0x{dev.address:02X}")
                    return
        click.echo("no device matched", err=True)
        raise SystemExit(1)

    asyncio.run(run())
```

(Import tidy: move `from . import ...` lines to the top of the file body where
they don't shadow the command's own module reference — `lights` the module vs.
`lights` the CLI function. Final layout: the CLI function is named
`lights_cmd` (already named that in Task 4); `learn` imports
`from . import lights as _lights_mod` at module top.)

- [ ] **Step 5: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: PASS (all suites)

- [ ] **Step 6: [HW] Discover and learn the real panel, with the user present**

1. `uv run rvcw discover` → devices with function names; paste the list into `docs/protocol-notes.md` § Panel inventory.
2. For each light worth naming: `uv run rvcw learn <name>` with the user confirming; verify in `~/.config/rvcw/lights.toml`.
3. `uv run rvcw send CQ --light <first-name>` end-to-end check (this is the product working).
4. Optionally `uv run rvcw lights` and a combo save for a multi-light test.

- [ ] **Step 7: Write `docs/protocol-notes.md` and update README**

`docs/protocol-notes.md` must contain (write from the extracts; keep it terse):
- BLE UUID map (from the vendored `const.py`), gateway identity, MTU 185 note.
- Unlock: UNLOCK_STATUS read → 4-byte seed → `calculate_can_ble_key_seed_key(seed, CAN_BLE_KEY_SEED_CIPHER)` → KEY write → verify `unlocked`/`\x01`; PASSWORD_UNLOCK PIN flow.
- LocalHost: 1.05 s claim listen, NETWORK claim (src 0xFF, payload `[addr, 0x12, mac6]`), online NETWORK from claimed src, then the 5-frame identity advertisement.
- REMOTE_CONTROL: session id 0x0004; preflight 0x40/0x41; 0x42 seed (BE) → `tea_encrypt(RC_CYPHER, seed)` → 0x43 key (BE); 0.5 s heartbeats (0x44); never 0x45-close right after relay commands (1.0.48 gotcha, RESPONSE 0x0E).
- Device commands: 0x82 COMMAND, target=device_id, 0x01/0x00 in message_data, empty payload, raw `[dlc][id4BE][payload]` writes to CAN_WRITE, confirmation via DEVICE_STATUS.
- Panel inventory (Task 12 Step 6 output).
- Phase-1 probe outcome (Task 10 Step 5 output).

README gains a Usage section:

```markdown
## Usage

Configure once (mode 600):

    mkdir -p ~/.config/rvcw && chmod 700 ~/.config/rvcw
    cat > ~/.config/rvcw/config.toml <<'EOF'
    [gateway]
    mac = "10:06:1C:E7:B8:3E"
    pin = "123456"
    wpm = 8
    EOF

Then:

    uv run rvcw discover              # find devices
    uv run rvcw learn awning          # map a name to a light
    uv run rvcw selftest --light awning
    uv run rvcw send "CQ CQ" --light awning
```

- [ ] **Step 8: Commit and push**

```bash
git add rvcw/ docs/ README.md
git commit -m "feat: discovery, guided learning, protocol notes and README

Co-Authored-By: Claude Code <noreply@anthropmic.com>"
git push
```

(Correct the trailer typo in that final commit command: `noreply@anthropic.com`.)

---

## Plan self-review notes

- Spec coverage: morse (T2), lights+combos (T3), CLI/dry-run+config (T4),
  transport/V2 decode (T5), unlock (T6), LocalHost (T7), RC session (T8),
  relay command + status derive (T9), phase-1 probe (T10), executor +
  off-guarantee + RTT honesty + selftest (T11), discovery/learn/docs (T12).
- The plan deliberately leaves TWO hardware-derived datapoints to be filled
  with real captured data (relay status byte mapping; phase-1 probe
  outcome). Those are marked [HW] with exact capture procedures, not
  placeholders for thinking.
- Task ordering is dependency-strict except T9 (sniff) which only needs T5's
  transport; it sits after T8 only because `set_relay` consumes `rc`.
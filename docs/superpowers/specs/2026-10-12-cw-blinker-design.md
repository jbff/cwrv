# CW (Morse) blinker for RV panel lights — design

Date: 2026-10-12
Repo: https://github.com/jbff/cwrv (public, `main`)
Status: approved in chat 2026-10-12; awaiting spec review

## Purpose

A standalone CLI (`rvcw`) that blinks CW/Morse messages on the RV's OneControl
panel lights by speaking the IDS-CAN BLE protocol directly — no HA in the send
path — while coexisting with the existing HA integration on the same BLE
connection.

Origin: `morse-rv-lights-briefing.md` (feasibility scan, 2026-10-04).
Panel: `LCIRemotezNS6KvsCb` (`10:06:1C:E7:B8:3E`, manufacturer_id 1177),
bonded to host `minivax` (this machine), gateway is an IDS-CAN BLE family
device. Source protocol code: `phurth/ha-onecontrol` v1.0.49 (MIT),
installed in incus container `has`.

## Decisions (agreed)

| Topic        | Decision |
|--------------|----------|
| Route        | Straight to standalone BLE/IDS-CAN (no HA-API interim) |
| Coexistence  | Multiplex with HA over host BlueZ D-Bus (same underlying GATT link); never kick HA off |
| Interface    | CLI tool (`rvcw send`, `rvcw lights`, `rvcw discover`, `rvcw selftest`, `rvcw probe-session`) |
| Build method | Approach A: extract-and-adapt the pure-Python protocol layer from ha-onecontrol (MIT — verified 2026-10-12 from the upstream GitHub repo; manifest lacks a license field, attribution note added regardless) |
| Defaults     | 8 WPM default, `--wpm` flag; TOML light mapping in `~/.config/rvcw/lights.toml` |

## Architecture

Python package `rvcw` (asyncio, `uv`-managed, Python 3.12+).

```
rvcw/
├─ cli.py          # click CLI — the only user-facing surface
├─ morse.py        # pure Morse engine: text → timed on/off schedule. No I/O.
├─ lights.py       # named lights/combos → CAN switch addresses; TOML load/save
├─ discovery.py    # enumerate switches; guided toggle-and-observe name mapping
└─ ble/
   ├─ transport.py # Bleak client over BlueZ D-Bus; CAN_WRITE write / DATA_READ notify
   ├─ can.py       # V1/V2 frame codec, CAN password unlock, source claiming
                   # (adapted from ids_can_wire.py)
   └─ session.py   # step-1 UNLOCK_STATUS→KEY, step-2 SEED→KEY (16-byte) TEA
                   # handshake, session state (ported from coordinator.py)
plus vendored protocol/ from ha-onecontrol: commands.py, events.py, cobs.py,
crc8.py, tea.py — imported as-is where practical, with MIT attribution
(NOTICE file), lightly normalized for import outside HA.
```

Boundary rule: `morse.py`/`lights.py` are hardware-free and BLE-free; `ble/`
knows nothing about Morse. The CLI composes them.

Known doc gap: `commands.py` references "INTERNALS.md § Command Building" —
check whether INTERNALS.md exists upstream; if not, reconstruct frame-building
notes from the code into `docs/protocol-notes.md` during implementation.

## Send flow

`rvcw send "CQ CQ DE W1AW" --light awning --wpm 10`

1. **Morse engine** (`morse.py`): ITU standard timing — dit = 1 unit,
   dah = 3, intra-character gap = 1, inter-character gap = 3, word gap = 7;
   unit = 1200/WPM ms. Output: an on/off schedule against the mapped light.
   Also supports prosigns (`AR`, `SK`, `BT`) and repeats (`--repeat`).
2. **Session** (`session.py`): reuse the authenticated gateway session or
   open one (multiplexed per Phase-1 findings).
3. **Executor**: walk the schedule ack-paced — each on/off step is a
   `CMD_ACTION_SWITCH` (0x40 frame) write awaited against the `0x02` ack
   event; the next element starts on ack, keeping timing honest against BLE
   latency. Combos expand to concurrent per-members schedules, each
   ack-paced.
4. **Restore**: final commands return every touched light to its pre-send
   state (see Safety).

Speed honesty: live ack round-trip is measured; if RTT exceeds ~60% of dit
length, the CLI downscales WPM (warningly) instead of garbling the message.
Expected envelope 8–15 WPM per the briefing's 50–150 ms element granularity.

## Light mapping

TOML at `~/.config/rvcw/lights.toml`:

```toml
[lights.awning]
address = "switch:0x2E41"        # addressing per prior-art func_id/counter notes

[combos.night_mode]
members = ["awning", "spots"]
```

`--light night_mode` expands combos. `rvcw lights` lists the current map.
`rvcw discover` enumerates switch entities from device metadata and runs a
guided toggle loop: flip one candidate, user confirms which physical light
it is, tool records name → address. Re-runnable anytime; unknown names fail
fast naming the file and key.

## Coexistence with HA (multiplexing)

Both HA's coordinator (in container `has`) and `rvcw` are Bleak clients over
the same host BlueZ; BlueZ dedupes them onto one GATT connection. The BLE
link is therefore not the risk — the IDS-CAN session (seed/key TEA auth,
LocalHost source claiming, REMOTE_CONTROL handling) may be single-owner.

**Phase 1 — session probe (first implementation milestone):**
`rvcw probe-session` opens an authenticated second session while HA stays
connected and reports the outcome. Outcomes feed phase 2:

- Second session works → straightforward multiplexed sending.
- Gateway refuses → document the failure mode, then design the fallback
  (coordinate with HA's coordinator to pause, e.g. disable the integration
  entry programmatically) **before** continuing; never silently contend.

`rvcw send` fails fast with a clear message if it cannot obtain a session.

## Safety

- **Off-guarantee:** every termination path (SIGINT, ack failure, reconnect
  failure, mid-message exception) restores the pre-send state of all mapped
  lights in a `finally`/signal-handler path.
- **Ack timeout:** one write retry, then clean abort with off-guarantee.
- **Reconnect:** mid-message BLE loss → Bleak retry reconnect + re-auth +
  resume remaining schedule once; then abort with off-guarantee.
- **Fail-fast config errors** name the offending key and file.

## Testing

- **Unit (no hardware):** Morse golden timings ("A" pattern at N WPM in ms);
  frame codec against vectors from the integration; TOML load/combo expand;
  `rvcw send --dry-run` prints the element schedule + addresses without BLE.
- **`rvcw selftest`:** blinks "R" at 5 WPM on one light — validates auth,
  ack pacing, off-safety against the real panel; run before trusting longer
  messages.
- **Phase-1 probe** is itself scripted (`rvcw probe-session`).

## Repository & licensing

- Public repo `jbff/cwrv` (done; briefing + README pushed).
- Extracted ha-onecontrol code is MIT; include `NOTICE` (or retained license
  headers) crediting `phurth/ha-onecontrol` in the vendored files. Our own
  code ships MIT too. Manifest license field was absent; upstream repo states
  MIT (verified 2026-10-12).
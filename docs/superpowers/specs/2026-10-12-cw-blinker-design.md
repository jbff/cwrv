# CW (Morse) blinker for RV panel lights — design

Date: 2026-10-12; **route amended 2026-10-13 — legacy MyRvLink COBS** (see "Route amendment")
Repo: https://github.com/jbff/cwrv (public, `main`)
Status: implemented (IDS-CAN build merged); legacy-COBS re-route in progress

## Route amendment (2026-10-13, from live GATT evidence)

Full stable GATT enumeration (3 fresh connections) shows this gateway —
`10:06:1C:E7:B8:3E', HA config entry `gateway_family: "legacy"` — has NO
IDS-CAN characteristics (no CAN_WRITE 00000001 / CAN_READ 00000002 /
CAN_VERSION / PASSWORD_UNLOCK). What it exposes:

- auth service `00000010-0200-a58e-e411-afe28044e62c`: UNLOCK_STATUS `0x12`
  (read), SEED `0x11` (read+notify), `0x14` (read+notify), KEY `0x13` (write)
- data service `00000030-…`: data write `0x33` (write), DATA_READ `0x34`
  (read+notify), probe char `0x31` (read)

This is the **MyRvLink COBS family**: `CMD_ACTION_SWITCH` (0x40) /
`CMD_ACTION_DIMMABLE` (0x43) frames, COBS-encoded, written to the data-write
char; events stream back on DATA_READ (`0x02` DEVICE_COMMAND metadata,
`0x05`/`0x06` RelayBasicLatching = the relay feedback for state-confirmed
pacing); auth = step-1 UNLOCK_STATUS→KEY + step-2 SEED→16-byte
KEY (`calculate_step2_key(seed, gateway_pin)`) — already vendored.

The IDS-CAN layers (LocalHost claim, REMOTE_CONTROL sessions, 0x82 commands)
are inert for this device and retained for other gateway families. The
executor, off-guarantee, morse engine, lights/combos mapping, and CLI are
transport-agnostic and carry over unchanged. The briefing's "GATT discovery
missing CAN_WRITE" note is hereby re-interpreted: not an enumeration
quirk — the characteristics do not exist on this gateway.

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
├─ lights.py       # named lights/combos → CAN device addresses; TOML load/save
├─ executor.py     # walks the schedule against the live gateway (pacing, retries,
│                  # off-guarantee, WPM honesty)
├─ discovery.py    # enumerate devices; guided toggle-and-observe name mapping; sniff
└─ ble/
   ├─ transport.py # GatewayLink: Bleak client, MTU 185, CAN_WRITE write / CAN_READ
   │               # notify; V2 notification decoding (Packed/ElevenBit/TwentyNineBit)
   ├─ can.py       # LocalHost address claim + identity advertisement; relay COMMAND
   │               # 0x82 frame composition; DEVICE_STATUS relay-state parsing
   └─ session.py   # unlock (key/seed via UNLOCK_STATUS/KEY, GATT PASSWORD_UNLOCK PIN)
                   # + per-device REMOTE_CONTROL session (preflight, 0x42/0x43
                   # seed/key, 0.5 s heartbeat, close)
plus vendored protocol/ from ha-onecontrol: commands.py, events.py, cobs.py,
crc8.py, tea.py, ids_can_wire.py, function_names.py, const.py — imported
as-is (except import fixes), under NOTICE/retained headers (MIT).
```

Boundary rule: `morse.py`/`lights.py` are hardware-free and BLE-free; `ble/`
knows nothing about Morse. The CLI composes them.

- `docs/superpowers/specs/` — this spec (plus plans under `plans/`).

Config files (never committed): `~/.config/rvcw/config.toml` —
`[gateway] mac = "10:06:1C:E7:B8:3E"`, `pin = "<6-digit>"` (PIN stored
mode 600; also discoverable from HA's config-entry storage in container
`has`), default WPM.

## Send flow

`rvcw send "CQ CQ DE W1AW" --light awning --wpm 10`

1. **Morse engine** (`morse.py`): ITU standard timing — dit = 1 unit,
   dah = 3, intra-character gap = 1, inter-character gap = 3, word gap = 7;
   unit = 1200/WPM ms. Output: an on/off schedule against the mapped light.
   Also supports prosigns in `<AR>`/`<SK>`/`<BT>` form and repeats (`--repeat`).
2. **Session**: reuse or open the gateway path — key/seed GATT unlock
   (UNLOCK_STATUS → TEA `CAN_BLE_KEY_SEED_CIPHER` → KEY), PASSWORD_UNLOCK
   PIN if the gate reports locked, LocalHost address claim, then a
   per-device REMOTE_CONTROL session with 0.5 s heartbeat (open once per
   sending run, per Phase-1 findings on coexistence).
3. **Executor**: walk the schedule **clock-scheduled with confirmation
   supervision**. Each step is written at its scheduled wall-clock time
   (relay COMMAND 0x82 frame, `message_data` 0x01/0x00, empty payload,
   raw `[dlc][id4][payload]` to CAN_WRITE); the DEVICE_STATUS feedback
   stream then confirms the relay reached the commanded state within the
   element's window. Source had no dedicated ack event — 0x02 frames are
   metadata responses — so state-confirmation, not ack-events, is the
   pacing verifier. A missed confirmation retries ONCE within the element;
   two consecutive misses abort with the off-guarantee.
4. **Restore**: final commands return every touched light to its pre-send
   state (see Safety).

Speed honesty (amended 2026-10-12 after final review): the relay-command round
trip is measured live during each sending run; when the commanded WPM exceeds
the measured RTT envelope (~60% of dit length), the CLI prints a warning with
the suggested max WPM. In v1 this warning is advisory and arrives AFTER the
run — a first-send auto-downscale is impossible without a pre-measured RTT
(chicken-and-egg), and no RTT state is retained between runs yet. Once the
sniff capture (Task 9 Step 5) populates real RTT data, auto-downscale from the
stored envelope becomes a follow-up. Expected envelope 8–15 WPM given
BLE + status-feedback latency.

## Light mapping and what "a light" is

On the IDS-CAN path, relays/latched panel switches are commanded by
**device address** (the device's bus source address; `table_id` is dropped):
0x82 COMMAND frame, `target_address=device_id`, `message_data` 0x01=on /
0x00=off, empty payload. Dimmable/RGB light entities have no
CAN-BLE direct path in the source (they go via the legacy COBS data
service, out of scope for v1) — v1 blinks on/off-capable relay targets.

TOML at `~/.config/rvcw/lights.toml`:

```toml
[lights.awning]
device_id = 0x2E41              # IDS-CAN bus source address of the relay device

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
link is therefore not the risk — two independent IDS-CAN participant
identities are: each client claims its own LocalHost address (distinct,
chosen from unobserved addresses) and opens its own per-device
REMOTE_CONTROL session. Source facts: REMOTE_CONTROL session ID is 4
(0x0004) and is per-device, kept alive by 0.5 s heartbeats; sessions stay
owned until explicitly closed (0x45) or the BLE link drops — **but closing
a session right after relay commands was tried upstream (1.0.48) and
reverted (devices rejected later commands, reason 0x0E)**, so rvcw leaves
its session open across sends and closes it only on explicit
`rvcw session --end` or link teardown.

**Phase 1 — session probe (first implementation milestone):**
`rvcw probe-session` connects alongside HA, unlocks, claims its LocalHost,
opens a REMOTE_CONTROL session with one relay device while HA is connected,
reports the outcome, and leaves the bus as it found it. Outcomes feed
phase 2:

- Second participant works → straightforward multiplexed sending.
- Gateway refuses → document the failure mode, then design the fallback
  (coordinate with HA's coordinator to pause, e.g. disable the integration
  entry programmatically) **before** continuing; never silently contend.

`rvcw send` fails fast with a clear message if it cannot obtain a session.

## Reality notes (from source extracts, 2026-10-12)

- No dedicated ack event exists; `0x02` frames are metadata responses
  correlated by 16-bit command id. Pacing relies on DEVICE_STATUS feedback.
- No `INTERNALS.md` is shipped (referenced by docstrings only); rvcw keeps
  its own `docs/protocol-notes.md` from the extracts.
- Key derivation is pure 32-round TEA (no hashing) over a 4-byte seed;
  CRC-8 uses the integration's custom table (init 0x55, poly 0x07).
- Our gateway is the 24955 ("V1"-behaving) variant — it requires the
  explicit REMOTE_CONTROL seed/key exchange even though the version char
  says V1.
- DEVICE_STATUS/relay-state payload parsing on the CAN-BLE path is not
  fully documented in the source; it is derived from live captures
  (`rvcw sniff` toggling while HA flips a switch) in the plan, with a
  time-only pacing fallback if status parsing proves unreliable.

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
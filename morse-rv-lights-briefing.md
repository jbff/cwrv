# Morse-code blinker on RV panel lights — feasibility briefing

> Hand this to a fresh agent working in an empty repo. Written 2026-10-04 after a
> feasibility scan of the `ha_onecontrol` integration (v1.0.49, `phurth/ha-onecontrol`,
> installed in incus container `has` at
> `/var/lib/homeassistant/custom_components/ha_onecontrol/`). User goal: blink
> morse messages on RV panel lights from standalone code.

## Verdict: feasible via three routes

1. **HA API route (no protocol needed):** toggle the HA `light` entities via
   REST/WebSocket with timed on/off. ~100 lines python. Works now. Timing
   granularity ≈ 50–150 ms/element (BLE + API overhead) → good for ~5–10 WPM.
2. **Standalone BLE (protocol extraction) — the recommended route here:** the
   gateway is IDS-CAN BLE (see Panel identity below); lift `ids_can_wire.py`
   + `protocol/` + the key/seed CAN unlock flow from the coordinator and
   drive CAN_WRITE/CAN_READ directly via host BlueZ (bond exists). Crisp
   ack-based pacing. ~a weekend.
3. ~~**WiFi/TCP route:**~~ **ruled out** — the gateway broadcasts no WiFi AP
   (confirmed on-site 2026-10-04).

## What to extract from ha-onecontrol

- `custom_components/ha_onecontrol/protocol/`: `commands.py` (light switch =
  `CMD_ACTION_SWITCH 0x40`, dimmable `0x43`, RGB `0x44`; frames are
  `[CmdId_L][CmdId_M][CmdType][payload]` with a 16-bit LE command id for ack
  correlation), `events.py` (ack event type `0x02`), `cobs.py`, `crc8.py`
  (custom table), `tea.py` (auth cipher), `ids_can_wire.py`,
  `advertisement.py`. All pure-Python, no HA imports.
- `ble_agent.py`: BlueZ PIN/pairing agent (auto-accept PIN authorizations).
- The connect handshake (seed/key TEA exchange, `DATA_READ`/`SEED`
  subscribe) lives in `coordinator.py` (~224 KB; grep for SEED / DATA_READ /
  establish_session). The live device is `LCIRemotezNS6KvsCb`
  (`10:06:1C:E7:B8:3E`, manufacturer_id 1177).
- **Panel identity (confirmed 2026-10-04 on-site)**: the gateway is an
  **IDS-CAN BLE family** device — bonded to host `hci0` (`Paired: yes`,
  `Bonded: yes`, `Trusted: yes`, connects fine). GATT services:
  `00000010-0200-a58e-e411-afe28044e62c` (key/seed auth; seed char is
  `00000011-...`, matches boot log "Subscribed to SEED (0x0011)"),
  `00000030-0200-...` (data service — CAN_WRITE/CAN_READ; boot log shows
  "Subscribed to DATA_READ (0x0034)" and "GATT discovery missing
  CAN_WRITE"), plus `00000041-0200-...` (manufacturer/service gate), GAP,
  GATT, Device Information. Transport = CAN wire frames over
  CAN_WRITE/CAN_READ (V1/V2 notification decoding, CAN password unlock,
  LocalHost source claiming, REMOTE_CONTROL session handling — the 1.0.48/
  1.0.49 commits). So the agent should model the IDS-CAN BLE path, not the
  MyRvLink COBS data path (though cobs/crc8/tea still apply to auth).
- The panel does **not** broadcast a WiFi AP — the WiFi/TCP route
  (`manos/OneControl-RV-C-Protocol`, `MyRV_*` AP) is ruled out for this
  gateway; routes 1 (HA API) and 2 (BLE direct) remain.
- Protocol documentation: `docs/TECH_SPEC.md` in the repo (gateway families,
  pairing models, frame format). Note the file `commands.py` references
  "INTERNALS.md § Command Building" — check whether that doc exists upstream.

## Prior art

- `manos/OneControl-RV-C-Protocol` (MIT, Python): full WiFi/TCP client,
  `docs/PROTOCOL.md` + `docs/ANALYSIS.md`; `func_id` vs `counter` addressing.
- `rv_onecontrol` (Rust, docs.rs): IDS-CAN BLE session layer recovered from
  static app analysis; NOTE: detects but cannot command the newer "MyRvLink"
  gateway generation.
- Wesley M. Ray's app-decompile writeup (LinkedIn): modified TEA on a separate
  GATT service for auth; bespoke CRC-8 table; enum endianness gotcha.

## Gotchas

- **Single BLE connection**: while HA holds the panel, a second BLE client is
  refused (same behavior broke the SP548E strip polling; see
  `notes/home-assistant-setup.md` for the incident log). Options: release the
  HA integration first, or multiplex GATT access through the host BlueZ D-Bus.
- **Bonding**: the panel is already bonded to the host adapter (hci0, name
  `minivax`) via HA; a same-host client through BlueZ inherits the bond. On a
  DIFFERENT host you'd need the PIN (6-digit; integration supports PIN-based
  pairing, `ble_agent.py` auto-accepts).
- 15+ WPM timing will jitter on the HA-API route; prefer routes 2/3 for crisp
  morse.
- Don't run the morse script against a gateway the HA integration is bonded to
  concurrently from another host without releasing one side.
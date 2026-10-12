# rvcw protocol notes

Terse working notes for the OneControl gateway BLE/IDS-CAN protocol as used by
rvcw. Source of truth: the vendored onecontrol protocol layer (`rvcw/protocol/`,
from [phurth/ha-onecontrol](https://github.com/phurth/ha-onecontrol), MIT) and
the rvcw modules named in each section. All CAN frames below are **CAN adapter
wire frames** as written to CAN_WRITE / decoded from CAN_READ.

## BLE UUID map (`rvcw/protocol/const.py`)

Every UUID is `0000xxxx` + the base `-0200-a58e-e411-afe28044e62c`.

| Char/service             | Prefix | Notes                       |
|--------------------------|--------|-----------------------------|
| AUTH service             | 0x0010 | unlock/key exchange service |
| SEED                     | 0x0011 | 4-byte unlock seed (read)   |
| UNLOCK_STATUS            | 0x0012 | `unlocked` or seed status   |
| KEY                      | 0x0013 | unlock key write            |
| DATA service             | 0x0030 | COBS/status side channel    |
| DATA_WRITE / DATA_READ   | 0x0033 / 0x0034 | COBS event frames  |
| CAN_WRITE                | 0x0001 | raw IDS-CAN wire frames out |
| CAN_READ                 | 0x0002 | notify: V2 frames in        |
| CAN_VERSION              | 0x0004 | version string read         |
| PASSWORD_UNLOCK          | 0x0005 | PIN gate                    |
| X180T_DISCOVERY service  | 0x000f | X180T-family discovery      |

Gateway identity: Lippert OneControl panel connected to the vehicle bus,
advertises name prefix `LCIRemote`, manufacturer data ids `0x0499` (Lippert
Components) and `0x05C7` (LCI X4T2 / PIN-based gateway variant). The factory
default PIN is `000000` (documented sticker default in the vendored `const.py`,
not a secret).

MTU: request `185` on connect (`BLE_MTU_SIZE = 185`; large enough for the
biggest V2 packed notification). The request is best-effort — some stacks
report their negotiated MTU lower and everything still frames correctly.

## Unlock flow (`rvcw/ble/session.py: unlock_gateway`)

1. Read UNLOCK_STATUS: ASCII `unlocked` (case-insensitive) means done;
   otherwise expect a 4-byte seed.
2. `key = calculate_can_ble_key_seed_key(seed, CAN_BLE_KEY_SEED_CIPHER)` — a
   TEA-family cipher with a proprietary schedule implemented in
   `rvcw/protocol/tea.py`.
3. Write the key to KEY_CHAR (`response=True`), sleep 0.5 s
   (`UNLOCK_VERIFY_DELAY`), re-read UNLOCK_STATUS; success is `unlocked` or
   the single byte `\x01`.
4. PASSWORD_UNLOCK gate: read the char; a clear first byte (`0x00`) with the
   gate still locked means write the PIN (UTF-8 bytes) and re-read after 1.0 s,
   up to 2 attempts, expecting a non-zero first byte. A missing/unreadable
   PASSWORD_UNLOCK char is treated as "no gate here" (upstream parity).

## LocalHost flow (`rvcw/ble/can.py: claim_local_host`)

- Pseudo-MAC: `sha256("rvcw:<gateway mac>")[:6]` (all-zero result gets
  `0x01` in byte 0) — mirrors upstream CanAdapterFactory's stable participant
  identity.
- Address claim: free candidates are 0x01..0xFE minus observed participants;
  the start index is deterministic from `sha256(mac + "rvcw")`; never
  0x00/0xFF; fallback 0xFA only when the bus looks full.
- Sequence: 1.05 s silent listen (the official ~1 s claim window), then the
  claim broadcast — NETWORK (type 0x00) with source 0xFF (broadcast) and
  payload `[claimed_addr, protocol_version 0x12, mac6]`; wait another 1.05 s,
  then advertise the claimed address with 5 frames 40 ms apart: NETWORK
  online (from the claimed source), PRODUCT_STATUS, DEVICE_ID (product 46 /
  device type 22 / function name 2 = OneControl Android app), CIRCUIT_ID,
  DEVICE_STATUS (empty).

## REMOTE_CONTROL flow (`rvcw/ble/session.py: RemoteControl`)

- Session id `0x0004` (`sid2` = `00 04` BE); one session per target device at
  a time; the handler for RESPONSE (type 0x81) frames must be registered on
  `link.on_frame` before the first request is written.
- `ensure(target)`: preflight REQUEST 0x40 (`00 00`, then `00 01`) and
  REQUEST 0x41 `[sid2]` 50 ms apart plus a 0.20 s settle; then seed/key:
  - REQUEST 0x42 `[sid2]` → RESPONSE 0x42 `[sid2, seed4 BE]` (0.45 s timeout)
    → `key = tea_encrypt(RC_CYPHER, seed)`;
  - REQUEST 0x43 `[sid2, key4 BE]` → RESPONSE 0x43 echoes `[sid2]`
    (3 s timeout).
- Heartbeat: REQUEST 0x44 `[sid2]` every 0.5 s. An 0x44 ACK is **not** a
  session close; a device-initiated close arrives as a RESPONSE 0x45.
- Rejections arrive as RESPONSE 0x81 with the echoed request code and a
  single-byte IDS response status in the payload (e.g. 0x0D
  `KEY_NOT_CORRECT` for forged/derived keys); these propagate to the pending
  request future as `RemoteControlSessionError`.
- Gotcha (firmware 1.0.48): never send 0x45 SESSION_END right after relay
  COMMAND frames — the gateway answers RESPONSE 0x0E SESSION_NOT_OPEN and the
  relay command path misbehaves. rvcw's `aclose()` therefore cancels the
  heartbeat **without** 0x45 and lets the session drop with the BLE link.

## Device-command flow (`rvcw/ble/can.py: relay_command`)

- COMMAND (type 0x82) as a 29-bit extended wire frame: raw
  `[dlc][id4 BE][payload]` written to CAN_WRITE; `id4` packs
  type 0x82 | source (claimed LocalHost) | target = device_id | message_data
  0x01 (on) / 0x00 (off); payload is empty (RelayBasicLatching Type2).
- Confirmation: watch DEVICE_STATUS (0x03) frames from the target;
  `GatewayLink.relay_state_from_status` maps the payload to relay on/off.
  `confirm_relay` polls that mapping until the deadline, sends one idempotent
  rewrite on a miss, then raises `RelayConfirmError`. The status mapping is a
  two-hardware-datapoint derivation still pending capture (§ Panel inventory
  notes below and Task 9).
- Pacing: latching relay commands may be repeated safely; writes stay outside
  any 0x45-close window (see REMOTE_CONTROL gotcha above).

## Device discovery (`rvcw/discovery.py`)

- From the claimed LocalHost: broadcast REQUEST (type 0x80) with message_data
  0x02 ("DEVICE_ID request"), target 0xFF, empty payload.
- Devices answer with DEVICE_ID frames (type 0x02, 11-bit wire form
  `[dlc][id2][payload]`, `payload` 7–8 bytes): product_id `payload[0:2]` BE,
  product_instance `payload[2]`, device_type `payload[3]`, function_name
  `payload[4:6]` BE, device_instance/function_instance as high/low nibble of
  `payload[6]`, optional device_capabilities `payload[7]`. FUNCTION_NAMES
  (vendored) translates the 16-bit function id into labels.
- `collect_devices` keeps a chain-wrapped `on_frame` hook for a window
  (default 3 s), dedupes by source address, sorts by address and restores the
  previous hook (also on error).

## Panel inventory

(To be recorded at the on-site HW session.)

## Phase-1 probe outcome

(To be recorded at the on-site HW session.)

## lights.toml writer note (Task 3 correction)

`lights.toml` values must survive a TOML round-trip as unsigned hex integers
(`device_id = 0x2E41`), so `_write` in `rvcw/lights.py` emits the file as raw
TOML text instead of using tomli_w. Correction to the Task 3 commit message,
per review: the claim there that tomli_w "renders nested per-entry dicts as
flat tables instead of `[lights.NAME]` sub-tables" is **false** — tomli_w
emits sub-tables correctly. The real, verified limitation is only the hex
form: `tomli_w.dumps` writes a `"0x2E41"` value as a quoted string (rejected
by the loader) and a plain int as decimal — never as `0x…`. The hex-integer
requirement alone justifies the raw-text writer; tomli-w was removed from
`pyproject.toml` dependencies in Task 12 (unused by shipped code).

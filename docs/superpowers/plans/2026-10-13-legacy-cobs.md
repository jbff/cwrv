# rvcw Legacy-COBS Re-route Implementation Plan (addendum to 2026-10-12 plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task. Steps use checkbox syntax.

**Goal:** Re-route rvcw's transport for THIS gateway (legacy MyRvLink COBS family — no CAN characteristics) from the IDS-CAN CAN_WRITE path to the data-service COBS path, keeping the morse engine, executor semantics (clock pacing, confirm supervision, off-guarantee), lights/combos mapping, and CLI surface intact.

**Architecture:** Two changes only. (1) `GatewayLink` grows the data-path methods (step-1/step-2 TEA auth, DATA_READ COBS stream → relay/gateway-info state, 5 s GetDevices keepalive). (2) The executor's CAN-specific `RemoteControl` controller gets a sibling `CobsRelayLink` behind a shared `RelayController` protocol; `send_steps` now addresses `LightTarget(table_id, device_id)` instead of bare device ids, and the CLI/lights/discovery/learn adapt to (table, device) addressing.

**Tech Stack:** unchanged (bleak, click, uv, pytest-asyncio).

**Spec:** `docs/superpowers/specs/2026-10-12-cw-blinker-design.md` § Route amendment (2026-10-13). Source-of-truth extracts: ha_onecontrol coordinator.py 3778-3818 (step-1), 3859-3886 (step-2), 3824-3836 (subscribe order DATA_READ→SEED), 4036-4095 + 1222-1234 (GetDevices keepalive 5 s), 642-651 (cobs_encode → DATA_WRITE response=False), 4408-4443 (RelayStatus state), events.py 359-375 (parse_relay_status: `is_on = (status_byte & 0x0F) == 0x01`), 316-326 (parse_gateway_information: table_id at data[4]), 688-787 (metadata — NOT needed for v1), commands.py 93-107 (build_action_switch), 52-62 (build_get_devices). KEY FACT: switch commands are fire-and-forget upstream; the ONLY confirmation is the RelayStatus echo. Auth: legacy PIN gateways must NOT call BLE pair().

## Global Constraints

- All of the 2026-10-12 plan's Global Constraints still apply (no HA imports, response=False writes, MIT attribution, commit trailer, trailing newlines, full suite green before commit).
- Family handling: the CAN layers stay in the tree UNTOUCHED (they are inert for this device but must keep passing their existing tests). New code must not modify `rvcw/ble/session.py` or `rvcw/ble/can.py` except where a signature shared with the executor changes — and then the CAN implementations must be adapted, not deleted.
- `connect()` (both families) keeps the enumeration-retry + `bluetoothctl disconnect` recovery introduced at commits 36712af/c065ce1. Required chars for the data path: UNLOCK_STATUS `00000012-…`, KEY `00000013-…`, DATA_WRITE `00000033-…`, DATA_READ `00000034-…` (SEED `00000011-…` optional-but-expected; warn if missing).
- Auth order (upstream verbatim): step-1 (`calculate_step1_key(challenge)` BIG-endian, KEY write response=False, 0.5 s, verify "unlocked") → 0.2 s → subscribe DATA_READ → subscribe SEED → step-2 on SEED notify (`calculate_step2_key(seed, pin)` → 16-byte KEY write response=False). A legacy gateway whose pairing config is `push_button`/`pin` NEVER uses BLE SMP pairing.
- Relay-state keying: `link.relay_states: dict[tuple[int, int], bool]` keyed `(table_id, device_id)` for the data path. The CAN code's int-keyed usage moves to the CAN controller wrapper; the executor and lights use `LightTarget`/tuple keys exclusively.
- Keepalive: GetDevices every 5 s once `gateway_info.table_id` is known (gateway pushes event 0x01; wait up to 10 s for it after connect, else warn and continue without keepalive).
- Off-guarantee semantics unchanged (restore pre-state, unknown → OFF); pacing semantics unchanged.

## Review Focus

1. **SEED/step-2 race**: step-2 must be able to run while DATA_READ streams; a SEED arriving before DATA_READ subscription is fine (subscription order DATA_READ→SEED matches upstream). Test pins that a SEED handler fires the KEY write with `calculate_step2_key`.
2. **Table_id gating**: with no GatewayInformation event, commands must fail fast ("no gateway table — retry when the panel announces"), never send `table=0` blindly. (Test pins.)
3. **RelayStatus status byte**: only `(status & 0x0F) == 0x01` is ON — a status byte like 0x11 (faulted) is NOT "on" for pacing. (Test pins via vendored parser.)
4. **Lights TOML migration**: old `[lights.NAME] device_id = …` entries must fail with an explicit "re-run learn" message, never silently map a CAN device id onto the COBS path. (Test pins.)
5. **Combo bulk-write**: the legacy `build_action_switch` supports multiple device_ids in ONE frame — the executor may exploit this per element, but the confirm supervision must remain per-target. (Test pins.)

---

### Task 13: Data-path transport (auth, events, keepalive)

**Files:**
- Modify: `rvcw/ble/transport.py` (GateLink data-path methods; family split)
- Modify: `rvcw/ble/__init__.py` (exports)
- Test: `tests/test_data_path.py`

**Interfaces:**
- Consumes (vendored): `CobsByteDecoder`, `cobs_encode`, `calculate_step1_key`, `calculate_step2_key`, `parse_relay_status`, `parse_gateway_information`, `parse_event`, char UUIDs incl. `DATA_WRITE_CHAR_UUID`, `DATA_READ_CHAR_UUID`, `SEED_CHAR_UUID`, `UNLOCK_STATUS_CHAR_UUID`, `KEY_CHAR_UUID`.
- Produces (all on `GatewayLink — no new class):
  - `family: str` property — `"data"` when the required data-path chars are present in the discovered services, `"can"` if `CAN_WRITE_CHAR_UUID` is present, else `LinkError` at connect.
  - `connect()` — same retry strategy as today, but required-charset by detected family (`data`: the 4 data-path chars; `can`: CAN_READ/CAN_WRITE as today).
  - `async def authenticate_data(self, pin: str | None) -> None` — verbatim upstream step-1 (already-"unlocked" short-circuit; 4-byte challenge; all-zero challenge → GatewayAuthError "gateway not ready"; KEY write response=False; 0.5 s; verify). Installs the DATA_READ notify (CobsByteDecoder byte-stream → `self._on_data_frame`) and SEED notify (→ step-2). Raises `GatewayAuthError` (= existing `GatewayUnlockError` type) naming the config path if the gate stays locked and `pin` is None.
  - `async def _authenticate_step2(self, seed: bytes) -> None` — `key = calculate_step2_key(seed, self._pin)` → KEY write. Seed of wrong size → warn, continue.
  - `gateway_info: GatewayInformation | None`; `async def wait_gateway_info(self, timeout: float = 10.0) -> int` — resolves table_id from event 0x01; raises `LinkError("no gateway table announced …")` on timeout.
  - `_on_data_frame(self, frame: bytes)` — updates `gateway_info` (parse_gateway_information), `self.relay_states[(t, d)] = is_on` via parse_relay_status (guard: only when `event.device_id` valid — mirror upstream `is_valid_device_id` semantics: nonzero), leaves other events to `self.on_frame`.
  - `async def send_command(self, raw: bytes) -> None` — `cobs_encode` → DATA_WRITE `response=False`.
  - `async def start_keepalive(self) -> None` / internal task: every 5 s `send_command(build_get_devices(table_id))` once gateway_info known; stops on link loss. Uses vendored `CommandBuilder` from `rvcw.protocol.commands`.
  - `relay_states: dict[tuple[int, int], bool]` — CHANGED key type for the data path. The CAN-path code (session.py heartbeat, can.py confirm) must keep working: move ALL CAN relay-state accesses through the CAN controller wrapper (Task 14) — if session.py/can.py read `link.relay_states` directly today, adapt them to a `link.can_relay_states: dict[int, bool]` view maintained ONLY by the CAN notification path so the two families never share a dict.

- [ ] **Step 1: Write the failing tests** (`tests/test_data_path.py`) covering: step-1 happy path (challenge → vendored key written, response=False) and already-unlocked short-circuit; all-zero challenge error; step-2 SEED→KEY write with `calculate_step2_key(seed, pin)`; `_on_data_frame` storing `relay_states[(0x01, 0x2E)] = True` for a hand-built relay frame `bytes([0x05, 1, 0x2E, 0x01, 0x00])` and treating `bytes([0x05, 1, 0x2E, 0x11, 0x00])` as NOT on; `gateway_info` update from a 0x01 frame with table_id 2; `send_command` COBS-wrapping onto the fake client; `wait_gateway_info` timeout → LinkError. Use a fake BleakClient-style double like the Task 6 tests (FakeClient/FakeLink pattern) — no real BLE.
- [ ] **Step 2: red run.** Expected FAIL — missing attributes.
- [ ] **Step 3: implement** per the Interfaces above; keep the CAN tests green (family split must not break `tests/test_transport.py`, `tests/test_unlock.py`, `tests/test_remote_control.py`).
- [ ] **Step 4: green + full suite.**
- [ ] **Step 5: commit** `feat: data-path transport (step-1/step-2 auth, COBS events, keepalive)` + push.

---

### Task 14: RelayController unification + (table, device) addressing + CLI

**Files:**
- Create: `rvcw/ble/controller.py` (`RelayController` protocol, `CobsRelayLink`, `CanRelayLink` wrapper)
- Modify: `rvcw/executor.py` (`send_steps` targets), `rvcw/lights.py` (`LightTarget`), `rvcw/discovery.py` (data-path discovery), `rvcw/cli.py` (rewire send/selftest/status/sniff/discover/learn), `rvcw/ble/session.py` + `rvcw/ble/can.py` (CAN relay-state accesses move to the wrapper — adapt, don't delete)
- Test: `tests/test_controller.py` (extend `tests/test_executor.py`, `tests/test_cli.py`, `tests/test_lights.py`)

**Interfaces:**
- Consumes: Task 13's data-path `GatewayLink`, vendored `CommandBuilder.build_action_switch`, `build_get_devices`; existing CAN machinery (`RemoteControl`, `set_relay`, `relay_command`).
- Produces EXACTLY:
  - `@dataclass(frozen=True) class LightTarget: table_id: int; device_id: int` with `key -> tuple[int, int]` property. `rvcw/lights.py`: `LightEntry(name, table_id, device_id)`; TOML `[lights.NAME]` requires BOTH `table = 0x…` and `device = 0x…` — the old `device_id = …` key raises `ValueError("light 'NAME' uses the removed device_id key — re-run learn for the COBS path")` at load time (Review Focus 4). Combos unchanged. `save_light(path, name, table_id, device_id)`.
  - `class RelayController` (protocol): `async ensure(target) -> bool`; `async write(target, on) -> None` (write ONLY — the executor owns pacing); `async confirm(target, on, timeout) -> bool`; `async aclose() -> None`.
  - `CobsRelayLink(link: GatewayLink)`: `ensure` = auth/link check (`True`); `write` = `build_action_switch(table_id, on, [device_id])` via one shared `CommandBuilder` instance → `link.send_command` (Review Focus 5: combos may pass multiple device_ids in one write — the executor does per-target confirmations regardless); `confirm` = poll `link.relay_states[(t, d)]` every 50 ms to deadline, final check (`state is on`), mirrors today's `confirm_relay`.
  - `CanRelayLink(link: GatewayLink, rc: RemoteControl)`: delegates to the existing CAN path (`ensure`→`rc.ensure(device_id)`; `write`→`relay_command`+CAN_WRITE; `confirm`→the CAN relay-state view); the CAN relay-state dict int-views stay maintained by the transport notification path (family split).
  - `rvcw/executor.py`: `send_steps(link, controller: RelayController, targets: list[LightTarget], steps, *, confirm=True, confirm_timeout=2.0, config: GatewayConfig) -> SendStats` — identical pacing/supervision/off-guarantee; pre-state/restore keyed by `target.key`; reconnect uses `reconnect_gateway` + re-ensure per target (CAN controller re-`ensure`s its RC sessions too).
  - `rvcw/cli.py`: `send`/`selftest` build `LightTarget`s from the mapping; `sniff` prints decoded COBS events (relay/gateway-info highlighted); `discover` = connect+auth → `wait_gateway_info` → one `build_get_devices(table)` broadcast → collect `relay_states` for 3 s → print `(table, device, is_on)` rows; `learn NAME` = discovery as above, then per candidate: OFF first (defensive), ON, 0.6 s dwell in try/finally-OFF (the fix-wave pattern), confirm prompt, `save_light(path, name, table_id, device_id)` — candidates come from `relay_states` keys; `probe-session` re-purposed to print family detection only (CAN probe kept for CAN-family devices, data family prints "legacy gateway — COBS path, no sessions needed").
- [ ] **Step 1: failing tests** — Controller protocol behavior for both families (fakes), executor retarget test (two targets incl. combo via one CobsRelayLink write), lights migration rejection, CLI `send --dry-run` with new addressing, `discover`/`learn` on fake links.
- [ ] **Step 2: red.** **Step 3: implement.** **Step 4: green + full suite.** **Step 5: commit** `feat: RelayController abstraction + (table, device) addressing for the COBS path` + push.

---

### Task 15: On-site hardware session (controller-orchestrated; no code)

**Steps (user present):**
1. `uv run rvcw sniff` — verify GatewayInformation + RelayStatus frames stream (light toggled in HA appears as `relay_state` changes).
2. `uv run rvcw discover` — relay list printed; paste into docs/protocol-notes.md § Panel inventory.
3. `uv run rvcw learn <name>` per light; `uv run rvcw lights` to verify TOML.
4. `uv run rvcw selftest --light <name>` — verify blink + Ctrl-C restore (Review Focus 1 of the parent plan).
5. `uv run rvcw send "CQ CQ" --light <name>` — end-to-end.
6. Update docs/protocol-notes.md (legacy family section: auth flow observed, relay statuses, inventory, RTT observations).

## Plan self-review notes

- Spec coverage: transport family split + auth (13), addressing + controller + CLI (14), HW (15). The morse engine, lights combos, off-guarantee, pacing semantics are untouched by construction.
- Known simplifications (rulings carried): metadata/GetDevicesMetadata machinery NOT ported (discovery uses RelayStatus listing; function names deferred); cmdId correlation for switch acks NOT ported (upstream has none — RelayStatus echo is the only confirmation); `parse_event` unknown events logged, never crash.
# rvcw — CW (Morse) blinker for RV panel lights

Send CW/Morse messages over the RV's panel lights, driven by
standalone code.

- Gateway: IDS-CAN BLE OneControl panel (bonded to host `hci0`).
- Originating integration: [phurth/ha-onecontrol](https://github.com/phurth/ha-onecontrol).

## Usage

Configure once (mode 600):

    mkdir -p ~/.config/rvcw && chmod 700 ~/.config/rvcw
    cat > ~/.config/rvcw/config.toml <<'EOF'
    [gateway]
    mac = "10:06:1C:E7:B8:3E"
    pin = "123456"
    wpm = 8
    EOF

Two gateway families are supported: legacy MyRvLink COBS panels (UNLOCK/SEED
auth; relays addressed as `table` + `device`) and IDS-CAN panels (DEVICE_STATUS
status; relays addressed by participant id). `learn` writes `lights.toml`
with both halves; old `device_id = …` entries are rejected at load — re-run
`learn`.

Then:

    uv run rvcw discover              # find devices
    uv run rvcw learn awning          # map a name to a light
    uv run rvcw selftest --light awning
    uv run rvcw send "CQ CQ" --light awning

See [`docs/protocol-notes.md`](docs/protocol-notes.md) for the BLE/IDS-CAN
protocol as used.

See [`morse-rv-lights-briefing.md`](morse-rv-lights-briefing.md) for the
feasibility scan, protocol notes, and prior art.

## Status

Implemented end to end: morse engine, lights/combos mapping, the BLE protocol
layer, and the CLI (`send`, `selftest`, `session`, `probe-session`, `sniff`,
`discover`, `learn`). Panel name mapping and the DEVICE_STATUS relay-state
derivation still await the on-site hardware session — see
[`docs/protocol-notes.md`](docs/protocol-notes.md).

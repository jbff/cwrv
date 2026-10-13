# tests/test_discovery.py
import asyncio

import pytest
from click.testing import CliRunner

from rvcw.ble.session import RemoteControlSessionError
from rvcw.ble.transport import decode_notification
from rvcw.discovery import (
    DiscoveredDevice,
    collect_devices,
    device_discovery_request,
    parse_device_id_frame,
)
from rvcw.protocol import (
    IdsCanWireFrame,
    compose_ids_can_standard_wire_frame,
    parse_ids_can_wire_frame,
)


def test_parse_device_id_frame():
    # DEVICE_ID (11-bit) wire frame for address 0x2E: product 46, type 22, function 2
    frame_bytes = bytes([8, 0x02, 0x2E, 0x00, 0x2E, 0x2E, 22, 0x00, 0x02, 0x00, 0x00])
    dev = parse_device_id_frame(frame_bytes)
    assert dev is not None
    assert dev.address == 0x2E
    assert dev.product_id == 46
    assert dev.device_type == 22
    assert dev.function_name == 2


def test_parse_device_id_frame_parses_the_local_host_identity_frame():
    # the LocalHost's own DEVICE_ID advertisement round-trips the same sematics
    wire = parse_ids_can_wire_frame(
        compose_ids_can_standard_wire_frame(
            0x02, 0x2D,
            bytes([0x00, 0x2E, 0x2D, 22, 0x00, 0x02, 0x00, 0x00]),
        )
    )
    rebuilt = bytes([wire.dlc, 0x02, wire.source_address]) + wire.payload
    dev = parse_device_id_frame(rebuilt)
    assert dev == DiscoveredDevice(
        address=0x2D, product_id=46,
        device_type=22, function_name=2, function_instance=0,
    )


def test_parse_device_id_frame_rejects_non_device_id_and_short_payloads():
    # a 29-bit frame (byte1 high bit set) is never an 11-bit DEVICE_ID frame
    assert parse_device_id_frame(bytes([0, 0x82, 0x11, 0x22, 0x33, 0x44])) is None
    # 11-bit framing with the wrong message type (0x03 DEVICE_STATUS)
    status = bytes([8, 0x03, 0x2E]) + bytes(8)
    assert parse_device_id_frame(status) is None
    # DEVICE_ID with a too-short payload (want >= 7)
    assert parse_device_id_frame(bytes([4, 0x02, 0x2E, 0, 1, 2, 3])) is None
    # truncated frame: not even parseable wire
    assert parse_device_id_frame(bytes([2, 0x02])) is None
    assert parse_device_id_frame(b"") is None


def test_parse_device_id_frame_reads_v2_packed_device_frame():
    # a Packed V2 notification decodes a DEVICE_ID candidate frame byte-identical
    # to the hand-built golden above (product_id 0x002E=46, type 22, function 2)
    packed = bytes(
        [0x01, 0x2E, 0x00, 0x12]
        + list(bytes(6))  # mac
        + [0x00, 0x2E, 0x00, 22, 0x00, 0x02, 0x00, 0x00]  # device id payload
        + [0x00, 0x00]  # status len / status data
    )
    device_frames = [f for f in decode_notification(packed) if f.message_type == 0x02]
    assert device_frames
    rebuilt = bytes([device_frames[0].dlc, 0x02, device_frames[0].source_address])
    dev = parse_device_id_frame(rebuilt + device_frames[0].payload)
    assert dev.product_id == 46 and dev.device_type == 22 and dev.function_name == 2


class FakeLink:
    """GatewayLink stand-in: records writes, forwards frames to on_frame."""

    def __init__(self, local_host: int | None = 0x2D):
        self.local_host_address = local_host
        self.written: list[bytes] = []
        self.on_frame = None

    async def write_can_frame(self, frame: bytes) -> None:
        self.written.append(frame)

    def deliver(self, wire: IdsCanWireFrame) -> None:
        self.on_frame(wire)

    def device_id_wire(self, address: int, function_name: int = 2) -> bytes:
        return compose_ids_can_standard_wire_frame(
            0x02, address,
            bytes([0, 0x2E, address, 22, 0x00, function_name, 0x00, 0x00]),
        )


async def test_device_discovery_request_broadcasts_request_0x02_to_ff():
    link = FakeLink()
    await device_discovery_request(link)
    wire = parse_ids_can_wire_frame(link.written[0])
    assert (wire.message_type, wire.source_address, wire.target_address, wire.message_data) == (
        0x80, 0x2D, 0xFF, 0x02,
    )
    assert wire.payload == b""


async def test_device_discovery_request_requires_claimed_local_host():
    link = FakeLink(local_host=None)
    with pytest.raises(RemoteControlSessionError):
        await device_discovery_request(link)
    assert link.written == []


async def test_collect_devices_parses_frames_and_broadcasts_during_window():
    link = FakeLink()
    task = asyncio.create_task(collect_devices(link, window_s=0.05))
    for _ in range(20):  # wait until the broadcast went out and the hook is live
        await asyncio.sleep(0.01)
        if link.written:
            break
    wire = parse_ids_can_wire_frame(link.device_id_wire(0x2E, function_name=2))
    wire2 = parse_ids_can_wire_frame(link.device_id_wire(0x41, function_name=0x2E))
    link.deliver(wire)
    link.deliver(wire)  # duplicate address: deduped
    link.deliver(wire2)
    devices = await task
    assert devices == sorted(devices, key=lambda d: d.address)
    assert [d.address for d in devices] == [0x2E, 0x41]
    assert [d.function_name for d in devices] == [2, 0x2E]
    wire_req = parse_ids_can_wire_frame(link.written[0])
    assert (wire_req.message_type, wire_req.target_address, wire_req.message_data) == (0x80, 0xFF, 0x02)


async def test_collect_devices_chains_and_restores_the_previous_hook():
    link = FakeLink()
    seen: list[IdsCanWireFrame] = []

    def previous(wire: IdsCanWireFrame) -> None:
        seen.append(wire)

    link.on_frame = previous
    task = asyncio.create_task(collect_devices(link, window_s=0.05))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if link.written:
            break
    link.on_frame(parse_ids_can_wire_frame(link.device_id_wire(0x2E)))
    await task
    assert len(seen) == 1  # previous hook was still chained during collection
    assert link.on_frame is previous  # and restored afterwards


async def test_collect_devices_ignores_noise_frames():
    link = FakeLink()
    task = asyncio.create_task(collect_devices(link, window_s=0.05))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if link.written:
            break
    link.on_frame(parse_ids_can_wire_frame(link.device_id_wire(0x2E)))
    status = compose_ids_can_standard_wire_frame(0x03, 0x2E, bytes(8))
    link.on_frame(parse_ids_can_wire_frame(status))
    devices = await task
    assert [d.address for d in devices] == [0x2E]


def test_discovered_device_is_frozen():
    dev = DiscoveredDevice(0x2E, 46, 22, 2, 0)
    with pytest.raises(AttributeError):  # FrozenInstanceError subclasses AttributeError
        dev.device_type = 1  # type: ignore[misc]


def _install_open_gateway(monkeypatch, link, rc):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_open_gateway(cfg, observed=None):
        yield link, rc

    monkeypatch.setattr("rvcw.executor.open_gateway", fake_open_gateway)


def _fake_devices(addresses):
    return [
        DiscoveredDevice(a, 46, 22, 2 if a == 0x2E else 0x2E, 0) for a in addresses
    ]


async def _fake_collect_devices(link, **kw):
    """Async stand-in matching the real collect_devices signature."""
    return _fake_devices([0x2E, 0x41])


async def _fake_collect_devices_empty(link, **kw):
    return []


@pytest.fixture()
def rvcw_home(tmp_path, monkeypatch):
    home = tmp_path / "rvcw"
    home.mkdir()
    monkeypatch.setenv("RVCW_HOME", str(home))
    return home


def test_cli_learn_saves_first_confirmed_device(rvcw_home, monkeypatch):
    from rvcw import cli

    class FakeRC:
        pass

    captured: list = []

    async def fake_set_relay(link, rc, device_id, on, **kw):
        captured.append((device_id, on))

    monkeypatch.setattr("rvcw.ble.can.set_relay", fake_set_relay)
    monkeypatch.setattr(
        "rvcw.discovery.collect_devices", _fake_collect_devices
    )
    _install_open_gateway(monkeypatch, FakeLink(), FakeRC())

    result = CliRunner().invoke(cli.cli, ["learn", "awning", "--yes"])
    assert result.exit_code == 0, result.output
    assert "saved: awning = 0x2E" in result.output
    assert [(d, on) for d, on in captured] == [
        (0x2E, True), (0x2E, False),
    ]  # stops walking after the first confirmed device
    saved = (rvcw_home / "lights.toml").read_text()
    assert "[lights.awning]" in saved and "0x002E" in saved


def test_cli_learn_exits_1_when_no_devices(rvcw_home, monkeypatch):
    from rvcw import cli

    async def fake_set_relay(link, rc, device_id, on, **kw):
        pass

    monkeypatch.setattr("rvcw.ble.can.set_relay", fake_set_relay)
    monkeypatch.setattr("rvcw.discovery.collect_devices", _fake_collect_devices_empty)
    _install_open_gateway(monkeypatch, FakeLink(), object())

    result = CliRunner().invoke(cli.cli, ["learn", "awning", "--yes"])
    assert result.exit_code == 1
    assert "no devices found" in result.output


def test_cli_learn_walks_all_candidates_then_fails(rvcw_home, monkeypatch):
    from rvcw import cli

    toggled: list[tuple[int, bool]] = []

    async def fake_set_relay(link, rc, device_id, on, **kw):
        toggled.append((device_id, on))

    monkeypatch.setattr("rvcw.ble.can.set_relay", fake_set_relay)
    monkeypatch.setattr("rvcw.discovery.collect_devices", _fake_collect_devices)
    _install_open_gateway(monkeypatch, FakeLink(), object())

    result = CliRunner().invoke(
        cli.cli, ["learn", "awning"], input="n\nn\n"
    )  # answering "no" to every prompt walks all candidates
    assert result.exit_code == 1
    assert "no device matched" in result.output
    assert toggled == [(0x2E, True), (0x2E, False), (0x41, True), (0x41, False)]
    assert not (rvcw_home / "lights.toml").exists()


def test_cli_discover_lists_function_labels(rvcw_home, monkeypatch):
    from rvcw import cli

    class RealishLink:
        frames_received = 0

        def __init__(self, mac, pin=None):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

        async def write_can_frame(self, frame):
            pass

    monkeypatch.setattr("rvcw.ble.transport.GatewayLink", RealishLink)
    monkeypatch.setattr("rvcw.ble.session.unlock_gateway", _async_noop)
    monkeypatch.setattr("rvcw.ble.can.claim_local_host", _claim_noop)
    monkeypatch.setattr(
        "rvcw.discovery.collect_devices", _fake_collect_devices
    )

    result = CliRunner().invoke(cli.cli, ["discover"])
    assert result.exit_code == 0, result.output
    assert "device 0x2E product=46 type=22" in result.output
    assert "MyRV Tablet" in result.output  # FUNCTION_NAMES label


async def _async_noop(*args, **kw):
    pass


def _claim_noop(link, observed):
    return _async_noop()


# ---- Task 14: data-path relay-state discovery -------------------------------


async def test_collect_relay_states_broadcasts_get_devices_once_and_snapshots():
    from rvcw.discovery import collect_relay_states
    from rvcw.protocol.commands import CommandBuilder

    class DataLink:
        def __init__(self):
            self.relay_states: dict[tuple[int, int], bool] = {}
            self.sent: list[bytes] = []

        async def send_command(self, raw: bytes) -> None:
            self.sent.append(raw)
            async def later():
                await asyncio.sleep(0.05)
                self.relay_states[(0x02, 0x2E)] = True
            asyncio.create_task(later())

    link = DataLink()
    states = await collect_relay_states(link, 0x02, window_s=0.15)
    assert states == {(0x02, 0x2E): True}  # state echoed inside the window
    assert len(link.sent) == 1  # exactly ONE broadcast
    # [cid2][0x01 GetDevices][table][0x00 0xFF]
    assert link.sent[0][2] == CommandBuilder.CMD_GET_DEVICES
    assert link.sent[0][3] == 0x02


def test_collect_relay_states_is_a_snapshot_not_a_live_view():
    import asyncio as aio
    from rvcw.discovery import collect_relay_states

    class DataLink:
        relay_states = {(0x02, 0x2E): False}

        async def send_command(self, raw: bytes) -> None:
            pass

    link = DataLink()
    states = aio.run(collect_relay_states(link, 0x02, window_s=0.01))
    states[(0x02, 0x2F)] = True
    assert (0x02, 0x2F) not in link.relay_states  # mutating the snapshot is safe

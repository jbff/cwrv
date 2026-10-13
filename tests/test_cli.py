# tests/test_cli.py
import asyncio
from contextlib import asynccontextmanager

import pytest
from click.testing import CliRunner
from rvcw.protocol import parse_ids_can_wire_frame


@pytest.fixture()
def rvcw_home(tmp_path, monkeypatch):
    home = tmp_path / "rvcw"
    home.mkdir()
    monkeypatch.setenv("RVCW_HOME", str(home))
    # byte-sized device id: data-path device ids are single bytes on the wire
    # (build_action_switch masks to one byte) and load_map REJECTS larger ones
    (home / "lights.toml").write_text(
        "[lights.awning]\ntable = 0x00\ndevice = 0x2E\n"
    )
    return home


def test_send_dry_run_lists_schedule(rvcw_home):
    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["send", "E", "--light", "awning", "--dry-run"])
    assert result.exit_code == 0, result.output
    # new (table, device) addressing is shown per target
    assert "table=0x00" in result.output
    assert "device=0x002E" in result.output
    assert "ON    150ms" in result.output


def test_send_unknown_light_fails_fast(rvcw_home):
    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["send", "E", "--light", "ghost", "--dry-run"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_send_old_style_lights_toml_fails_fast(rvcw_home):
    """The removed device_id mapping surfaces as a Click error, not a traceback."""
    from rvcw.cli import cli

    (rvcw_home / "lights.toml").write_text("[lights.awning]\ndevice_id = 0x2E41\n")
    result = CliRunner().invoke(cli, ["send", "E", "--light", "awning", "--dry-run"])
    assert result.exit_code != 0
    assert "removed device_id key" in result.output


def test_send_rejects_over_range_device_id_cleanly(rvcw_home):
    """Minor A through the CLI: a lights.toml device entry above one byte is
    a clean (traced) error naming the light, not a masked wrong-relay send."""
    from rvcw.cli import cli

    (rvcw_home / "lights.toml").write_text(
        "[lights.awning]\ntable = 0x00\ndevice = 0x2E41\n"
    )
    result = CliRunner().invoke(cli, ["send", "E", "--light", "awning", "--dry-run"])
    assert result.exit_code != 0
    assert "light 'awning': device id 0x2E41 exceeds one byte" in result.output


def test_lights_list(rvcw_home):
    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["lights"])
    assert result.exit_code == 0
    assert "awning" in result.output and "0x002E" in result.output and "table=0x00" in result.output


# ---- learn: Ctrl-C during the ON->dwell->OFF walk must not latch ON --------


def test_learn_ctrl_c_during_dwell_still_commands_off(rvcw_home, monkeypatch):
    from rvcw import cli as cli_mod
    import rvcw.ble.can as can_mod
    import rvcw.discovery as discovery_mod
    import rvcw.executor as executor_mod

    class FakeLink:
        mac = "fake"

        def __init__(self):
            self.local_host_address = 0x2D
            self.written: list[bytes] = []

        async def write_can_frame(self, frame: bytes) -> None:
            self.written.append(frame)

    class FakeRC:
        async def ensure(self, device_id: int) -> bool:
            return True

        async def aclose(self) -> None:
            pass

    link = FakeLink()

    @asynccontextmanager
    async def fake_open_gateway(config, observed=None):
        yield link, FakeRC()

    sent_relays: list[tuple[int, bool]] = []

    async def fake_set_relay(glink, rc, device_id, on, confirm_timeout=2.0):
        sent_relays.append((device_id, on))
        await glink.write_can_frame(
            can_mod.relay_command(glink.local_host_address, device_id, on)
        )

    async def fake_collect_devices(glink, window_s=3.0):
        return [discovery_mod.DiscoveredDevice(0x33, 0, 0, 0, 0)]

    # Ctrl-C mid-dwell: the 0.6 s dwell (the only asyncio.sleep learn makes)
    # surfaces cancellation
    real_sleep = asyncio.sleep

    async def ctrl_c_sleep(delay, *args, **kwargs):
        if delay >= 0.6:
            raise asyncio.CancelledError()
        await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(executor_mod, "open_gateway", fake_open_gateway)
    monkeypatch.setattr(can_mod, "set_relay", fake_set_relay)
    monkeypatch.setattr(discovery_mod, "collect_devices", fake_collect_devices)
    monkeypatch.setattr(cli_mod.asyncio, "sleep", ctrl_c_sleep)

    from rvcw.cli import cli

    # CancelledError is a BaseException: it escapes CliRunner itself (the
    # asyncio.run KeyboardInterrupt path in real use) AFTER the finally ran
    with pytest.raises(asyncio.CancelledError):
        CliRunner().invoke(cli, ["learn", "awning", "--yes"])
    # ON was commanded, and the finally block commanded the best-effort OFF
    # to the SAME device — the light is not latched ON
    assert sent_relays == [(0x33, True)]
    off_frames = [
        (w.target_address, w.message_data)
        for f in link.written
        if (w := parse_ids_can_wire_frame(f))
        and w.message_type == 0x82
        and w.message_data == 0x00
    ]
    assert (0x33, 0x00) in off_frames


# ---- data family (COBS): discover / learn on fake links --------------------


class FakeDataLink:
    """GatewayLink stand-in for a connected legacy-COBS gateway."""

    def __init__(self, mac, pin=None):
        from rvcw.protocol.events import GatewayInformation

        self.family = "data"
        self.mac = mac
        self.pin = pin
        self.gateway_info = GatewayInformation(table_id=0x02)
        self.authed = False
        self.keepalive = False
        self.closed = False
        self.relay_states: dict[tuple[int, int], bool] = {}
        self.commands: list[bytes] = []

    async def connect(self) -> None:
        pass

    async def authenticate_data(self, pin) -> None:
        self.authed = True

    async def wait_gateway_info(self, timeout: float = 10.0) -> int:
        return 0x02

    async def start_keepalive(self) -> None:
        self.keepalive = True

    async def send_command(self, raw: bytes) -> None:
        self.commands.append(raw)

    async def close(self) -> None:
        self.closed = True


def _install_fake_gateway_link(monkeypatch, link_cls):
    import rvcw.ble.transport as transport_mod

    monkeypatch.setattr(transport_mod, "GatewayLink", link_cls)


def test_cli_discover_data_family_prints_table_device_on_rows(rvcw_home, monkeypatch):
    import rvcw.discovery as discovery_mod

    from rvcw.cli import cli

    tables: list[int] = []
    seen: list = []

    async def fake_collect_relay_states(glink, table, window_s=3.0):
        tables.append(table)
        seen.append(glink)
        glink.relay_states = {(0x02, 0x2E): True, (0x02, 0x2F): False}
        return dict(glink.relay_states)

    _install_fake_gateway_link(monkeypatch, FakeDataLink)
    monkeypatch.setattr(discovery_mod, "collect_relay_states", fake_collect_relay_states)

    result = CliRunner().invoke(cli, ["discover"])
    assert result.exit_code == 0, result.output
    (link,) = seen
    assert link.authed and link.keepalive and link.closed  # auth + keepalive + teardown
    assert tables == [0x02]  # one GetDevices broadcast for the announced table
    assert "relay table=0x02 device=0x2E on=True" in result.output
    assert "relay table=0x02 device=0x2F on=False" in result.output


def test_cli_discover_data_family_no_relays_is_an_error(rvcw_home, monkeypatch):
    import rvcw.discovery as discovery_mod

    from rvcw.cli import cli

    class EmptyLink(FakeDataLink):
        def __init__(self, mac, pin=None):
            super().__init__(mac, pin)
            self.relay_states = {}

    async def fake_collect_relay_states(glink, table, window_s=3.0):
        return {}

    _install_fake_gateway_link(monkeypatch, EmptyLink)
    monkeypatch.setattr(discovery_mod, "collect_relay_states", fake_collect_relay_states)

    result = CliRunner().invoke(cli, ["discover"])
    assert result.exit_code == 1
    assert "no relays reported" in result.output


@asynccontextmanager
async def fake_data_open_gateway(config, observed=None):
    yield FakeDataLink("AA:BB"), None  # rc=None marks the data family


def test_cli_learn_data_family_saves_table_and_device(rvcw_home, monkeypatch):
    from rvcw import cli as cli_mod
    import rvcw.discovery as discovery_mod
    import rvcw.executor as executor_mod

    monkeypatch.setattr(executor_mod, "open_gateway", fake_data_open_gateway)

    async def fake_collect_relay_states(glink, table, window_s=3.0):
        glink.relay_states = {(0x02, 0x2E): False}
        return dict(glink.relay_states)

    monkeypatch.setattr(discovery_mod, "collect_relay_states", fake_collect_relay_states)

    real_sleep = asyncio.sleep

    async def quick_sleep(delay, *args, **kwargs):
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(cli_mod.asyncio, "sleep", quick_sleep)

    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["learn", "awning", "--yes"])
    assert result.exit_code == 0, result.output
    saved = (rvcw_home / "lights.toml").read_text()
    assert "[lights.awning]" in saved
    assert "table = 0x02" in saved
    assert "device = 0x002E" in saved


def test_cli_learn_data_family_offs_first_and_never_latches_on(rvcw_home, monkeypatch):
    """Candidates come from relay_states keys; OFF first, ON, 0.6 s dwell,
    finally-OFF — an abort anywhere (here: the declined confirm walk) can
    never leave the light latched ON."""
    from rvcw import cli as cli_mod
    import rvcw.discovery as discovery_mod
    import rvcw.executor as executor_mod

    ctx_holder: dict = {}

    @asynccontextmanager
    async def fake_open(config, observed=None):
        link = FakeDataLink("AA:BB")
        ctx_holder["link"] = link
        yield link, None

    monkeypatch.setattr(executor_mod, "open_gateway", fake_open)

    async def fake_collect_relay_states(glink, table, window_s=3.0):
        glink.relay_states = {(0x02, 0x2E): False}
        return dict(glink.relay_states)

    monkeypatch.setattr(discovery_mod, "collect_relay_states", fake_collect_relay_states)

    real_sleep = asyncio.sleep

    async def quick_sleep(delay, *args, **kwargs):
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(cli_mod.asyncio, "sleep", quick_sleep)

    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["learn", "awning"], input="n\n")
    assert result.exit_code == 1, result.output
    link = ctx_holder["link"]
    # [cid2][0x40][table][state][device] — OFF, ON, then the finally-OFF
    switches = [(raw[3], raw[5], raw[4] == 0x01) for raw in link.commands]
    assert switches == [(0x02, 0x2E, False), (0x02, 0x2E, True), (0x02, 0x2E, False)]


# ---- sniff: decoded COBS event rows ----------------------------------------


def test_sniff_formatter_highlights_relay_and_gateway_rows():
    from rvcw import cli

    relay = cli._describe_data_event(bytes([0x05, 0x02, 0x2E, 0x01, 0x00]))
    assert "RELAY" in relay
    assert "table=0x02" in relay and "device=0x2E" in relay and "on=True" in relay

    relay_off = cli._describe_data_event(bytes([0x06, 0x02, 0x2E, 0x11, 0x00]))
    assert "RELAY" in relay_off and "on=True" in relay_off  # 0x11 IS on (vendored rule)

    info = cli._describe_data_event(bytes([0x01, 0x02, 0x00, 0x05, 0x02]) + bytes(8))
    assert "GATEWAY-INFO" in info and "table=0x02" in info

    other = cli._describe_data_event(bytes([0x07, 0x00, 0x0B]))
    assert "RV" in other or "0x07" in other  # decoded, never a crash


# ---- Minor B: wrong-PIN / silent-gateway auth failures exit cleanly --------
# send's except tuple must include GatewayAuthError (raised by
# open_gateway → authenticate_data / unlock_gateway for a wrong PIN or a
# silent gateway) and selftest's must include LinkError AND GatewayAuthError:
# a failure there exits 1 with the message, never a traceback.


def test_send_wrong_pin_exits_cleanly(rvcw_home, monkeypatch):
    from rvcw.ble.transport import GatewayAuthError

    import rvcw.executor as executor_mod

    @asynccontextmanager
    async def fake_open(config, observed=None):
        raise GatewayAuthError("PIN unlock failed — verify= (wrong PIN)")
        yield  # pragma: no cover

    monkeypatch.setattr(executor_mod, "open_gateway", fake_open)

    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["send", "E", "--light", "awning"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "aborted: PIN unlock failed" in result.output


def test_selftest_wrong_pin_exits_cleanly(rvcw_home, monkeypatch):
    from rvcw.ble.transport import LinkError

    import rvcw.executor as executor_mod

    @asynccontextmanager
    async def fake_open(config, observed=None):
        raise LinkError("connection dropped during auth")
        yield  # pragma: no cover

    monkeypatch.setattr(executor_mod, "open_gateway", fake_open)

    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["selftest", "--light", "awning"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "selftest FAILED: connection dropped during auth" in result.output


# ---- review fix 3: saved-table vs announced-table mismatch is loud ----------


class EchoDataLink(FakeDataLink):
    """FakeDataLink whose switch writes echo the relay state (runs a schedule)."""

    def __init__(self, mac, pin=None):
        super().__init__(mac, pin)
        self.relay_states = {}
        self.sent_to_relay = False

    async def send_command(self, raw: bytes) -> None:
        self.commands.append(raw)
        self.sent_to_relay = True
        for d in raw[5:]:
            self.relay_states[(raw[3], d)] = raw[4] == 0x01


def _byte_device_fixture(rvcw_home) -> None:
    """Live-send tests need a data-plausible (single-byte) device id:
    build_action_switch masks device ids to one byte on the COBS path."""
    (rvcw_home / "lights.toml").write_text(
        "[lights.awning]\ntable = 0x00\ndevice = 0x2E\n"
    )


def _install_data_gateway(monkeypatch, link_holder: dict, gateway_link_cls):
    import rvcw.executor as executor_mod

    @asynccontextmanager
    async def fake_open(config, observed=None):
        link = gateway_link_cls("AA:BB")
        link_holder["link"] = link
        yield link, None

    monkeypatch.setattr(executor_mod, "open_gateway", fake_open)


def test_send_refuses_table_mismatch_unless_forced(rvcw_home, monkeypatch):
    """Fixture maps awning to table 0x00; the gateway announces 0x02."""
    from rvcw.cli import cli

    _byte_device_fixture(rvcw_home)
    holder: dict = {}
    _install_data_gateway(monkeypatch, holder, EchoDataLink)

    result = CliRunner().invoke(cli, ["send", "E", "--light", "awning"])
    assert result.exit_code == 1, result.output
    assert "warning" in result.output
    assert "0x00" in result.output and "0x02" in result.output
    assert "awning" in result.output
    assert "--force-table" in result.output
    assert holder["link"].sent_to_relay is False  # nothing was sent

    result = CliRunner().invoke(cli, ["send", "E", "--light", "awning", "--force-table"])
    assert result.exit_code == 0, result.output
    assert "sent" in result.output
    assert holder["link"].sent_to_relay is True


def test_send_table_match_sends_without_force(rvcw_home, monkeypatch):
    from rvcw.cli import cli

    _byte_device_fixture(rvcw_home)
    holder: dict = {}

    class MatchedLink(EchoDataLink):
        def __init__(self, mac, pin=None):
            super().__init__(mac, pin)
            from rvcw.protocol.events import GatewayInformation

            self.gateway_info = GatewayInformation(table_id=0x00)

    _install_data_gateway(monkeypatch, holder, MatchedLink)

    result = CliRunner().invoke(cli, ["send", "E", "--light", "awning"])
    assert result.exit_code == 0, result.output
    assert "sent" in result.output


def test_selftest_warns_on_table_mismatch_but_runs(rvcw_home, monkeypatch):
    from rvcw.cli import cli

    _byte_device_fixture(rvcw_home)
    holder: dict = {}
    _install_data_gateway(monkeypatch, holder, EchoDataLink)

    result = CliRunner().invoke(cli, ["selftest", "--light", "awning"])
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    assert "table 0x00" in result.output and "0x02" in result.output
    assert "selftest OK" in result.output


def test_learn_data_family_warns_when_candidate_table_differs(rvcw_home, monkeypatch):
    from rvcw import cli as cli_mod
    import rvcw.discovery as discovery_mod
    import rvcw.executor as executor_mod

    holder: dict = {}

    @asynccontextmanager
    async def fake_open(config, observed=None):
        link = EchoDataLink("AA:BB")
        holder["link"] = link
        yield link, None

    monkeypatch.setattr(executor_mod, "open_gateway", fake_open)

    async def fake_collect_relay_states(glink, table, window_s=3.0):
        glink.relay_states = {(0x01, 0x2E): False}  # announced table is 0x02
        return dict(glink.relay_states)

    monkeypatch.setattr(discovery_mod, "collect_relay_states", fake_collect_relay_states)

    real_sleep = asyncio.sleep

    async def quick_sleep(delay, *args, **kwargs):
        await real_sleep(min(delay, 0.01))

    monkeypatch.setattr(cli_mod.asyncio, "sleep", quick_sleep)

    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["learn", "awning", "--yes"])
    assert result.exit_code == 0, result.output
    assert "warning" in result.output
    assert "0x01" in result.output and "0x02" in result.output
    saved = (rvcw_home / "lights.toml").read_text()
    assert "table = 0x01" in saved  # the OBSERVED relay table is what gets saved

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

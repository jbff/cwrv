# tests/test_open_gateway.py
import pytest

from rvcw.config import GatewayConfig


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

    # config=None (as the brief suggested) is unsatisfiable: open_gateway reads
    # config.mac, so NoneType raises AttributeError before the fakes matter.
    # A real GatewayConfig (monkeypatched GatewayLink ignores its args) works.
    fake_config = GatewayConfig(mac="fake")
    async with executor.open_gateway(fake_config) as (link, rc):
        assert isinstance(rc, RemoteControl)
        assert link.local_host_address == 0x2D
    assert calls[0] == "connect" and calls[-1] == "close"


async def test_open_gateway_data_family_auths_and_keeps_alive_without_rc(monkeypatch):
    """Legacy COBS gateways: connect → authenticate_data → wait_gateway_info →
    keepalive, and rc=None (no REMOTE_CONTROL machinery on the data path)."""
    from rvcw import executor

    calls: list[str] = []

    class FakeDataLink:
        mac = "fake"
        pin = "1234"
        family = "data"
        local_host_address = None

        def __init__(self):
            self.closed = False

        async def connect(self):
            calls.append("connect")

        async def authenticate_data(self, pin):
            calls.append(f"auth:{pin}")

        async def wait_gateway_info(self, timeout=10.0):
            calls.append("wait_info")
            return 2

        async def start_keepalive(self):
            calls.append("keepalive")

        async def close(self):
            self.closed = True
            calls.append("close")

    monkeypatch.setattr(executor, "GatewayLink", lambda mac, pin: FakeDataLink())

    fake_config = GatewayConfig(mac="fake", pin="1234")
    async with executor.open_gateway(fake_config) as (link, rc):
        assert rc is None
        assert link.family == "data"
    assert calls == ["connect", "auth:1234", "wait_info", "keepalive", "close"]
    assert calls[-1] == "close"

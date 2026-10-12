"""Schedule execution against the live gateway."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .ble.can import claim_local_host
from .ble.session import RemoteControl, unlock_gateway
from .ble.transport import GatewayLink
from .config import GatewayConfig


@asynccontextmanager
async def open_gateway(
    config: GatewayConfig, observed: set[int] | None = None
) -> AsyncIterator[tuple[GatewayLink, RemoteControl]]:
    """Connect + unlock + claim LocalHost; RC heartbeat cancelled on exit."""
    link = GatewayLink(config.mac, config.pin)
    await link.connect()
    rc: RemoteControl | None = None
    try:
        await unlock_gateway(link)
        await claim_local_host(link, observed if observed is not None else set())
        rc = RemoteControl(link)
        link.on_frame = rc.handle_frame
        yield link, rc
    finally:
        if rc is not None:
            await rc.aclose()  # cancel heartbeat; NOT end() (relay gotcha)
        await link.close()

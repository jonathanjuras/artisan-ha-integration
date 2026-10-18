"""Fixtures for Artisan Roaster Scope tests: a fake bridge speaking protocol v1."""

import asyncio
from collections.abc import AsyncGenerator, Callable
import copy
import socket
import time
from typing import Any

from aiohttp import web
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.artisan import client as client_module
from custom_components.artisan.client import ArtisanBridgeClient
from custom_components.artisan.const import (
    CONF_HOSTNAME,
    CONF_UNIT,
    DOMAIN,
    OPT_ROR_PRECISION,
    OPT_SAMPLE_INTERVAL,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant

TOKEN = "s3cr3t-t0ken-value"
BRIDGE_ID = "5b0c2d8e-3f43-4c39-9a51-2f3e7f0b9c11"
HOSTNAME = "roaster"
DEVICE_PREFIX = "artisan_roaster"


def snapshot_state() -> dict[str, Any]:
    """Return the snapshot state from spec 3.3."""
    return {
        "phase": "development",
        "monitoring": True,
        "recording": True,
        "artisan_online": True,
        "roast_id": "2026-10-17T14:03:12Z",
        "tm": 512.0,
        "bt": 196.3,
        "et": 225.0,
        "bt_ror": 9.8,
        "et_ror": 4.1,
        "dev_time": 48.0,
        "dtr": 9.4,
        "controls": {"heat": 70.0, "fan": 40.0},
        "events": {
            "charge": {"tm": 0.0, "bt": 185.2},
            "dry": {"tm": 245.0, "bt": 152.1},
            "fcs": {"tm": 464.0, "bt": 194.0},
        },
        "last_event": {"event": "fcs", "ts": 1792252483254},
        "last_summary": None,
    }


def now_ms() -> int:
    """Return the bridge timestamp format (epoch ms)."""
    return int(time.time() * 1000)


class FakeBridge:
    """An aiohttp server on an ephemeral localhost port that acts like the bridge."""

    def __init__(self) -> None:
        """Initialize with the spec's example data."""
        self.token = TOKEN
        self.hello: dict[str, Any] = {
            "v": 1,
            "type": "hello",
            "ts": now_ms(),
            "bridge_version": "0.1.0",
            "bridge_id": BRIDGE_ID,
            "hostname": HOSTNAME,
            "unit": "C",
            "protocol": 1,
        }
        self.state = snapshot_state()
        self.send_hello = True
        # When set, answer every request with this HTTP status instead of upgrading.
        self.reject_status: int | None = None
        self.clients: list[web.WebSocketResponse] = []
        self.connections = 0
        self.auth_failures = 0
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """Start listening on 127.0.0.1 with an ephemeral port."""
        app = web.Application()
        app.router.add_get("/v1", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        site = web.SockSite(self._runner, sock)
        await site.start()

    async def stop(self) -> None:
        """Close all clients and the server."""
        await self.drop_clients()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if self.reject_status is not None:
            return web.Response(status=self.reject_status)
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            self.auth_failures += 1
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        if self.send_hello:
            await ws.send_json({**self.hello, "ts": now_ms()})
            await ws.send_json(
                {
                    "v": 1,
                    "type": "snapshot",
                    "ts": now_ms(),
                    "state": copy.deepcopy(self.state),
                }
            )
        self.clients.append(ws)
        try:
            async for _ in ws:
                pass  # the HA client never sends application messages
        finally:
            self.clients.remove(ws)
        return ws

    async def push(self, msg_type: str, **fields: Any) -> dict[str, Any]:
        """Send one protocol message to every connected client."""
        msg = {"v": 1, "type": msg_type, "ts": now_ms(), **fields}
        for ws in list(self.clients):
            await ws.send_json(msg)
        return msg

    async def drop_clients(self) -> None:
        """Close every client connection from the server side."""
        for ws in list(self.clients):
            await ws.close()


async def wait_for(
    hass: HomeAssistant, predicate: Callable[[], bool], timeout: float = 5.0
) -> None:
    """Wait until the predicate holds, then let Home Assistant settle."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)
    await hass.async_block_till_done()


def get_client(entry: MockConfigEntry) -> ArtisanBridgeClient:
    """Return the bridge client of a loaded entry."""
    client: ArtisanBridgeClient = entry.runtime_data.client
    return client


async def push_and_wait(
    hass: HomeAssistant,
    bridge: FakeBridge,
    entry: MockConfigEntry,
    msg_type: str,
    **fields: Any,
) -> dict[str, Any]:
    """Push a message and wait until the client has processed it."""
    client = get_client(entry)
    received = client.messages_received
    msg = await bridge.push(msg_type, **fields)
    await wait_for(hass, lambda: client.messages_received > received)
    return msg


def unused_port() -> int:
    """Return a localhost port with nothing listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink reconnect timing so tests do not wait for real backoff."""
    monkeypatch.setattr(client_module, "BACKOFF_INITIAL", 0.01)
    monkeypatch.setattr(client_module, "BACKOFF_MAX", 0.05)
    monkeypatch.setattr(client_module, "HELLO_TIMEOUT", 2.0)


@pytest.fixture
async def fake_bridge(socket_enabled: None) -> AsyncGenerator[FakeBridge]:
    """Run a fake bridge for the duration of the test."""
    bridge = FakeBridge()
    await bridge.start()
    yield bridge
    await bridge.stop()


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Return config entry options; throttling is off so tests see every write."""
    return {OPT_SAMPLE_INTERVAL: 0.0, OPT_ROR_PRECISION: 1}


@pytest.fixture
def config_entry(
    hass: HomeAssistant, fake_bridge: FakeBridge, entry_options: dict[str, Any]
) -> MockConfigEntry:
    """Return a config entry pointing at the fake bridge."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Artisan ({HOSTNAME})",
        unique_id=BRIDGE_ID,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: fake_bridge.port,
            CONF_TOKEN: TOKEN,
            CONF_HOSTNAME: HOSTNAME,
            CONF_UNIT: "C",
        },
        options=entry_options,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, fake_bridge: FakeBridge
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration and wait for the hello + snapshot."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    client = get_client(config_entry)
    await wait_for(hass, lambda: client.connected and client.state.phase is not None)
    yield config_entry
    # Unload while the bridge still runs so the client task ends cleanly.
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

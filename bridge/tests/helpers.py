"""Shared test helpers (importable as `helpers`; tests/ is on sys.path via pytest rootdir)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from artisan_ha_bridge.config import Config
from artisan_ha_bridge.protocol import OutboundMessage

FIXTURES = Path(__file__).parent / "fixtures"

# 2026-10-17T14:03:12Z
EPOCH = 1792245792.0


@dataclass
class FakeClock:
    """Wall clock in epoch seconds."""

    now: float = EPOCH

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Recorder:
    """State-machine listener that keeps every outbound message."""

    messages: list[OutboundMessage] = field(default_factory=list)

    def __call__(self, message: OutboundMessage) -> None:
        self.messages.append(message)

    def types(self) -> list[str]:
        return [m["type"] for m in self.messages]

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [dict(m) for m in self.messages if m["type"] == kind]

    def clear(self) -> None:
        self.messages.clear()


def load_fixture(name: str) -> Iterator[dict[str, Any]]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


async def wait_for(predicate: Callable[[], bool], within: float = 3.0) -> None:
    """Poll `predicate` until it is true or fail after `within` seconds."""
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class FakeWebLCDs:
    """Minimal stand-in for Artisan's WebLCDs server: pushes queued frames to every client."""

    def __init__(self, path: str = "/websocket") -> None:
        self.path = path
        self.connections = 0
        self._sockets: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        app = web.Application()
        app.router.add_get(path, self._handle)
        self._app = app

    @property
    def port(self) -> int:
        assert self._runner is not None
        return int(self._runner.addresses[0][1])

    @property
    def open_sockets(self) -> int:
        return len(self._sockets)

    async def start(self, port: int = 0) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", port).start()

    async def stop(self) -> None:
        await self.drop_clients()
        if self._runner is not None:
            await self._runner.cleanup()

    async def send(self, frame: object) -> None:
        data = frame if isinstance(frame, str) else json.dumps(frame)
        for ws in list(self._sockets):
            await ws.send_str(data)

    async def send_bytes(self, data: bytes) -> None:
        for ws in list(self._sockets):
            await ws.send_bytes(data)

    async def drop_clients(self) -> None:
        for ws in list(self._sockets):
            await ws.close()

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        self._sockets.add(ws)
        try:
            async for _ in ws:
                pass
        finally:
            self._sockets.discard(ws)
        return ws


@dataclass
class FakeAdvertiser:
    """Records discovery calls instead of touching multicast."""

    port: int | None = None
    started: bool = False
    stopped: bool = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def fake_advertiser_factory(holder: list[FakeAdvertiser]) -> Callable[[Any, int, str], Any]:
    def factory(_config: Any, port: int, _hostname: str) -> FakeAdvertiser:
        advertiser = FakeAdvertiser(port=port)
        holder.append(advertiser)
        return advertiser

    return factory


def make_config(**ha: Any) -> Config:
    """A generated config bound to loopback with ephemeral ports."""
    config = Config.generate()
    config.ha.bind = "127.0.0.1"
    config.ha.port = 0
    config.ingest.port = 0
    for key, value in ha.items():
        setattr(config.ha, key, value)
    return config


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

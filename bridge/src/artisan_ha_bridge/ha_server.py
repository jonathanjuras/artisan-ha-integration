"""Authenticated WebSocket server for Home Assistant (spec 3.2 and 4.3)."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import math
import time
from collections.abc import Callable
from typing import Final

from aiohttp import WSCloseCode, web

from . import __version__
from .protocol import OutboundMessage, StateSnapshot, build_hello, build_ping, build_snapshot

_LOGGER = logging.getLogger(__name__)

PATH: Final = "/v1"
PING_INTERVAL_S: Final = 15.0
QUEUE_SIZE: Final = 256


class _Client:
    def __init__(self, ws: web.WebSocketResponse, peer: str) -> None:
        self.ws = ws
        self.peer = peer
        self.queue: asyncio.Queue[str] = asyncio.Queue(QUEUE_SIZE)
        self.last_sample_at = -math.inf
        self.pending_sample: str | None = None
        self.sample_timer: asyncio.TimerHandle | None = None
        self.writer: asyncio.Task[None] | None = None
        self.dropped = False


class HAServer:
    def __init__(
        self,
        *,
        bind: str,
        port: int,
        token: str,
        bridge_id: str,
        unit: str,
        hostname: str,
        snapshot: Callable[[], StateSnapshot],
        sample_interval_s: float = 1.0,
        ping_interval_s: float = PING_INTERVAL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bind = bind
        self._port = port
        self._token = token.encode()
        self._bridge_id = bridge_id
        self._unit = unit
        self._hostname = hostname
        self._snapshot = snapshot
        self._interval = sample_interval_s
        self._ping_interval = ping_interval_s
        self._clock = clock
        self._clients: set[_Client] = set()
        self._background: set[asyncio.Task[None]] = set()
        self._runner: web.AppRunner | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self.last_client_seen: float | None = None  # epoch seconds
        app = web.Application()
        app.router.add_get(PATH, self._handle)
        self._app = app

    @property
    def port(self) -> int:
        if self._runner is None:
            return self._port
        return int(self._runner.addresses[0][1])

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def start(self) -> None:
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self._bind, self._port).start()
        except OSError:
            await runner.cleanup()
            raise
        self._runner = runner
        self._ping_task = asyncio.create_task(self._ping_loop(), name="ha-ping")
        _LOGGER.info("Home Assistant server listening on %s:%d%s", self._bind, self.port, PATH)

    async def stop(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ping_task
            self._ping_task = None
        for client in list(self._clients):
            self._drop(client, "server shutting down")
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def publish(self, message: OutboundMessage) -> None:
        """Fan a message out to every client. Samples are throttled per client."""
        data = json.dumps(message, separators=(",", ":"))
        is_sample = message["type"] == "sample"
        for client in list(self._clients):
            if is_sample:
                self._offer_sample(client, data)
            else:
                self._enqueue(client, data)

    # --- connection handling -----------------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(credentials.strip().encode(), self._token)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        peer = request.remote or "?"
        if not self._authorized(request):
            # Rejected before the upgrade, so the client sees a plain HTTP 401.
            reason = "bad token" if "Authorization" in request.headers else "no token"
            _LOGGER.warning("Rejected connection to %s from %s: %s", PATH, peer, reason)
            return web.Response(status=401, text="unauthorized")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        client = _Client(ws, peer)
        # hello and snapshot are queued before the client joins the fan-out, so nothing can
        # overtake them.
        now_ms = int(self._clock() * 1000)
        hello = build_hello(
            now_ms,
            bridge_version=__version__,
            bridge_id=self._bridge_id,
            hostname=self._hostname,
            unit=self._unit,
        )
        client.queue.put_nowait(json.dumps(hello, separators=(",", ":")))
        snapshot = build_snapshot(now_ms, self._snapshot())
        client.queue.put_nowait(json.dumps(snapshot, separators=(",", ":")))
        self._clients.add(client)
        self.last_client_seen = self._clock()
        client.writer = asyncio.create_task(self._writer(client), name=f"ha-writer-{peer}")
        _LOGGER.info("Home Assistant connected from %s", peer)
        try:
            async for _message in ws:
                pass  # the protocol is one-way; inbound frames are ignored
        finally:
            self._drop(client, "disconnected")
            await client.writer
        return ws

    async def _writer(self, client: _Client) -> None:
        try:
            while True:
                data = await client.queue.get()
                await client.ws.send_str(data)
        except asyncio.CancelledError:
            pass
        except (ConnectionError, RuntimeError) as err:
            self._drop(client, f"send failed: {err}")
        with contextlib.suppress(ConnectionError, RuntimeError):
            await client.ws.close(code=WSCloseCode.GOING_AWAY)

    def _drop(self, client: _Client, reason: str) -> None:
        if client.dropped:
            return
        client.dropped = True
        self._clients.discard(client)
        self.last_client_seen = self._clock()
        if client.sample_timer is not None:
            client.sample_timer.cancel()
        _LOGGER.info("Home Assistant client %s removed: %s", client.peer, reason)
        if client.writer is not None and client.writer is not asyncio.current_task():
            client.writer.cancel()
            # The writer closes the socket on its way out; keep a reference until it does.
            self._background.add(client.writer)
            client.writer.add_done_callback(self._background.discard)

    # --- queueing ----------------------------------------------------------------------------

    def _enqueue(self, client: _Client, data: str) -> None:
        try:
            client.queue.put_nowait(data)
        except asyncio.QueueFull:
            self._drop(client, "send queue overflow")

    def _offer_sample(self, client: _Client, data: str) -> None:
        if client.sample_timer is not None:
            client.pending_sample = data  # a send is scheduled; it will carry the latest
            return
        loop = asyncio.get_running_loop()
        due = client.last_sample_at + self._interval
        if loop.time() >= due:
            client.last_sample_at = loop.time()
            self._enqueue(client, data)
            return
        client.pending_sample = data
        client.sample_timer = loop.call_at(due, self._flush_sample, client)

    def _flush_sample(self, client: _Client) -> None:
        client.sample_timer = None
        data, client.pending_sample = client.pending_sample, None
        if data is not None and not client.dropped:
            client.last_sample_at = asyncio.get_running_loop().time()
            self._enqueue(client, data)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            self.publish(build_ping(int(self._clock() * 1000)))

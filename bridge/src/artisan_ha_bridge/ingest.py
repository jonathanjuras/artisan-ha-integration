"""Event source: loopback ingest for Artisan event and slider actions (spec 3.1)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Final

from aiohttp import WSMsgType, web

from .protocol import IngestControl, IngestError, IngestMessage, parse_ingest
from .sources import EventSink

_LOGGER = logging.getLogger(__name__)

LOOPBACK: Final = "127.0.0.1"
StatusProvider = Callable[[], Mapping[str, Any]]


def dispatch(message: IngestMessage, sink: EventSink) -> None:
    if isinstance(message, IngestControl):
        sink.on_control(message.channel, message.value)
    else:
        sink.on_event(message.event, message.bt, message.et, message.tm)


class IngestServer:
    """`EventSource` serving `/artisan` (WebSocket), `/emit` (POST) and `/status` (GET).

    `/status` is a bridge addition for `doctor`; it is loopback-only and never contains secrets.
    """

    def __init__(
        self,
        port: int,
        sink: EventSink,
        *,
        host: str = LOOPBACK,
        status: StatusProvider | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._sink = sink
        self._status = status
        self._runner: web.AppRunner | None = None
        app = web.Application()
        app.router.add_get("/artisan", self._handle_ws)
        app.router.add_post("/emit", self._handle_emit)
        app.router.add_get("/status", self._handle_status)
        self._app = app

    @property
    def port(self) -> int:
        """The bound port (useful when configured as 0)."""
        if self._runner is None:
            return self._port
        address = self._runner.addresses[0]
        return int(address[1])

    async def start(self) -> None:
        runner = web.AppRunner(self._app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self._host, self._port).start()
        except OSError:
            await runner.cleanup()
            raise
        self._runner = runner
        _LOGGER.info("Ingest listening on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _accept(self, raw: object, transport: str) -> bool:
        try:
            message = parse_ingest(raw)
        except IngestError as err:
            _LOGGER.warning("Dropping %s ingest message: %s", transport, err)
            return False
        dispatch(message, self._sink)
        return True

    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        _LOGGER.info("Artisan connected to ingest")
        # Artisan expects no replies; anything sent back would be read as a command result.
        async for message in ws:
            if message.type is not WSMsgType.TEXT:
                continue
            try:
                raw = json.loads(message.data)
            except ValueError:
                _LOGGER.warning("Dropping malformed ingest frame")
                continue
            self._accept(raw, "WebSocket")
        _LOGGER.info("Artisan disconnected from ingest")
        return ws

    async def _handle_emit(self, request: web.Request) -> web.StreamResponse:
        try:
            raw = await request.json()
        except ValueError:
            _LOGGER.warning("Dropping malformed emit body")
            return web.Response(status=400, text="invalid JSON")
        if not self._accept(raw, "emit"):
            return web.Response(status=400, text="schema error")
        return web.Response(status=204)

    async def _handle_status(self, _request: web.Request) -> web.StreamResponse:
        return web.json_response(dict(self._status()) if self._status else {})

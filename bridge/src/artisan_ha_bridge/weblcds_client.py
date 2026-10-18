"""Telemetry source: client for Artisan's WebLCDs WebSocket (spec 4.3)."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Final

import aiohttp

from .config import WeblcdsConfig
from .sources import TelemetrySink

_LOGGER = logging.getLogger(__name__)

_NUMBER_RE: Final = re.compile(r"-?[\d.]+")
_TIME_RE: Final = re.compile(r"(-)?(?:(\d+):)?(\d+):(\d{1,2})")
RETRY_S: Final = 2.0
LIVENESS_TICK_S: Final = 1.0


def parse_value(raw: object) -> float | None:
    """A WebLCDs display string to a float. Artisan's none-symbols (`&nbsp;--`, `-.-`) -> None."""
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    if not isinstance(raw, str):
        return None
    text = html.unescape(raw).strip()
    if not _NUMBER_RE.fullmatch(text):
        return None
    try:
        return float(text)
    except ValueError:  # e.g. "1.2.3" or "."
        return None


def parse_time(raw: object) -> float | None:
    """`mm:ss` (optionally `h:mm:ss`, optionally negative during charge delay) to seconds."""
    if not isinstance(raw, str):
        return None
    match = _TIME_RE.fullmatch(html.unescape(raw).strip())
    if match is None:
        return None
    sign, hours, minutes, seconds = match.groups()
    total = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
    return float(-total if sign else total)


class FrameHandler:
    """Turns decoded WebLCDs frames into sink calls and tracks liveness.

    Shared by the live client and `replay`, so both exercise the same parsing.
    """

    def __init__(self, sink: TelemetrySink, offline_after_s: float) -> None:
        self._sink = sink
        self._offline_after = offline_after_s
        self._bt: float | None = None
        self._et: float | None = None
        self._tm: float | None = None
        self._last_frame: float | None = None
        self._watch_since: float | None = None

    def handle(self, payload: object, now: float) -> None:
        if not isinstance(payload, dict):
            _LOGGER.debug("Ignoring non-object WebLCDs frame")
            return
        alert = payload.get("alert")
        if isinstance(alert, dict) and alert.get("text") is not None:
            title = alert.get("title")
            self._sink.on_alert(str(alert["text"]), None if title is None else str(title))
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        self._last_frame = now
        # Missing keys keep their previous value; present-but-blank values become None.
        if "bt" in data:
            self._bt = parse_value(data["bt"])
        if "et" in data:
            self._et = parse_value(data["et"])
        if "time" in data:
            self._tm = parse_time(data["time"])
        self._sink.on_sample(self._bt, self._et, self._tm, now)

    def check_liveness(self, now: float) -> None:
        """Mark Artisan offline after `offline_after_s` without a frame while monitoring."""
        if not (self._sink.monitoring and self._sink.artisan_online):
            self._watch_since = None
            return
        if self._watch_since is None:
            # Start the timer when monitoring is first seen, so an `on` event without any
            # following frame still times out.
            self._watch_since = now
        last = self._watch_since
        if self._last_frame is not None and self._last_frame > last:
            last = self._last_frame
        if now - last > self._offline_after:
            _LOGGER.warning("No WebLCDs frame for %.0f s; Artisan considered offline", now - last)
            self._sink.set_artisan_online(False)
            self._watch_since = None

    def disconnected(self) -> None:
        self._sink.set_artisan_online(False)


class WeblcdsClient:
    """`TelemetrySource` that reconnects to WebLCDs forever."""

    def __init__(
        self,
        config: WeblcdsConfig,
        sink: TelemetrySink,
        *,
        offline_after_s: float,
        retry_s: float = RETRY_S,
        liveness_tick_s: float = LIVENESS_TICK_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = config.url
        self._handler = FrameHandler(sink, offline_after_s)
        self._retry_s = retry_s
        self._tick_s = liveness_tick_s
        self._monotonic = monotonic
        self._tasks: list[asyncio.Task[None]] = []
        self.connected = False

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._run(), name="weblcds-client"),
            asyncio.create_task(self._liveness(), name="weblcds-liveness"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _liveness(self) -> None:
        while True:
            await asyncio.sleep(self._tick_s)
            self._handler.check_liveness(self._monotonic())

    async def _run(self) -> None:
        failures = 0
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(self._url, autoping=True) as ws:
                        self.connected = True
                        failures = 0
                        _LOGGER.info("Connected to WebLCDs at %s", self._url)
                        await self._receive(ws)
                    _LOGGER.info("WebLCDs connection closed")
                except (aiohttp.ClientError, OSError, TimeoutError) as err:
                    failures += 1
                    # Log the first failure loudly, then stay quiet while Artisan is closed.
                    log = _LOGGER.warning if failures == 1 else _LOGGER.debug
                    log("Cannot reach WebLCDs at %s: %s", self._url, err)
                if self.connected:
                    # Only a lost connection means Artisan went away; failed retries while it is
                    # closed are covered by the liveness timeout instead.
                    self.connected = False
                    self._handler.disconnected()
                await asyncio.sleep(self._retry_s)

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except ValueError:
                _LOGGER.warning("Ignoring malformed WebLCDs frame")
                continue
            self._handler.handle(payload, self._monotonic())

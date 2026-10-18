"""Wires the components of a running bridge together."""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import __version__
from .alog_watch import AlogWatcher
from .config import Config
from .discovery import Advertiser, Discovery
from .ha_server import PING_INTERVAL_S, HAServer
from .ingest import IngestServer
from .protocol import OutboundMessage
from .sources import EventSource, TelemetrySource
from .state import Enrichment, RoastStateMachine
from .weblcds_client import FrameHandler, WeblcdsClient

_LOGGER = logging.getLogger(__name__)

AdvertiserFactory = Callable[[Config, int, str], Advertiser]


def zeroconf_advertiser(config: Config, port: int, hostname: str) -> Advertiser:
    return Discovery(bridge_id=config.bridge_id, hostname=hostname, bind=config.ha.bind, port=port)


class _NoAdvertiser:
    async def start(self) -> None:
        _LOGGER.info("Zeroconf discovery disabled")

    async def stop(self) -> None:
        return


def no_advertiser(_config: Config, _port: int, _hostname: str) -> Advertiser:
    return _NoAdvertiser()


class Bridge:
    """The HA server and state machine, plus optional live inputs.

    `replay` builds one with `telemetry=False, ingest=False` and feeds `frames` and `state`.
    """

    def __init__(
        self,
        config: Config,
        *,
        clock: Callable[[], float] = time.time,
        hostname: str | None = None,
        advertiser_factory: AdvertiserFactory = zeroconf_advertiser,
        telemetry: bool = True,
        ingest: bool = True,
        alog: bool = True,
        ping_interval_s: float = PING_INTERVAL_S,
    ) -> None:
        self.config = config
        self.hostname = hostname or socket.gethostname()
        self.state = RoastStateMachine(
            self._dispatch, ror_window_s=config.ror_window_s, clock=clock
        )
        self.ha = HAServer(
            bind=config.ha.bind,
            port=config.ha.port,
            token=config.ha.token,
            bridge_id=config.bridge_id,
            unit=config.unit,
            hostname=self.hostname,
            snapshot=self.state.snapshot,
            sample_interval_s=config.ha.sample_interval_s,
            ping_interval_s=ping_interval_s,
            clock=clock,
        )
        self.frames = FrameHandler(self.state, config.offline_after_s)
        self._advertiser_factory = advertiser_factory
        self.ingest: IngestServer | None = None
        self._telemetry: TelemetrySource | None = None
        self._events: EventSource | None = None
        if ingest:
            self.ingest = IngestServer(config.ingest.port, self.state, status=self.status)
            self._events = self.ingest
        if telemetry:
            self._telemetry = WeblcdsClient(
                config.weblcds, self.state, offline_after_s=config.offline_after_s
            )
        self._alog: AlogWatcher | None = None
        if alog and config.autosave_dir is not None:
            self._alog = AlogWatcher(config.autosave_dir, self._enrich)
        self._stops: list[Callable[[], Awaitable[None]]] = []

    def status(self) -> dict[str, Any]:
        """Served on the ingest `/status` route for `doctor`. Never include the token."""
        return {
            "bridge_id": self.config.bridge_id,
            "version": __version__,
            "phase": self.state.phase,
            "artisan_online": self.state.artisan_online,
            "ha_clients": self.ha.client_count,
            "ha_last_seen": self.ha.last_client_seen,
        }

    async def start(self) -> None:
        try:
            await self.ha.start()
            self._stops.append(self.ha.stop)
            advertiser = self._advertiser_factory(self.config, self.ha.port, self.hostname)
            await advertiser.start()
            self._stops.append(advertiser.stop)
            for component in (self._events, self._telemetry, self._alog):
                if component is not None:
                    await component.start()
                    self._stops.append(component.stop)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        while self._stops:
            stop = self._stops.pop()
            try:
                await stop()
            except Exception:
                _LOGGER.exception("Error while stopping a component")

    def _dispatch(self, message: OutboundMessage) -> None:
        if message["type"] == "state":
            _LOGGER.info(
                "Phase %s, Artisan %s",
                message["phase"],
                "online" if message["artisan_online"] else "offline",
            )
        elif message["type"] == "event":
            _LOGGER.info("Event %s at tm=%s", message["event"], message["tm"])
        self.ha.publish(message)

    def _enrich(self, enrichment: Enrichment) -> None:
        if self.state.enrich_summary(enrichment):
            _LOGGER.info("Roast summary enriched from .alog (%s)", enrichment.title)
        else:
            _LOGGER.info("Saved .alog does not match the last roast summary; ignored")

"""Bridge composition: start/stop ordering, status, logging and failure cleanup."""

from __future__ import annotations

import logging
import socket
from pathlib import Path

import aiohttp
import pytest

from artisan_ha_bridge import __version__
from artisan_ha_bridge.app import Bridge, no_advertiser, zeroconf_advertiser
from artisan_ha_bridge.config import Config
from artisan_ha_bridge.discovery import Discovery
from artisan_ha_bridge.state import Enrichment

from helpers import FakeAdvertiser, FakeClock, fake_advertiser_factory, make_config


async def test_full_bridge_lifecycle(tmp_path: Path, clock: FakeClock) -> None:
    config = make_config()
    config.autosave_dir = tmp_path
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        config.weblcds.port = probe.getsockname()[1]  # nothing listens: client keeps retrying
    advertisers: list[FakeAdvertiser] = []
    bridge = Bridge(
        config, clock=clock, hostname="lab", advertiser_factory=fake_advertiser_factory(advertisers)
    )
    await bridge.start()
    try:
        assert bridge.ingest is not None
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{bridge.ingest.port}/status") as response,
        ):
            status = await response.json()
        assert status == {
            "bridge_id": config.bridge_id,
            "version": __version__,
            "phase": "off",
            "artisan_online": False,
            "ha_clients": 0,
            "ha_last_seen": None,
        }
        assert config.ha.token not in str(status)
    finally:
        await bridge.stop()
    assert advertisers[0].stopped
    await bridge.stop()  # idempotent


async def test_start_failure_cleans_up(clock: FakeClock) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        config = make_config()
        config.ingest.port = busy.getsockname()[1]
        advertisers: list[FakeAdvertiser] = []
        bridge = Bridge(
            config,
            clock=clock,
            telemetry=False,
            advertiser_factory=fake_advertiser_factory(advertisers),
        )
        with pytest.raises(OSError):  # noqa: PT011 - errno differs between platforms
            await bridge.start()
    assert advertisers[0].stopped
    assert bridge.ha.client_count == 0


async def test_stop_errors_are_logged(clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    class BrokenAdvertiser(FakeAdvertiser):
        async def stop(self) -> None:
            raise RuntimeError("zeroconf exploded")

    bridge = Bridge(
        make_config(),
        clock=clock,
        telemetry=False,
        ingest=False,
        advertiser_factory=lambda _c, _p, _h: BrokenAdvertiser(),
    )
    await bridge.start()
    with caplog.at_level(logging.ERROR):
        await bridge.stop()
    assert "Error while stopping" in caplog.text


def test_dispatch_logs_and_enrich(clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    bridge = Bridge(make_config(), clock=clock, telemetry=False, ingest=False)
    with caplog.at_level(logging.INFO, logger="artisan_ha_bridge.app"):
        bridge.state.on_event("on", None, None, None)
        bridge.state.on_event("charge", 185.0, 230.0, 0.0)
        bridge._enrich(Enrichment(charge_epoch=clock.now))
        bridge.state.on_event("drop", 205.0, 230.0, 600.0)
        bridge._enrich(Enrichment(charge_epoch=clock.now, title="Kenya"))
    assert "Phase monitoring, Artisan online" in caplog.text
    assert "Event charge at tm=0.0" in caplog.text
    assert "does not match" in caplog.text
    assert "enriched from .alog (Kenya)" in caplog.text


async def test_advertiser_factories(caplog: pytest.LogCaptureFixture) -> None:
    config = Config.generate()
    assert isinstance(zeroconf_advertiser(config, 8765, "host"), Discovery)
    advertiser = no_advertiser(config, 8765, "host")
    with caplog.at_level(logging.INFO):
        await advertiser.start()
        await advertiser.stop()
    assert "discovery disabled" in caplog.text

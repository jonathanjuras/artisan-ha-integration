"""`doctor` against a fake WebLCDs server and a real running bridge (M4)."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from artisan_ha_bridge.app import Bridge
from artisan_ha_bridge.config import Config, save_config
from artisan_ha_bridge.doctor import (
    RECENT_CLIENT_S,
    Check,
    _check_client,
    _local_host,
    format_checks,
    run_checks,
)

from helpers import FakeAdvertiser, FakeWebLCDs, bearer, fake_advertiser_factory, wait_for


def free_ports(count: int) -> list[int]:
    sockets = [socket.socket() for _ in range(count)]
    try:
        for probe in sockets:
            probe.bind(("127.0.0.1", 0))
        return [int(probe.getsockname()[1]) for probe in sockets]
    finally:
        for probe in sockets:
            probe.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    weblcds, ingest, ha = free_ports(3)
    config = Config.generate()
    config.weblcds.port = weblcds
    config.ingest.port = ingest
    config.ha.port = ha
    config.ha.bind = "127.0.0.1"
    return config


@pytest.fixture
def config_path(tmp_path: Path, config: Config) -> Path:
    path = tmp_path / "config.yaml"
    save_config(config, path)
    return path


@pytest.fixture
async def fake_weblcds(config: Config) -> AsyncIterator[FakeWebLCDs]:
    server = FakeWebLCDs()
    await server.start(config.weblcds.port)
    yield server
    await server.stop()


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


async def test_doctor_passes_with_running_bridge(
    config: Config, config_path: Path, fake_weblcds: FakeWebLCDs
) -> None:
    advertisers: list[FakeAdvertiser] = []
    bridge = Bridge(config, advertiser_factory=fake_advertiser_factory(advertisers))
    await bridge.start()
    try:
        await wait_for(lambda: fake_weblcds.open_sockets == 1)
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(
                f"ws://127.0.0.1:{config.ha.port}/v1", headers=bearer(config.ha.token)
            ) as ws,
        ):
            await ws.receive(timeout=2)
            await wait_for(lambda: bridge.ha.client_count == 1)
            checks = await run_checks(config_path)
            report = format_checks(checks)
            assert all(check.ok for check in checks), report
            assert [check.name for check in checks] == [
                "Config", "WebLCDs", "Ingest", "HA server", "HA client",
            ]  # fmt: skip
            assert report.endswith("All checks passed.")
            assert "1 connected" in report
            assert config.ha.token not in report
    finally:
        await bridge.stop()


async def test_doctor_fails_when_nothing_runs(config_path: Path) -> None:
    checks = by_name(await run_checks(config_path, limit_s=1.0))
    assert checks["Config"].ok
    assert not checks["WebLCDs"].ok
    assert "WebLCDs enabled" in checks["WebLCDs"].detail
    assert not checks["Ingest"].ok
    assert "is `artisan-ha-bridge run` running?" in checks["Ingest"].detail
    assert not checks["HA server"].ok
    assert not checks["HA client"].ok
    assert "4 check(s) failed." in format_checks(list(checks.values()))


async def test_doctor_reports_config_problems(tmp_path: Path, config: Config) -> None:
    config.unit = "K"
    path = tmp_path / "config.yaml"
    save_config(config, path)
    checks = by_name(await run_checks(path, limit_s=0.5))
    assert not checks["Config"].ok
    assert "unit must be C or F" in checks["Config"].detail


async def _serve(port: int, handler: Callable[[web.Request], Any]) -> web.AppRunner:
    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


@pytest.mark.parametrize(
    ("status_body", "expected"),
    [
        ({"hello": 1}, "used by another program"),
        ({"bridge_id": "someone-else"}, "different config"),
        ([1, 2], "used by another program"),
    ],
)
async def test_doctor_detects_impostors(
    config: Config, config_path: Path, status_body: object, expected: str
) -> None:
    async def status(_request: web.Request) -> web.StreamResponse:
        return web.json_response(status_body)

    async def ok(_request: web.Request) -> web.StreamResponse:
        return web.Response(text="hi")

    runners = [await _serve(config.ingest.port, status), await _serve(config.ha.port, ok)]
    try:
        checks = by_name(await run_checks(config_path, limit_s=1.0))
    finally:
        for runner in runners:
            await runner.cleanup()
    assert expected in checks["Ingest"].detail
    assert "answered 200, expected 401" in checks["HA server"].detail


async def test_doctor_ingest_not_json(config: Config, config_path: Path) -> None:
    async def html(_request: web.Request) -> web.StreamResponse:
        return web.Response(text="<html>", content_type="text/html")

    runner = await _serve(config.ingest.port, html)
    try:
        checks = by_name(await run_checks(config_path, limit_s=1.0))
    finally:
        await runner.cleanup()
    assert not checks["Ingest"].ok


def test_check_client() -> None:
    now = 10_000.0
    assert _check_client({"ha_clients": 2}, now).ok
    recent = _check_client({"ha_clients": 0, "ha_last_seen": now - 60}, now)
    assert recent.ok
    assert "last seen 60 s ago" in recent.detail
    stale = _check_client({"ha_clients": 0, "ha_last_seen": now - RECENT_CLIENT_S - 1}, now)
    assert not stale.ok
    assert "show-token" in stale.detail
    assert not _check_client({"ha_clients": 0, "ha_last_seen": None}, now).ok
    assert not _check_client(None, now).ok


@pytest.mark.parametrize(
    ("bind", "host"),
    [("0.0.0.0", "127.0.0.1"), ("", "127.0.0.1"), ("::", "[::1]"), ("192.168.1.5", "192.168.1.5")],
)
def test_local_host(bind: str, host: str) -> None:
    assert _local_host(bind) == host

"""Ingest server with real HTTP and WebSocket clients."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import aiohttp
import pytest

from artisan_ha_bridge.ingest import IngestServer
from artisan_ha_bridge.state import RoastStateMachine

from helpers import FakeClock, Recorder, wait_for


@pytest.fixture
def sm(recorder: Recorder, clock: FakeClock) -> RoastStateMachine:
    return RoastStateMachine(recorder, clock=clock)


@pytest.fixture
async def ingest(sm: RoastStateMachine) -> AsyncIterator[IngestServer]:
    server = IngestServer(0, sm, status=lambda: {"phase": sm.phase})
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as http:
        yield http


def url(server: IngestServer, path: str, scheme: str = "http") -> str:
    return f"{scheme}://127.0.0.1:{server.port}{path}"


async def test_emit_post(
    ingest: IngestServer, session: aiohttp.ClientSession, sm: RoastStateMachine
) -> None:
    body = {"k": "ev", "e": "charge", "bt": 185.2, "et": 220.1, "tm": 0.0, "id": 48211}
    async with session.post(url(ingest, "/emit"), json=body) as response:
        assert response.status == 204
    assert sm.phase == "drying"
    async with session.post(url(ingest, "/emit"), json={"k": "ctl", "ch": "heat", "v": 70}) as r:
        assert r.status == 204
    assert sm.snapshot()["controls"] == {"heat": 70.0}


@pytest.mark.parametrize(
    "body",
    [
        b'{"k":"ev","e":"bogus"}',
        b'{"k":"zz"}',
        b"[]",
        b"not json",
        b'{"k":"ctl","ch":"BAD","v":1}',
    ],
)
async def test_emit_post_schema_errors(
    ingest: IngestServer,
    session: aiohttp.ClientSession,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
    body: bytes,
) -> None:
    with caplog.at_level(logging.WARNING):
        async with session.post(
            url(ingest, "/emit"), data=body, headers={"Content-Type": "application/json"}
        ) as response:
            assert response.status == 400
    assert recorder.messages == []
    assert "Dropping" in caplog.text


async def test_websocket_ingest(
    ingest: IngestServer,
    session: aiohttp.ClientSession,
    sm: RoastStateMachine,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        async with session.ws_connect(url(ingest, "/artisan", "ws")) as ws:
            await ws.send_str('{"k":"ev","e":"on","bt":-1,"et":-1,"tm":-1,"id":7}')
            await ws.send_str('{"k":"ev","e":"explode","id":8}')
            await ws.send_str("{broken")
            await ws.send_bytes(b"\x01")
            await ws.send_json({"k": "ctl", "ch": "fan", "v": 40.0, "id": 9, "machine": 1})
            await wait_for(lambda: "control" in recorder.types())
            # The ingest never replies.
            with pytest.raises(asyncio.TimeoutError):
                await ws.receive(timeout=0.1)
    assert sm.phase == "monitoring"
    assert sm.snapshot()["controls"] == {"fan": 40.0}
    assert "unknown event 'explode'" in caplog.text
    assert "malformed ingest frame" in caplog.text


async def test_status(ingest: IngestServer, session: aiohttp.ClientSession) -> None:
    async with session.get(url(ingest, "/status")) as response:
        assert response.status == 200
        assert await response.json() == {"phase": "off"}


async def test_status_without_provider(
    sm: RoastStateMachine, session: aiohttp.ClientSession
) -> None:
    server = IngestServer(0, sm)
    assert server.port == 0
    await server.start()
    try:
        assert server.port != 0
        async with session.get(url(server, "/status")) as response:
            assert await response.json() == {}
    finally:
        await server.stop()
    await server.stop()


async def test_port_conflict_raises(ingest: IngestServer, sm: RoastStateMachine) -> None:
    other = IngestServer(ingest.port, sm)
    with pytest.raises(OSError):  # noqa: PT011 - errno differs between platforms
        await other.start()
    await other.stop()

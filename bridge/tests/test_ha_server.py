"""HA WebSocket server: auth, framing, throttling, pings and client removal."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from artisan_ha_bridge import __version__
from artisan_ha_bridge.ha_server import QUEUE_SIZE, HAServer
from artisan_ha_bridge.protocol import build_control, build_event, build_sample
from artisan_ha_bridge.state import RoastStateMachine

from helpers import FakeClock, Recorder, bearer, wait_for

TOKEN = "correct-horse-battery-staple-0123456789"


def make_server(sm: RoastStateMachine, clock: FakeClock, **kwargs: Any) -> HAServer:
    options: dict[str, Any] = {"sample_interval_s": 0.2, "ping_interval_s": 60.0} | kwargs
    return HAServer(
        bind="127.0.0.1",
        port=0,
        token=TOKEN,
        bridge_id="b7d4c1f0-0000-4000-8000-000000000001",
        unit="C",
        hostname="roaster-laptop",
        snapshot=sm.snapshot,
        clock=clock,
        **options,
    )


@pytest.fixture
def sm(recorder: Recorder, clock: FakeClock) -> RoastStateMachine:
    return RoastStateMachine(recorder, clock=clock)


@pytest.fixture
async def server(sm: RoastStateMachine, clock: FakeClock) -> AsyncIterator[HAServer]:
    ha = make_server(sm, clock)
    await ha.start()
    yield ha
    await ha.stop()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as http:
        yield http


def ws_url(server: HAServer, path: str = "/v1") -> str:
    return f"ws://127.0.0.1:{server.port}{path}"


async def receive(ws: aiohttp.ClientWebSocketResponse, within: float = 1.0) -> dict[str, Any]:
    message = await ws.receive(timeout=within)
    assert message.type is aiohttp.WSMsgType.TEXT, message
    data: dict[str, Any] = json.loads(message.data)
    return data


async def drain(ws: aiohttp.ClientWebSocketResponse, quiet: float = 0.3) -> list[dict[str, Any]]:
    """Read until nothing arrives for `quiet` seconds."""
    received: list[dict[str, Any]] = []
    while True:
        try:
            message = await ws.receive(timeout=quiet)
        except TimeoutError:
            return received
        if message.type is not aiohttp.WSMsgType.TEXT:
            return received
        received.append(json.loads(message.data))


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"Authorization": f"Bearer {TOKEN}x"},
    ],
)
async def test_bad_token_is_401_before_upgrade(
    server: HAServer, session: aiohttp.ClientSession, headers: dict[str, str]
) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
        await session.ws_connect(ws_url(server), headers=headers)
    assert excinfo.value.status == 401
    async with session.get(f"http://127.0.0.1:{server.port}/v1", headers=headers) as response:
        assert response.status == 401
    assert server.client_count == 0


async def test_hello_then_snapshot(
    server: HAServer, session: aiohttp.ClientSession, sm: RoastStateMachine
) -> None:
    sm.on_event("on", None, None, None)
    sm.on_control("heat", 70.0)
    assert server.last_client_seen is None
    async with session.ws_connect(ws_url(server), headers=bearer(TOKEN)) as ws:
        hello = await receive(ws)
        assert hello == {
            "v": 1,
            "type": "hello",
            "ts": 1792245792000,
            "bridge_version": __version__,
            "bridge_id": "b7d4c1f0-0000-4000-8000-000000000001",
            "hostname": "roaster-laptop",
            "unit": "C",
            "protocol": 1,
        }
        snapshot = await receive(ws)
        assert snapshot["type"] == "snapshot"
        assert snapshot["v"] == 1
        assert snapshot["ts"] == 1792245792000
        assert snapshot["state"] == json.loads(json.dumps(sm.snapshot()))
        assert snapshot["state"]["controls"] == {"heat": 70.0}
        await wait_for(lambda: server.client_count == 1)
        assert server.last_client_seen == 1792245792.0
        await ws.send_str("ignored")  # inbound frames are ignored
    await wait_for(lambda: server.client_count == 0)


async def test_samples_throttled_other_messages_not(
    server: HAServer, session: aiohttp.ClientSession
) -> None:
    async with session.ws_connect(ws_url(server), headers=bearer(TOKEN)) as ws:
        await receive(ws)
        await receive(ws)
        await wait_for(lambda: server.client_count == 1)
        for i in range(50):
            server.publish(
                build_sample(i, tm=float(i), bt=float(i), et=None, bt_ror=None, et_ror=None)
            )
            if i % 10 == 0:
                server.publish(
                    build_event(i, event="dry", tm=None, bt=None, et=None, roast_id=None)
                )
            if i == 25:
                server.publish(build_control(i, channel="heat", value=1.0))
        received = await drain(ws, quiet=0.5)
    samples = [m for m in received if m["type"] == "sample"]
    # The first sample goes out immediately; the rest collapse into the latest one.
    assert [s["bt"] for s in samples] == [0.0, 49.0]
    assert [m["ts"] for m in received if m["type"] == "event"] == [0, 10, 20, 30, 40]
    assert len([m for m in received if m["type"] == "control"]) == 1


async def test_sample_interval_spacing(server: HAServer, session: aiohttp.ClientSession) -> None:
    async with session.ws_connect(ws_url(server), headers=bearer(TOKEN)) as ws:
        await receive(ws)
        await receive(ws)
        await wait_for(lambda: server.client_count == 1)
        loop = asyncio.get_running_loop()
        arrivals: list[tuple[float, dict[str, Any]]] = []

        async def read() -> None:
            while True:
                data = await receive(ws, within=0.5)
                arrivals.append((loop.time(), data))

        reader = asyncio.create_task(read())
        for i in range(12):
            server.publish(build_sample(i, tm=None, bt=float(i), et=None, bt_ror=None, et_ror=None))
            await asyncio.sleep(0.05)
        with pytest.raises(TimeoutError):
            await reader
    # 0.6 s of samples at a 0.2 s interval: about four sends, never closer than the interval.
    assert 3 <= len(arrivals) <= 5
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(arrivals)]
    assert all(gap >= 0.15 for gap in gaps), gaps
    assert arrivals[-1][1]["bt"] == 11.0


async def test_ping(
    sm: RoastStateMachine, clock: FakeClock, session: aiohttp.ClientSession
) -> None:
    server = make_server(sm, clock, ping_interval_s=0.05)
    await server.start()
    try:
        async with session.ws_connect(ws_url(server), headers=bearer(TOKEN)) as ws:
            await receive(ws)
            await receive(ws)
            assert await receive(ws) == {"v": 1, "type": "ping", "ts": 1792245792000}
    finally:
        await server.stop()


async def test_queue_overflow_drops_client(
    server: HAServer, session: aiohttp.ClientSession
) -> None:
    async with session.ws_connect(ws_url(server), headers=bearer(TOKEN)) as ws:
        await receive(ws)
        await receive(ws)
        await wait_for(lambda: server.client_count == 1)
        # No await in between: the writer cannot drain, so the bounded queue overflows.
        for i in range(QUEUE_SIZE + 1):
            server.publish(build_control(i, channel="heat", value=float(i)))
        assert server.client_count == 0
        await drain(ws)
        assert ws.closed or (await ws.receive(timeout=1)).type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
        )


async def test_send_failure_drops_client(
    server: HAServer, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session.ws_connect(ws_url(server), headers=bearer(TOKEN)) as ws:
        await receive(ws)
        await receive(ws)
        await wait_for(lambda: server.client_count == 1)
        (client,) = server._clients

        async def broken(_data: str) -> None:
            raise ConnectionResetError("peer went away")

        monkeypatch.setattr(client.ws, "send_str", broken)
        server.publish(build_control(1, channel="heat", value=1.0))
        await wait_for(lambda: server.client_count == 0)
        await drain(ws)


async def test_pending_sample_discarded_on_drop(
    server: HAServer, session: aiohttp.ClientSession
) -> None:
    async with session.ws_connect(ws_url(server), headers=bearer(TOKEN)) as ws:
        await receive(ws)
        await receive(ws)
        await wait_for(lambda: server.client_count == 1)
        (client,) = server._clients
        server.publish(build_sample(1, tm=None, bt=1.0, et=None, bt_ror=None, et_ror=None))
        server.publish(build_sample(2, tm=None, bt=2.0, et=None, bt_ror=None, et_ror=None))
        assert client.sample_timer is not None
        timer = client.sample_timer
        await ws.close()
        await wait_for(lambda: server.client_count == 0)
        assert timer.cancelled()
        # A timer that fires after the drop must not resurrect the client.
        server._flush_sample(client)
        assert client.queue.empty() or client.dropped


async def test_stop_closes_clients(sm: RoastStateMachine, clock: FakeClock) -> None:
    server = make_server(sm, clock)
    assert server.port == 0
    await server.start()
    async with (
        aiohttp.ClientSession() as session,
        session.ws_connect(ws_url(server), headers=bearer(TOKEN)) as ws,
    ):
        await receive(ws)
        await receive(ws)
        await wait_for(lambda: server.client_count == 1)
        await server.stop()
        message = await ws.receive(timeout=2)
        assert message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    await server.stop()


async def test_port_conflict(server: HAServer, sm: RoastStateMachine, clock: FakeClock) -> None:
    other = make_server(sm, clock)
    other._port = server.port
    with pytest.raises(OSError):  # noqa: PT011 - errno differs between platforms
        await other.start()

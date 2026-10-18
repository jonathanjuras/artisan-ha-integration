"""WebLCDs parsing, frame handling, liveness and the reconnecting client."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import AsyncIterator

import pytest

from artisan_ha_bridge.config import WeblcdsConfig
from artisan_ha_bridge.state import RoastStateMachine
from artisan_ha_bridge.weblcds_client import FrameHandler, WeblcdsClient, parse_time, parse_value

from helpers import FakeClock, FakeWebLCDs, Recorder, wait_for


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("185.2", 185.2),
        (" 185.2 ", 185.2),
        ("-5.0", -5.0),
        ("0", 0.0),
        ("&#49;85.2", 185.2),
        ("&nbsp;--", None),
        ("&nbsp;&nbsp;-.-", None),
        ("\u00a0--", None),
        ("--", None),
        ("-.-", None),
        ("", None),
        (".", None),
        ("1.2.3", None),
        ("185.2C", None),
        (185, 185.0),
        (185.5, 185.5),
        (True, None),
        (None, None),
        ([1], None),
    ],
)
def test_parse_value(raw: object, expected: float | None) -> None:
    assert parse_value(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00:00", 0.0),
        ("10:20", 620.0),
        ("0:07", 7.0),
        ("-0:05", -5.0),
        ("-1:05", -65.0),
        ("1:02:03", 3723.0),
        ("&nbsp;01:00", 60.0),
        ("--:--", None),
        ("", None),
        ("12", None),
        (65, None),
        (None, None),
    ],
)
def test_parse_time(raw: object, expected: float | None) -> None:
    assert parse_time(raw) == expected


@pytest.fixture
def sm(recorder: Recorder, clock: FakeClock) -> RoastStateMachine:
    return RoastStateMachine(recorder, clock=clock)


def samples(recorder: Recorder) -> list[tuple[object, object, object]]:
    return [(m["bt"], m["et"], m["tm"]) for m in recorder.of_type("sample")]


def test_partial_frames_keep_previous_values(sm: RoastStateMachine, recorder: Recorder) -> None:
    handler = FrameHandler(sm, offline_after_s=10.0)
    handler.handle({"data": {"bt": "185.2", "et": "220.1", "time": "00:00"}}, now=0.0)
    handler.handle({"data": {"bt": "184.0"}}, now=1.0)
    handler.handle({"data": {"et": "&nbsp;&nbsp;-.-", "time": "-0:05"}}, now=2.0)
    handler.handle({"data": {}}, now=3.0)
    assert samples(recorder) == [
        (185.2, 220.1, 0.0),
        (184.0, 220.1, 0.0),
        (184.0, None, -5.0),
        (184.0, None, -5.0),
    ]


def test_alerts_and_ignored_frames(sm: RoastStateMachine, recorder: Recorder) -> None:
    handler = FrameHandler(sm, offline_after_s=10.0)
    handler.handle({"alert": {"text": "Drop now", "title": "Alarm", "timeout": 5}}, now=0.0)
    handler.handle({"alert": {"text": "Untitled"}}, now=0.0)
    handler.handle({"alert": {"title": "no text"}}, now=0.0)
    handler.handle({"alert": "bogus"}, now=0.0)
    handler.handle(["not", "an", "object"], now=0.0)
    handler.handle({"data": "bogus"}, now=0.0)
    assert recorder.types() == ["alert", "alert"]
    assert recorder.of_type("alert")[0]["title"] == "Alarm"
    assert recorder.of_type("alert")[1]["title"] is None
    assert sm.phase == "off"


def test_liveness(sm: RoastStateMachine, recorder: Recorder) -> None:
    handler = FrameHandler(sm, offline_after_s=10.0)
    handler.check_liveness(0.0)  # off: nothing to watch
    handler.handle({"data": {"bt": "200"}}, now=100.0)
    handler.check_liveness(100.0)
    handler.check_liveness(105.0)
    handler.check_liveness(110.0)
    assert sm.artisan_online
    handler.check_liveness(110.5)
    assert not sm.artisan_online
    assert sm.phase == "monitoring"
    handler.check_liveness(200.0)  # already offline: no repeat
    assert [m["artisan_online"] for m in recorder.of_type("state")] == [True, False]
    handler.handle({"data": {"bt": "200"}}, now=201.0)
    assert sm.artisan_online


def test_liveness_on_event_without_frames(sm: RoastStateMachine) -> None:
    handler = FrameHandler(sm, offline_after_s=10.0)
    sm.on_event("on", None, None, None)
    handler.check_liveness(50.0)  # timer starts now
    handler.check_liveness(60.0)
    assert sm.artisan_online
    handler.check_liveness(60.1)
    assert not sm.artisan_online


def test_disconnect_is_offline_immediately(sm: RoastStateMachine) -> None:
    handler = FrameHandler(sm, offline_after_s=10.0)
    handler.handle({"data": {"bt": "200"}}, now=0.0)
    handler.disconnected()
    assert not sm.artisan_online


# --- client against a fake server ------------------------------------------------------------


@pytest.fixture
async def fake_weblcds() -> AsyncIterator[FakeWebLCDs]:
    server = FakeWebLCDs()
    await server.start()
    yield server
    await server.stop()


async def test_client_receives_frames_and_reconnects(
    fake_weblcds: FakeWebLCDs, sm: RoastStateMachine, recorder: Recorder
) -> None:
    client = WeblcdsClient(
        WeblcdsConfig(port=fake_weblcds.port),
        sm,
        offline_after_s=30.0,
        retry_s=0.05,
        liveness_tick_s=0.05,
    )
    await client.start()
    try:
        await wait_for(lambda: fake_weblcds.open_sockets == 1)
        assert client.connected
        await fake_weblcds.send({"data": {"bt": "&nbsp;--", "et": "230.0", "time": "00:00"}})
        await fake_weblcds.send("{not json")
        await fake_weblcds.send_bytes(b"\x00")
        await fake_weblcds.send({"data": {"bt": "200.1"}})
        await wait_for(lambda: len(recorder.of_type("sample")) == 2)
        assert samples(recorder) == [(None, 230.0, 0.0), (200.1, 230.0, 0.0)]
        assert sm.artisan_online

        await fake_weblcds.drop_clients()
        await wait_for(lambda: not sm.artisan_online)
        assert sm.phase == "monitoring"
        await wait_for(lambda: fake_weblcds.connections == 2 and fake_weblcds.open_sockets == 1)
        await fake_weblcds.send({"data": {"bt": "201.0"}})
        await wait_for(lambda: sm.artisan_online)
    finally:
        await client.stop()
    await client.stop()  # idempotent


async def test_client_liveness_timeout_with_open_socket(
    fake_weblcds: FakeWebLCDs, sm: RoastStateMachine
) -> None:
    client = WeblcdsClient(
        WeblcdsConfig(port=fake_weblcds.port),
        sm,
        offline_after_s=0.2,
        retry_s=0.05,
        liveness_tick_s=0.02,
    )
    await client.start()
    try:
        await wait_for(lambda: fake_weblcds.open_sockets == 1)
        await fake_weblcds.send({"data": {"bt": "200.0", "time": "00:00"}})
        await wait_for(lambda: sm.artisan_online)
        await wait_for(lambda: not sm.artisan_online)
        assert fake_weblcds.open_sockets == 1
    finally:
        await client.stop()


async def test_client_retries_while_artisan_closed(
    sm: RoastStateMachine, caplog: pytest.LogCaptureFixture
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = WeblcdsClient(WeblcdsConfig(port=port), sm, offline_after_s=10.0, retry_s=0.01)
    with caplog.at_level(logging.DEBUG, logger="artisan_ha_bridge.weblcds_client"):
        await client.start()
        await wait_for(lambda: caplog.text.count("Cannot reach WebLCDs") >= 3)
        await client.stop()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert not client.connected


async def test_failed_retries_do_not_mark_offline(sm: RoastStateMachine) -> None:
    """Ingest events prove Artisan runs even when WebLCDs is unreachable; only the liveness
    timeout (not each failed retry) may mark it offline."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = WeblcdsClient(
        WeblcdsConfig(port=port), sm, offline_after_s=0.3, retry_s=0.01, liveness_tick_s=0.02
    )
    sm.on_event("on", None, None, None)
    await client.start()
    try:
        await asyncio.sleep(0.15)
        assert sm.artisan_online
        await wait_for(lambda: not sm.artisan_online)
    finally:
        await client.stop()

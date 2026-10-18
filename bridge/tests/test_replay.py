"""Recorded fixtures through the real pipeline, including the M3 end-to-end test."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from artisan_ha_bridge.app import Bridge
from artisan_ha_bridge.replay import ReplayRecord, load_records, parse_records, replay
from artisan_ha_bridge.state import RoastStateMachine
from artisan_ha_bridge.weblcds_client import FrameHandler

from helpers import (
    FIXTURES,
    FakeAdvertiser,
    FakeClock,
    Recorder,
    bearer,
    fake_advertiser_factory,
    make_config,
    wait_for,
)

FAST = 1e6  # effectively "as fast as possible" while keeping fixture time for RoR/liveness


async def run_fixture(
    name: str, recorder: Recorder, clock: FakeClock, *, linger_until: Any = None
) -> RoastStateMachine:
    sm = RoastStateMachine(recorder, clock=clock)
    frames = FrameHandler(sm, offline_after_s=10.0)
    records = load_records(FIXTURES / name)
    if linger_until is None:
        await replay(records, frames=frames, sink=sm, speed=FAST)
        return sm
    task = asyncio.create_task(replay(records, frames=frames, sink=sm, speed=FAST, linger=True))
    await wait_for(lambda: linger_until(sm))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return sm


def events(recorder: Recorder) -> list[str]:
    return [m["event"] for m in recorder.of_type("event")]


def check_full_roast(sm: RoastStateMachine, recorder: Recorder) -> None:
    assert sm.phase == "off"
    preliminary, final = recorder.of_type("summary")
    assert preliminary["final"] is False
    assert final["final"] is True
    expected = {
        "roast_id": "2026-10-17T14:03:12Z",
        "charge_bt": 185.2,
        "dry_tm": 245.0,
        "dry_bt": 152.1,
        "fcs_tm": 464.0,
        "fcs_bt": 194.0,
        "drop_tm": 620.0,
        "drop_bt": 208.4,
        "dev_time": 156.0,
        "dtr": 25.2,
    }
    for key, value in expected.items():
        assert preliminary[key] == value, key
        assert final[key] == value, key
    assert preliminary["cool_tm"] is None
    assert final["cool_tm"] == 740.0
    snap = sm.snapshot()
    assert snap["controls"] == {"heat": 50.0, "fan": 100.0}
    assert snap["last_summary"] == final
    assert [a["text"] for a in recorder.of_type("alert")] == ["Drop in 10 s"]
    # BT RoR in mid-development is a plausible roast value.
    rors = [
        s["bt_ror"] for s in recorder.of_type("sample") if s["tm"] is not None and s["tm"] == 540
    ]
    assert rors
    assert 3.0 < rors[0] < 10.0
    # The ET none-symbol frame at 6:40 arrives as null.
    assert any(s["et"] is None and s["tm"] == 400 for s in recorder.of_type("sample"))


async def test_full_roast_fixture(recorder: Recorder, clock: FakeClock) -> None:
    sm = await run_fixture("full_roast.jsonl", recorder, clock)
    assert events(recorder) == ["on", "start", "charge", "dry", "fcs", "drop", "cool", "off"]
    phases = [m["phase"] for m in recorder.of_type("state")]
    assert phases == [
        "monitoring", "recording", "drying", "maillard", "development", "cooling", "finished",
        "off",
    ]  # fmt: skip
    check_full_roast(sm, recorder)


async def test_dropped_on_fixture(recorder: Recorder, clock: FakeClock) -> None:
    sm = await run_fixture("dropped_on.jsonl", recorder, clock)
    assert events(recorder)[0] == "start"
    # The first WebLCDs frame starts monitoring on its own.
    assert recorder.types()[:2] == ["state", "sample"]
    assert recorder.of_type("state")[0]["phase"] == "monitoring"
    check_full_roast(sm, recorder)


async def test_reset_mid_roast_fixture(recorder: Recorder, clock: FakeClock) -> None:
    sm = await run_fixture("reset_mid_roast.jsonl", recorder, clock)
    assert events(recorder) == ["on", "start", "charge", "dry", "reset"]
    assert sm.phase == "monitoring"
    assert sm.roast_id is None
    assert sm.snapshot()["events"] == {}
    assert recorder.of_type("summary") == []
    assert sm.artisan_online


async def test_artisan_crash_fixture(recorder: Recorder, clock: FakeClock) -> None:
    sm = await run_fixture(
        "artisan_crash.jsonl",
        recorder,
        clock,
        linger_until=lambda s: s.phase != "off" and not s.artisan_online,
    )
    assert sm.phase == "maillard"
    assert sm.roast_id is not None
    last_state = recorder.of_type("state")[-1]
    assert last_state["artisan_online"] is False
    assert last_state["phase"] == "maillard"
    assert recorder.of_type("summary") == []


async def test_replay_paces_by_speed(recorder: Recorder, clock: FakeClock) -> None:
    sm = RoastStateMachine(recorder, clock=clock)
    frames = FrameHandler(sm, offline_after_s=10.0)
    records = [
        ReplayRecord(0.0, "weblcds", {"data": {"bt": "1"}}),
        ReplayRecord(2.0, "weblcds", {"data": {"bt": "2"}}),
        ReplayRecord(4.0, "ingest", {"k": "ev", "e": "bogus"}),
    ]
    loop = asyncio.get_running_loop()
    started = loop.time()
    await replay(records, frames=frames, sink=sm, speed=20.0)
    assert 0.18 <= loop.time() - started < 1.0
    assert len(recorder.of_type("sample")) == 2
    with pytest.raises(ValueError, match="positive"):
        await replay(records, frames=frames, sink=sm, speed=0)


def test_parse_records_errors(tmp_path: Path) -> None:
    good = '{"dt": 0, "src": "weblcds", "msg": {}}'
    assert parse_records([good, "", "  "]) == [ReplayRecord(0.0, "weblcds", {})]
    for line, match in [
        ("nope", "invalid JSON"),
        ("[]", "expected an object"),
        ('{"dt": "0", "src": "weblcds"}', "'dt' must be a number"),
        ('{"dt": true, "src": "weblcds"}', "'dt' must be a number"),
        ('{"dt": 0, "src": "serial"}', "'src' must be"),
    ]:
        with pytest.raises(ValueError, match=match):
            parse_records([good, line], "x.jsonl")
    path = tmp_path / "r.jsonl"
    path.write_text(good + "\n")
    assert len(load_records(path)) == 1


# --- M3 end-to-end -----------------------------------------------------------------------


@pytest.fixture
async def bridge(clock: FakeClock) -> AsyncIterator[tuple[Bridge, list[FakeAdvertiser]]]:
    advertisers: list[FakeAdvertiser] = []
    config = make_config(sample_interval_s=0.05)
    instance = Bridge(
        config,
        clock=clock,
        hostname="roaster-laptop",
        telemetry=False,
        ingest=False,
        alog=False,
        advertiser_factory=fake_advertiser_factory(advertisers),
    )
    await instance.start()
    yield instance, advertisers
    await instance.stop()


async def test_replay_to_home_assistant_client(
    bridge: tuple[Bridge, list[FakeAdvertiser]],
) -> None:
    instance, advertisers = bridge
    assert advertisers[0].started
    assert advertisers[0].port == instance.ha.port
    url = f"ws://127.0.0.1:{instance.ha.port}/v1"
    records = load_records(FIXTURES / "full_roast.jsonl")
    frame_count = sum(r.src == "weblcds" for r in records)
    speed = 400.0
    received: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
            await session.ws_connect(url, headers=bearer("not-the-token"))
        assert excinfo.value.status == 401

        async with session.ws_connect(url, headers=bearer(instance.config.ha.token)) as ws:
            received.append(json.loads((await ws.receive(timeout=2)).data))
            received.append(json.loads((await ws.receive(timeout=2)).data))
            task = asyncio.create_task(
                replay(records, frames=instance.frames, sink=instance.state, speed=speed)
            )
            while True:
                message = await ws.receive(timeout=5)
                assert message.type is aiohttp.WSMsgType.TEXT
                data = json.loads(message.data)
                received.append(data)
                if data["type"] == "state" and data["phase"] == "off":
                    break
            await task

    types = [m["type"] for m in received]
    assert types[:2] == ["hello", "snapshot"]
    assert received[0]["bridge_id"] == instance.config.bridge_id
    assert received[1]["state"]["phase"] == "off"
    for item in received:
        assert item["v"] == 1
        assert isinstance(item["ts"], int)

    sequence = [
        f"event:{m['event']}" if m["type"] == "event" else f"summary:{m['final']}"
        for m in received
        if m["type"] in ("event", "summary")
    ]
    assert sequence == [
        "event:on", "event:start", "event:charge", "event:dry", "event:fcs", "event:drop",
        "summary:False", "event:cool", "summary:True", "event:off",
    ]  # fmt: skip

    samples = [m for m in received if m["type"] == "sample"]
    duration = records[-1].dt / speed
    assert 5 <= len(samples) <= duration / 0.05 + 5
    assert len(samples) < frame_count / 10
    assert [m["channel"] for m in received if m["type"] == "control"] == [
        "heat", "fan", "heat", "heat", "fan", "fan",
    ]  # fmt: skip
    assert [m["text"] for m in received if m["type"] == "alert"] == ["Drop in 10 s"]
    summaries = [m for m in received if m["type"] == "summary"]
    assert summaries[1]["dtr"] == 25.2
    assert summaries[1]["cool_tm"] == 740.0

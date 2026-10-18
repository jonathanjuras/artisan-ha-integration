"""`.alog` parsing, enrichment and the watchdog-based watcher."""

from __future__ import annotations

import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from artisan_ha_bridge import alog_watch
from artisan_ha_bridge.alog_watch import AlogError, AlogWatcher, parse_alog, read_alog
from artisan_ha_bridge.state import Enrichment, RoastStateMachine

from helpers import EPOCH, FIXTURES, FakeClock, Recorder, wait_for

SAMPLE = FIXTURES / "sample_roast.alog"


def profile(**fields: Any) -> str:
    base: dict[str, Any] = {"roastepoch": 1792245762, "timex": [0.0, 1.0], "timeindex": [-1]}
    base.update(fields)
    return repr(base)


def test_sample_fixture_parses() -> None:
    enrichment = read_alog(SAMPLE)
    assert enrichment.charge_epoch == EPOCH  # roastepoch + timex[CHARGE index]
    assert enrichment.title == "Ethiopia Guji"
    assert enrichment.beans == "Ethiopia Guji natural, grade 1"
    assert enrichment.weight_in_g == 250.0
    assert enrichment.weight_out_g == 212.5
    assert enrichment.fills == {
        "charge_bt": 185.2,
        "dry_tm": 245.0,
        "dry_bt": 152.1,
        "fcs_tm": 464.0,
        "fcs_bt": 194.0,
        "drop_tm": 620.0,
        "drop_bt": 208.4,
        "dev_time": 156.0,
        "cool_tm": 740.0,
    }


def test_sample_fixture_enriches_summary(recorder: Recorder, clock: FakeClock) -> None:
    sm = RoastStateMachine(recorder, clock=clock)
    sm.on_event("on", None, None, None)
    sm.on_event("charge", 185.2, 232.0, 0.0)
    sm.on_event("fcs", 194.0, 224.0, 464.0)
    clock.advance(620)
    sm.on_event("drop", 208.4, 229.0, 620.0)
    clock.advance(140)
    sm.on_event("cool", 55.0, 100.0, 740.0)
    recorder.clear()
    assert sm.enrich_summary(read_alog(SAMPLE))
    (summary,) = recorder.of_type("summary")
    assert summary["final"] is True
    assert summary["roast_id"] == "2026-10-17T14:03:12Z"
    assert summary["title"] == "Ethiopia Guji"
    assert summary["beans"] == "Ethiopia Guji natural, grade 1"
    assert summary["weight_in_g"] == 250.0
    assert summary["weight_out_g"] == 212.5
    assert summary["dry_tm"] == 245.0  # missing at the bridge, filled from `computed`
    assert summary["dev_time"] == 156.0
    assert summary["dtr"] == 25.2


@pytest.mark.parametrize(
    ("weight", "expected"),
    [
        ([250.0, 212.5, "g"], (250.0, 212.5)),
        ([0.25, 0.2125, "Kg"], (250.0, 212.5)),
        ([1, 0, "lb"], (453.6, None)),
        ([8, 7, "oz"], (226.8, 198.4)),
        ([250, 200, "stone"], (None, None)),
        ([250, 200, 3], (None, None)),
        (["a", None, "g"], (None, None)),
        ([250, 200], (None, None)),
        ("250g", (None, None)),
    ],
)
def test_weights(weight: object, expected: tuple[float | None, float | None]) -> None:
    enrichment = parse_alog(profile(weight=weight))
    assert (enrichment.weight_in_g, enrichment.weight_out_g) == expected


def test_minimal_profile() -> None:
    enrichment = parse_alog(profile(title="  ", beans=3, computed="x"))
    assert enrichment == Enrichment(charge_epoch=1792245762.0)


def test_charge_offset_variants() -> None:
    assert parse_alog(profile(timeindex=[1])).charge_epoch == 1792245763.0
    assert parse_alog(profile(timeindex=[5])).charge_epoch == 1792245762.0  # out of range
    assert parse_alog(profile(timeindex=["x"])).charge_epoch == 1792245762.0
    assert parse_alog(profile(timeindex=[])).charge_epoch == 1792245762.0
    assert parse_alog(profile(timex="bad")).charge_epoch == 1792245762.0
    assert parse_alog(profile(timeindex=[0], timex=[None])).charge_epoch == 1792245762.0
    computed = {"CHARGE_BT": True, "DRY_time": "245", "FCs_time": 400}
    assert parse_alog(profile(computed=computed)).fills == {"fcs_tm": 400.0}
    assert alog_watch._number(float("nan")) is None


def test_roast_date_fallback() -> None:
    text = repr(
        {"roastisodate": "2026-10-17", "roasttime": "16:02:42", "timex": [0.0], "timeindex": [0]}
    )
    expected = datetime.fromisoformat("2026-10-17T16:02:42").astimezone().timestamp()
    assert parse_alog(text).charge_epoch == expected


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("{'a': ", "not a Python literal"),
        ("__import__('os').system('true')", "not a Python literal"),
        ("[1, 2]", "not a dict"),
        ("{'title': 'x'}", "no roast date"),
        ("{'roastisodate': '2026-13-45', 'roasttime': '25:00:00'}", "no roast date"),
    ],
)
def test_parse_errors(text: str, match: str) -> None:
    with pytest.raises(AlogError, match=match):
        parse_alog(text)


def test_read_alog_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alog_watch, "MAX_ALOG_BYTES", 10)
    with pytest.raises(AlogError, match="too large"):
        read_alog(SAMPLE)


# --- watcher -------------------------------------------------------------------------------


async def test_watcher_end_to_end(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    received: list[Enrichment] = []
    watcher = AlogWatcher(tmp_path, received.append, debounce_s=0.2)
    await watcher.start()
    try:
        (tmp_path / "notes.txt").write_text("ignored")
        (tmp_path / "subdir").mkdir()
        target = tmp_path / "roast.alog"
        shutil.copyfile(SAMPLE, target)
        with target.open("a") as handle:  # a second write within the debounce window
            handle.write("\n")
        await wait_for(lambda: len(received) == 1)
        assert received[0].title == "Ethiopia Guji"

        with caplog.at_level(logging.WARNING):
            (tmp_path / "broken.ALOG").write_text("{'oops': ")
            await wait_for(lambda: "Cannot use broken.ALOG" in caplog.text)
        assert len(received) == 1
    finally:
        await watcher.stop()


async def test_watcher_debounces_and_handles_moves(tmp_path: Path) -> None:
    received: list[Enrichment] = []
    watcher = AlogWatcher(tmp_path, received.append, debounce_s=0.1)
    await watcher.start()
    try:
        staging = tmp_path / "roast.alog.tmp"
        shutil.copyfile(SAMPLE, staging)
        staging.rename(tmp_path / "roast.alog")
        await wait_for(lambda: len(received) == 1)
        watcher.notify(tmp_path / "roast.alog")
        watcher.notify(tmp_path / "roast.alog")
        await wait_for(lambda: len(received) == 2)
    finally:
        await watcher.stop()


async def test_watcher_stop_cancels_pending_work(tmp_path: Path) -> None:
    received: list[Enrichment] = []
    watcher = AlogWatcher(tmp_path, received.append, debounce_s=10.0)
    watcher.notify(tmp_path / "pending.alog")
    await watcher.stop()  # never started: no observer, pending timer canceled
    watcher = AlogWatcher(tmp_path, received.append, debounce_s=0.0)
    watcher.notify(SAMPLE)
    await wait_for(lambda: bool(watcher._tasks) or bool(received))
    await watcher.stop()
    assert not watcher._tasks
    assert len(received) <= 1


async def test_watcher_without_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "watchdog.observers", None)
    watcher = AlogWatcher(tmp_path, lambda _e: None)
    with caplog.at_level(logging.ERROR):
        await watcher.start()
    assert "install artisan-ha-bridge[watch]" in caplog.text
    await watcher.stop()

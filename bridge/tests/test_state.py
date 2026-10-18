"""State machine: every transition row in spec 4.3 plus the M1 edge cases."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from artisan_ha_bridge.protocol import EventName, IngestEvent, parse_ingest
from artisan_ha_bridge.state import Enrichment, RoastStateMachine, roast_id_for

from helpers import FakeClock, Recorder

ROAST_ID = "2026-10-17T14:03:12Z"


@pytest.fixture
def sm(recorder: Recorder, clock: FakeClock) -> RoastStateMachine:
    return RoastStateMachine(recorder, ror_window_s=30.0, clock=clock)


def ev(
    sm: RoastStateMachine,
    event: EventName,
    tm: float | None = None,
    bt: float | None = None,
    et: float | None = None,
) -> None:
    sm.on_event(event, bt, et, tm)


def to_phase(sm: RoastStateMachine, recorder: Recorder, phase: str) -> None:
    """Drive a fresh machine to `phase` along the normal path, then clear the recorder."""
    path: list[tuple[EventName, float, float]] = [
        ("on", 0.0, 200.0),
        ("start", 0.0, 200.0),
        ("charge", 0.0, 185.2),
        ("dry", 245.0, 152.1),
        ("fcs", 464.0, 194.0),
        ("drop", 620.0, 208.4),
        ("cool", 740.0, 60.0),
    ]
    order = ["monitoring", "recording", "drying", "maillard", "development", "cooling", "finished"]
    for (event, tm, bt), reached in zip(path, order, strict=True):
        ev(sm, event, tm=tm, bt=bt, et=220.0)
        if reached == phase:
            break
    assert sm.phase == phase
    recorder.clear()


def state_messages(recorder: Recorder) -> list[dict[str, Any]]:
    return recorder.of_type("state")


# --- transition table -------------------------------------------------------------------------


def test_initial_snapshot(sm: RoastStateMachine) -> None:
    assert sm.snapshot() == {
        "phase": "off",
        "monitoring": False,
        "recording": False,
        "artisan_online": False,
        "roast_id": None,
        "tm": None,
        "bt": None,
        "et": None,
        "bt_ror": None,
        "et_ror": None,
        "dev_time": None,
        "dtr": None,
        "controls": {},
        "events": {},
        "last_event": None,
        "last_summary": None,
    }


def test_off_on_event_starts_monitoring(sm: RoastStateMachine, recorder: Recorder) -> None:
    ev(sm, "on", tm=None, bt=200.0, et=230.0)
    assert sm.phase == "monitoring"
    assert sm.monitoring
    assert not sm.recording
    assert sm.artisan_online
    assert recorder.types() == ["event", "state"]
    event, state = recorder.messages
    assert event == {
        "v": 1,
        "type": "event",
        "ts": 1792245792000,
        "event": "on",
        "tm": None,
        "bt": 200.0,
        "et": 230.0,
        "roast_id": None,
    }
    assert state == {
        "v": 1,
        "type": "state",
        "ts": 1792245792000,
        "phase": "monitoring",
        "monitoring": True,
        "recording": False,
        "artisan_online": True,
        "roast_id": None,
    }


def test_off_weblcds_frame_starts_monitoring(sm: RoastStateMachine, recorder: Recorder) -> None:
    sm.on_sample(200.0, 230.0, 0.0, now=100.0)
    assert sm.phase == "monitoring"
    assert sm.artisan_online
    assert recorder.types() == ["state", "sample"]
    assert recorder.messages[1] == {
        "v": 1,
        "type": "sample",
        "ts": 1792245792000,
        "tm": 0.0,
        "bt": 200.0,
        "et": 230.0,
        "bt_ror": None,
        "et_ror": None,
    }
    recorder.clear()
    sm.on_sample(200.5, 230.0, 0.0, now=101.0)
    assert recorder.types() == ["sample"]


def test_on_while_monitoring_is_noop(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "recording")
    ev(sm, "on")
    assert sm.phase == "recording"
    assert recorder.types() == ["event"]


def test_monitoring_start_recording(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "monitoring")
    ev(sm, "start", tm=0.0, bt=200.0, et=230.0)
    assert sm.phase == "recording"
    assert sm.recording
    assert state_messages(recorder)[0]["recording"] is True


@pytest.mark.parametrize("start_phase", ["monitoring", "recording"])
def test_charge_starts_roast(
    sm: RoastStateMachine, recorder: Recorder, clock: FakeClock, start_phase: str
) -> None:
    to_phase(sm, recorder, start_phase)
    ev(sm, "fcs", tm=10.0, bt=150.0)  # out of order, recorded, then cleared by charge
    recorder.clear()
    ev(sm, "charge", tm=0.0, bt=185.2, et=220.1)
    assert sm.phase == "drying"
    assert sm.recording
    assert sm.roast_id == ROAST_ID == roast_id_for(clock.now)
    assert sm.snapshot()["events"] == {"charge": {"tm": 0.0, "bt": 185.2}}
    assert recorder.types() == ["event", "state"]
    assert recorder.messages[0]["roast_id"] == ROAST_ID  # type: ignore[typeddict-item]
    assert state_messages(recorder)[0]["roast_id"] == ROAST_ID


def test_drying_dry_to_maillard(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "drying")
    ev(sm, "dry", tm=245.0, bt=152.1)
    assert sm.phase == "maillard"
    assert sm.snapshot()["events"]["dry"] == {"tm": 245.0, "bt": 152.1}


def test_maillard_fcs_to_development(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "maillard")
    ev(sm, "fcs", tm=464.0, bt=194.0)
    assert sm.phase == "development"
    snap = sm.snapshot()
    assert snap["dev_time"] == 0.0
    assert snap["dtr"] == 0.0


def test_skipped_dry_infers_maillard(
    sm: RoastStateMachine, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    to_phase(sm, recorder, "drying")
    with caplog.at_level(logging.INFO):
        ev(sm, "fcs", tm=464.0, bt=194.0)
    assert sm.phase == "development"
    assert "inferring maillard" in caplog.text
    assert "dry" not in sm.snapshot()["events"]


@pytest.mark.parametrize("mark", ["fce", "scs", "sce"])
def test_development_marks_record_only(
    sm: RoastStateMachine, recorder: Recorder, caplog: pytest.LogCaptureFixture, mark: EventName
) -> None:
    to_phase(sm, recorder, "development")
    with caplog.at_level(logging.DEBUG):
        ev(sm, mark, tm=500.0, bt=200.0)
    assert sm.phase == "development"
    assert sm.snapshot()["events"][mark] == {"tm": 500.0, "bt": 200.0}
    assert recorder.types() == ["event"]
    assert caplog.text == ""


@pytest.mark.parametrize("start_phase", ["drying", "maillard", "development"])
def test_drop_emits_preliminary_summary(
    sm: RoastStateMachine, recorder: Recorder, start_phase: str
) -> None:
    to_phase(sm, recorder, start_phase)
    ev(sm, "drop", tm=620.0, bt=208.4)
    assert sm.phase == "cooling"
    assert recorder.types() == ["event", "state", "summary"]
    summary = recorder.of_type("summary")[0]
    assert summary["final"] is False
    assert summary["drop_tm"] == 620.0
    assert summary["drop_bt"] == 208.4
    assert summary["cool_tm"] is None
    if start_phase == "development":
        assert summary["dev_time"] == 156.0
        assert summary["dtr"] == 25.2
    else:
        assert summary["dev_time"] is None
        assert summary["dtr"] is None


def test_full_summary_matches_spec_example(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "cooling")
    snap = sm.snapshot()
    assert snap["last_summary"] == {
        "v": 1,
        "type": "summary",
        "ts": 1792245792000,
        "roast_id": ROAST_ID,
        "final": False,
        "charge_bt": 185.2,
        "dry_tm": 245.0,
        "dry_bt": 152.1,
        "fcs_tm": 464.0,
        "fcs_bt": 194.0,
        "drop_tm": 620.0,
        "drop_bt": 208.4,
        "dev_time": 156.0,
        "dtr": 25.2,
        "cool_tm": None,
        "title": None,
        "beans": None,
        "weight_in_g": None,
        "weight_out_g": None,
    }
    # Dev time is frozen at drop.
    assert snap["dev_time"] == 156.0
    assert snap["dtr"] == 25.2
    sm.on_sample(150.0, 180.0, 700.0, now=0.0)
    assert sm.snapshot()["dev_time"] == 156.0


def test_cooling_cool_emits_final_summary(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "cooling")
    ev(sm, "cool", tm=740.0, bt=60.0)
    assert sm.phase == "finished"
    assert recorder.types() == ["event", "state", "summary"]
    summary = recorder.of_type("summary")[0]
    assert summary["final"] is True
    assert summary["cool_tm"] == 740.0
    assert summary["dev_time"] == 156.0


def test_cooling_off_emits_final_summary(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "cooling")
    ev(sm, "off", tm=700.0, bt=60.0)
    assert sm.phase == "off"
    assert not sm.monitoring
    assert not sm.recording
    assert recorder.types() == ["event", "state", "summary"]
    summary = recorder.of_type("summary")[0]
    assert summary["final"] is True
    assert summary["cool_tm"] is None
    assert sm.snapshot()["last_summary"] == summary


def test_finished_off_does_not_resend_summary(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "finished")
    ev(sm, "off")
    assert sm.phase == "off"
    assert recorder.types() == ["event", "state"]
    # The roast record stays visible until the next charge or reset.
    assert sm.roast_id == ROAST_ID


@pytest.mark.parametrize(
    "start_phase", ["monitoring", "recording", "drying", "maillard", "development"]
)
def test_any_off_stops(sm: RoastStateMachine, recorder: Recorder, start_phase: str) -> None:
    to_phase(sm, recorder, start_phase)
    ev(sm, "off")
    assert sm.phase == "off"
    state = state_messages(recorder)[0]
    assert state["monitoring"] is False
    assert state["recording"] is False
    assert recorder.of_type("summary") == []
    assert sm.snapshot()["bt_ror"] is None


def test_off_while_off_emits_only_event(sm: RoastStateMachine, recorder: Recorder) -> None:
    ev(sm, "off")
    assert recorder.types() == ["event"]


@pytest.mark.parametrize("start_phase", ["development", "cooling", "finished"])
def test_reset_mid_roast_online(
    sm: RoastStateMachine, recorder: Recorder, start_phase: str
) -> None:
    to_phase(sm, recorder, start_phase)
    had_summary = sm.snapshot()["last_summary"]
    ev(sm, "reset")
    snap = sm.snapshot()
    assert sm.phase == "monitoring"
    assert snap["roast_id"] is None
    assert snap["events"] == {}
    assert snap["dev_time"] is None
    assert snap["dtr"] is None
    assert snap["last_summary"] == had_summary
    assert snap["last_event"] == {"event": "reset", "ts": 1792245792000}
    assert recorder.types() == ["event", "state"]
    assert recorder.messages[0]["roast_id"] is None  # type: ignore[typeddict-item]


def test_reset_while_offline_goes_off(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "maillard")
    sm.set_artisan_online(False)
    ev(sm, "reset")
    assert sm.phase == "off"


def test_reset_in_cooling_drops_pending_summary(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "cooling")
    ev(sm, "reset")
    ev(sm, "off")
    assert recorder.of_type("summary") == []


@pytest.mark.parametrize(
    "start_phase", ["monitoring", "drying", "development", "cooling", "finished"]
)
def test_artisan_offline_keeps_phase(
    sm: RoastStateMachine, recorder: Recorder, start_phase: str
) -> None:
    to_phase(sm, recorder, start_phase)
    sm.set_artisan_online(False)
    assert sm.phase == start_phase
    assert recorder.types() == ["state"]
    assert state_messages(recorder)[0]["artisan_online"] is False
    recorder.clear()
    sm.set_artisan_online(False)
    assert recorder.messages == []
    sm.on_sample(200.0, 220.0, 300.0, now=0.0)
    assert sm.artisan_online
    assert recorder.types() == ["state", "sample"]


# --- edge cases -------------------------------------------------------------------------------


def test_dropped_on_then_frames(sm: RoastStateMachine, recorder: Recorder) -> None:
    sm.on_sample(200.0, 230.0, 0.0, now=0.0)
    ev(sm, "start", tm=0.0)
    ev(sm, "charge", tm=0.0, bt=185.2)
    assert sm.phase == "drying"


@pytest.mark.parametrize(("event", "phase"), [("start", "recording"), ("charge", "drying")])
def test_dropped_on_without_frames_is_inferred(
    sm: RoastStateMachine, recorder: Recorder, event: EventName, phase: str
) -> None:
    ev(sm, event, tm=0.0, bt=185.2)
    assert sm.phase == phase
    assert sm.artisan_online
    assert state_messages(recorder)[-1]["phase"] == phase


def test_duplicate_events_overwrite(
    sm: RoastStateMachine, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    to_phase(sm, recorder, "development")
    with caplog.at_level(logging.WARNING):
        ev(sm, "dry", tm=250.0, bt=153.0)
        ev(sm, "fcs", tm=470.0, bt=195.0)
        ev(sm, "charge", tm=1.0, bt=186.0)
    assert caplog.text == ""
    events = sm.snapshot()["events"]
    assert events["dry"] == {"tm": 250.0, "bt": 153.0}
    assert events["fcs"] == {"tm": 470.0, "bt": 195.0}
    assert events["charge"] == {"tm": 1.0, "bt": 186.0}
    assert sm.phase == "development"
    assert sm.roast_id == ROAST_ID


def test_duplicate_start_while_recording(
    sm: RoastStateMachine, recorder: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    to_phase(sm, recorder, "recording")
    with caplog.at_level(logging.WARNING):
        ev(sm, "start")
    assert caplog.text == ""
    assert sm.phase == "recording"


def test_duplicate_drop_refines_frozen_dev(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "cooling")
    ev(sm, "drop", tm=630.0, bt=209.0)
    snap = sm.snapshot()
    assert snap["dev_time"] == 166.0
    assert snap["dtr"] == 26.3
    assert recorder.types() == ["event"]
    ev(sm, "cool", tm=740.0)
    assert recorder.of_type("summary")[0]["drop_tm"] == 630.0


@pytest.mark.parametrize(
    ("start_phase", "event"),
    [
        ("monitoring", "fcs"),
        ("monitoring", "drop"),
        ("off", "dry"),
        ("drying", "fce"),
        ("drying", "cool"),
        ("cooling", "start"),
        ("finished", "charge"),
    ],
)
def test_out_of_order_events_record_and_warn(
    sm: RoastStateMachine,
    recorder: Recorder,
    caplog: pytest.LogCaptureFixture,
    start_phase: str,
    event: EventName,
) -> None:
    if start_phase != "off":
        to_phase(sm, recorder, start_phase)
    before = sm.snapshot()["events"]
    with caplog.at_level(logging.WARNING):
        ev(sm, event, tm=123.0, bt=150.0)
    assert sm.phase == start_phase
    assert "Out-of-order" in caplog.text or event in before
    if event != "start":
        assert sm.snapshot()["events"][event] == {"tm": 123.0, "bt": 150.0}
    assert recorder.of_type("summary") == []
    assert "state" not in recorder.types()


def test_minus_one_substitutions_use_latest_frame(
    sm: RoastStateMachine, recorder: Recorder
) -> None:
    sm.on_sample(186.0, 221.0, 30.0, now=0.0)
    ev(sm, "start")
    parsed = parse_ingest({"k": "ev", "e": "charge", "bt": -1, "et": -1.0, "tm": -1, "id": 1})
    assert isinstance(parsed, IngestEvent)
    assert (parsed.bt, parsed.et, parsed.tm) == (None, None, None)
    recorder.clear()
    sm.on_event(parsed.event, parsed.bt, parsed.et, parsed.tm)
    event = recorder.of_type("event")[0]
    assert (event["bt"], event["et"], event["tm"]) == (186.0, 221.0, 30.0)
    assert sm.snapshot()["events"]["charge"] == {"tm": 30.0, "bt": 186.0}


def test_minus_one_without_any_frame_stays_null(sm: RoastStateMachine, recorder: Recorder) -> None:
    ev(sm, "on")
    ev(sm, "charge")
    assert sm.snapshot()["events"]["charge"] == {"tm": None, "bt": None}


def test_live_dev_time_and_dtr(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "development")
    sm.on_sample(196.3, 225.0, 512.0, now=0.0)
    snap = sm.snapshot()
    assert snap["dev_time"] == 48.0
    assert snap["dtr"] == 9.4
    # A frame without a timer value keeps the previous figures.
    sm.on_sample(196.5, 225.0, None, now=1.0)
    assert sm.snapshot()["dev_time"] == 48.0
    # A slightly lagging frame right after fcs never produces negative dev time.
    sm.on_sample(194.0, 225.0, 463.0, now=2.0)
    assert sm.snapshot()["dev_time"] == 0.0


def test_fcs_without_time_keeps_dev_unknown(sm: RoastStateMachine, recorder: Recorder) -> None:
    ev(sm, "on")
    ev(sm, "charge")
    ev(sm, "fcs")
    sm.on_sample(196.3, 225.0, 512.0, now=0.0)
    assert sm.snapshot()["dev_time"] == 0.0
    ev(sm, "drop", tm=600.0)
    assert sm.snapshot()["dev_time"] is None


def test_drop_at_zero_time_has_no_dtr(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "development")
    ev(sm, "fcs", tm=0.0)
    ev(sm, "drop", tm=0.0)
    summary = recorder.of_type("summary")[0]
    assert summary["dev_time"] == 0.0
    assert summary["dtr"] is None


def test_ror_before_and_after_charge(sm: RoastStateMachine, recorder: Recorder) -> None:
    # Before charge the x-axis is monotonic time: 1 degree per second = 60 degrees/min.
    for i in range(16):
        sm.on_sample(100.0 + i, 200.0, 0.0, now=1000.0 + i)
    snap = sm.snapshot()
    assert snap["bt_ror"] == 60.0
    assert snap["et_ror"] == 0.0
    ev(sm, "charge", tm=0.0, bt=185.0)
    assert sm.snapshot()["bt_ror"] is None
    # After charge the x-axis is roast time; monotonic `now` is ignored.
    for i in range(16):
        sm.on_sample(150.0 + 0.5 * i, 210.0 - 0.25 * i, float(i), now=5.0)
    snap = sm.snapshot()
    assert snap["bt_ror"] == 30.0
    assert snap["et_ror"] == -15.0
    # Frames without a timer value are not added after charge.
    sm.on_sample(500.0, 500.0, None, now=6.0)
    assert sm.snapshot()["bt_ror"] == 30.0
    samples = recorder.of_type("sample")
    assert samples[-1]["bt_ror"] == 30.0


def test_controls(sm: RoastStateMachine, recorder: Recorder) -> None:
    sm.on_control("heat", 70.0)
    sm.on_control("fan", None)
    sm.on_control("heat", 75.0)
    assert sm.snapshot()["controls"] == {"heat": 75.0, "fan": None}
    assert recorder.messages[0] == {
        "v": 1,
        "type": "control",
        "ts": 1792245792000,
        "channel": "heat",
        "value": 70.0,
    }


def test_alert(sm: RoastStateMachine, recorder: Recorder) -> None:
    sm.on_alert("Drop soon", "Alarm")
    sm.on_alert("No title", None)
    assert recorder.messages == [
        {"v": 1, "type": "alert", "ts": 1792245792000, "title": "Alarm", "text": "Drop soon"},
        {"v": 1, "type": "alert", "ts": 1792245792000, "title": None, "text": "No title"},
    ]


def test_snapshot_is_a_copy(sm: RoastStateMachine, recorder: Recorder) -> None:
    to_phase(sm, recorder, "cooling")
    snap = sm.snapshot()
    snap["events"]["charge"]["bt"] = 0.0
    snap["controls"]["x"] = 1.0
    assert snap["last_summary"] is not None
    snap["last_summary"]["title"] = "mutated"
    assert snap["last_event"] is not None
    snap["last_event"]["event"] = "mutated"
    fresh = sm.snapshot()
    assert fresh["events"]["charge"]["bt"] == 185.2
    assert fresh["controls"] == {}
    assert fresh["last_summary"] is not None
    assert fresh["last_summary"]["title"] is None
    assert fresh["last_event"] == {"event": "drop", "ts": 1792245792000}


def test_new_roast_after_off(sm: RoastStateMachine, recorder: Recorder, clock: FakeClock) -> None:
    to_phase(sm, recorder, "finished")
    ev(sm, "off")
    clock.advance(3600)
    ev(sm, "on")
    ev(sm, "charge", tm=0.0, bt=190.0)
    assert sm.roast_id == "2026-10-17T15:03:12Z"
    assert sm.snapshot()["events"] == {"charge": {"tm": 0.0, "bt": 190.0}}
    assert sm.snapshot()["last_summary"] is not None


# --- .alog enrichment -------------------------------------------------------------------------


def test_enrich_without_summary(sm: RoastStateMachine, clock: FakeClock) -> None:
    assert not sm.enrich_summary(Enrichment(charge_epoch=clock.now, title="x"))


def test_enrich_mismatched_roast(
    sm: RoastStateMachine, recorder: Recorder, clock: FakeClock
) -> None:
    to_phase(sm, recorder, "cooling")
    assert not sm.enrich_summary(Enrichment(charge_epoch=clock.now + 61, title="x"))
    assert recorder.messages == []


def test_enrich_current_roast_carries_into_final(
    sm: RoastStateMachine, recorder: Recorder, clock: FakeClock
) -> None:
    to_phase(sm, recorder, "cooling")
    clock.advance(30)
    enrichment = Enrichment(
        charge_epoch=clock.now - 50,
        title="Ethiopia Guji",
        beans="Guji natural",
        weight_in_g=250.0,
        weight_out_g=212.5,
        fills={"charge_bt": 1.0, "cool_tm": 745.0},
    )
    assert sm.enrich_summary(enrichment)
    resent = recorder.of_type("summary")[0]
    assert resent["final"] is False
    assert resent["ts"] == int(clock.now * 1000)
    assert resent["title"] == "Ethiopia Guji"
    assert resent["beans"] == "Guji natural"
    assert resent["weight_in_g"] == 250.0
    assert resent["weight_out_g"] == 212.5
    assert resent["charge_bt"] == 185.2  # bridge value wins
    assert resent["cool_tm"] == 745.0  # filled from computed
    assert sm.snapshot()["last_summary"] == resent
    recorder.clear()
    ev(sm, "cool", tm=740.0)
    final = recorder.of_type("summary")[0]
    assert final["final"] is True
    assert final["title"] == "Ethiopia Guji"
    assert final["cool_tm"] == 740.0


def test_enrich_after_reset_fills_dev_and_dtr(
    sm: RoastStateMachine, recorder: Recorder, clock: FakeClock
) -> None:
    ev(sm, "on")
    ev(sm, "charge", tm=0.0, bt=185.0)
    ev(sm, "drop", tm=600.0, bt=205.0)  # no fcs recorded: dev/dtr unknown
    ev(sm, "off")
    ev(sm, "reset")
    recorder.clear()
    enrichment = Enrichment(charge_epoch=clock.now, fills={"dev_time": 90.0, "fcs_tm": 510.0})
    assert sm.enrich_summary(enrichment)
    resent = recorder.of_type("summary")[0]
    assert resent["final"] is True
    assert resent["dev_time"] == 90.0
    assert resent["dtr"] == 15.0
    assert resent["title"] is None
    # Not the current roast any more, so nothing is carried forward.
    ev(sm, "charge", tm=0.0)
    ev(sm, "drop", tm=600.0)
    assert recorder.of_type("summary")[-1]["dev_time"] is None

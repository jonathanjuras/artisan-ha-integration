"""Ingest schema validation and outbound message builders."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from artisan_ha_bridge import protocol
from artisan_ha_bridge.protocol import IngestControl, IngestError, IngestEvent, parse_ingest


def test_spec_examples_parse() -> None:
    assert parse_ingest(
        {"k": "ev", "e": "charge", "bt": 185.2, "et": 220.1, "tm": 0.0, "id": 48211}
    ) == IngestEvent(event="charge", bt=185.2, et=220.1, tm=0.0)
    assert parse_ingest({"k": "ctl", "ch": "heat", "v": 70.0, "id": 1093}) == IngestControl(
        channel="heat", value=70.0
    )


@pytest.mark.parametrize("event", sorted(protocol.EVENT_NAMES))
def test_all_events_accepted(event: str) -> None:
    parsed = parse_ingest({"k": "ev", "e": event})
    assert isinstance(parsed, IngestEvent)
    assert parsed.event == event
    assert (parsed.bt, parsed.et, parsed.tm) == (None, None, None)


def test_event_names_match_spec() -> None:
    assert {
        "on", "off", "start", "reset", "charge", "dry",
        "fcs", "fce", "scs", "sce", "drop", "cool",
    } == protocol.EVENT_NAMES  # fmt: skip
    assert protocol.PHASES == (
        "off", "monitoring", "recording", "drying",
        "maillard", "development", "cooling", "finished",
    )  # fmt: skip


@pytest.mark.parametrize("value", [-1, -1.0, None, math.nan, math.inf])
def test_not_sampling_values_become_null(value: object) -> None:
    parsed = parse_ingest({"k": "ev", "e": "dry", "bt": value, "et": value, "tm": value})
    assert parsed == IngestEvent(event="dry", bt=None, et=None, tm=None)


def test_integers_are_floats() -> None:
    parsed = parse_ingest({"k": "ev", "e": "dry", "bt": 150, "et": 0, "tm": 245})
    assert parsed == IngestEvent(event="dry", bt=150.0, et=0.0, tm=245.0)
    assert isinstance(parsed, IngestEvent)
    assert isinstance(parsed.bt, float)


def test_control_minus_one_is_a_real_value() -> None:
    assert parse_ingest({"k": "ctl", "ch": "fan", "v": -1}) == IngestControl("fan", -1.0)
    assert parse_ingest({"k": "ctl", "ch": "fan"}) == IngestControl("fan", None)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ([1, 2], "JSON object"),
        ("ev", "JSON object"),
        ({}, "unknown kind"),
        ({"k": "xx"}, "unknown kind"),
        ({"k": "ev"}, "unknown event"),
        ({"k": "ev", "e": "explode"}, "unknown event"),
        ({"k": "ev", "e": 3}, "unknown event"),
        ({"k": "ev", "e": "dry", "bt": "150"}, "'bt' must be a number"),
        ({"k": "ev", "e": "dry", "tm": True}, "'tm' must be a number"),
        ({"k": "ctl", "ch": "Heat", "v": 1}, "invalid control channel"),
        ({"k": "ctl", "ch": "a_b", "v": 1}, "invalid control channel"),
        ({"k": "ctl", "ch": "x" * 17, "v": 1}, "invalid control channel"),
        ({"k": "ctl", "ch": "", "v": 1}, "invalid control channel"),
        ({"k": "ctl", "v": 1}, "invalid control channel"),
        ({"k": "ctl", "ch": "fan", "v": [1]}, "'v' must be a number"),
    ],
)
def test_schema_errors(raw: Any, match: str) -> None:
    with pytest.raises(IngestError, match=match):
        parse_ingest(raw)


def test_every_builder_has_envelope() -> None:
    ts = 1792252483254
    messages: list[protocol.OutboundMessage] = [
        protocol.build_hello(ts, bridge_version="0.1.0", bridge_id="id", hostname="h", unit="C"),
        protocol.build_snapshot(ts, _empty_state()),
        protocol.build_sample(ts, tm=1.0, bt=2.0, et=3.0, bt_ror=None, et_ror=None),
        protocol.build_event(ts, event="dry", tm=1.0, bt=2.0, et=3.0, roast_id=None),
        protocol.build_control(ts, channel="heat", value=1.0),
        protocol.build_state(
            ts, phase="off", monitoring=False, recording=False, artisan_online=False, roast_id=None
        ),
        protocol.build_alert(ts, text="t", title=None),
        protocol.build_summary(ts, roast_id=None, final=False),
        protocol.build_ping(ts),
    ]
    for message in messages:
        assert message["v"] == 1
        assert isinstance(message["ts"], int)
        json.dumps(message)  # serializable
    assert [m["type"] for m in messages] == [
        "hello", "snapshot", "sample", "event", "control", "state", "alert", "summary", "ping",
    ]  # fmt: skip
    hello = messages[0]
    assert hello == {
        "v": 1,
        "type": "hello",
        "ts": ts,
        "bridge_version": "0.1.0",
        "bridge_id": "id",
        "hostname": "h",
        "unit": "C",
        "protocol": 1,
    }


def test_summary_field_names() -> None:
    summary = protocol.build_summary(1, roast_id="r", final=True)
    assert list(summary) == [
        "v", "type", "ts", "roast_id", "final", "charge_bt", "dry_tm", "dry_bt", "fcs_tm",
        "fcs_bt", "drop_tm", "drop_bt", "dev_time", "dtr", "cool_tm", "title", "beans",
        "weight_in_g", "weight_out_g",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("dev", "total", "expected"),
    [(156.0, 620.0, 25.2), (None, 620.0, None), (10.0, None, None), (10.0, 0.0, None)],
)
def test_dtr_percent(dev: float | None, total: float | None, expected: float | None) -> None:
    assert protocol.dtr_percent(dev, total) == expected


def _empty_state() -> protocol.StateSnapshot:
    return {
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

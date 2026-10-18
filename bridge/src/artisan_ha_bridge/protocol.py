"""Wire protocol: ingest validation (spec 3.1) and bridge-to-HA messages (spec 3.2-3.4).

Field names here are shared byte-for-byte with the Home Assistant integration.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final, Literal, TypedDict, get_args

PROTOCOL_VERSION: Final = 1

EventName = Literal[
    "on", "off", "start", "reset", "charge", "dry", "fcs", "fce", "scs", "sce", "drop", "cool"
]
EVENT_NAMES: Final[frozenset[str]] = frozenset(get_args(EventName))

Phase = Literal[
    "off", "monitoring", "recording", "drying", "maillard", "development", "cooling", "finished"
]
PHASES: Final[tuple[Phase, ...]] = get_args(Phase)

_CHANNEL_RE: Final = re.compile(r"[a-z0-9]{1,16}")
# Artisan substitutes -1 for {BT}/{ET}/{t} when it is not sampling.
_NOT_SAMPLING: Final = -1.0


# --- Ingest (Artisan -> bridge) --------------------------------------------------------------


class IngestError(ValueError):
    """An ingest message does not match the schema in spec 3.1."""


@dataclass(frozen=True, slots=True)
class IngestEvent:
    event: EventName
    bt: float | None
    et: float | None
    tm: float | None


@dataclass(frozen=True, slots=True)
class IngestControl:
    channel: str
    value: float | None


IngestMessage = IngestEvent | IngestControl


def _number(raw: dict[str, object], key: str, *, sentinel: bool) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    # bool is an int subclass; True/False are never valid readings.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IngestError(f"{key!r} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or (sentinel and number == _NOT_SAMPLING):
        return None
    return number


def parse_ingest(raw: object) -> IngestMessage:
    """Validate one decoded ingest JSON object. Unknown keys (Artisan's `id`) are ignored."""
    if not isinstance(raw, dict):
        raise IngestError("message must be a JSON object")
    kind = raw.get("k")
    if kind == "ev":
        event = raw.get("e")
        if not isinstance(event, str) or event not in EVENT_NAMES:
            raise IngestError(f"unknown event {event!r}")
        return IngestEvent(
            event=event,  # type: ignore[arg-type]  # narrowed by the EVENT_NAMES check
            bt=_number(raw, "bt", sentinel=True),
            et=_number(raw, "et", sentinel=True),
            tm=_number(raw, "tm", sentinel=True),
        )
    if kind == "ctl":
        channel = raw.get("ch")
        if not isinstance(channel, str) or not _CHANNEL_RE.fullmatch(channel):
            raise IngestError(f"invalid control channel {channel!r}")
        return IngestControl(channel=channel, value=_number(raw, "v", sentinel=False))
    raise IngestError(f"unknown kind {kind!r}")


# --- Bridge -> HA messages ---------------------------------------------------------------------


class EventRecord(TypedDict):
    tm: float | None
    bt: float | None


class LastEvent(TypedDict):
    event: str
    ts: int


class HelloMessage(TypedDict):
    v: int
    type: Literal["hello"]
    ts: int
    bridge_version: str
    bridge_id: str
    hostname: str
    unit: str
    protocol: int


class SummaryMessage(TypedDict):
    v: int
    type: Literal["summary"]
    ts: int
    roast_id: str | None
    final: bool
    charge_bt: float | None
    dry_tm: float | None
    dry_bt: float | None
    fcs_tm: float | None
    fcs_bt: float | None
    drop_tm: float | None
    drop_bt: float | None
    dev_time: float | None
    dtr: float | None
    cool_tm: float | None
    title: str | None
    beans: str | None
    weight_in_g: float | None
    weight_out_g: float | None


# Summary fields that `.alog` computed values may fill when the bridge saw no value.
SummaryNumberField = Literal[
    "charge_bt", "dry_tm", "dry_bt", "fcs_tm", "fcs_bt", "drop_tm", "drop_bt", "dev_time", "cool_tm"
]


class StateSnapshot(TypedDict):
    phase: Phase
    monitoring: bool
    recording: bool
    artisan_online: bool
    roast_id: str | None
    tm: float | None
    bt: float | None
    et: float | None
    bt_ror: float | None
    et_ror: float | None
    dev_time: float | None
    dtr: float | None
    controls: dict[str, float | None]
    events: dict[str, EventRecord]
    last_event: LastEvent | None
    last_summary: SummaryMessage | None


class SnapshotMessage(TypedDict):
    v: int
    type: Literal["snapshot"]
    ts: int
    state: StateSnapshot


class SampleMessage(TypedDict):
    v: int
    type: Literal["sample"]
    ts: int
    tm: float | None
    bt: float | None
    et: float | None
    bt_ror: float | None
    et_ror: float | None


class EventMessage(TypedDict):
    v: int
    type: Literal["event"]
    ts: int
    event: str
    tm: float | None
    bt: float | None
    et: float | None
    roast_id: str | None


class ControlMessage(TypedDict):
    v: int
    type: Literal["control"]
    ts: int
    channel: str
    value: float | None


class StateMessage(TypedDict):
    v: int
    type: Literal["state"]
    ts: int
    phase: Phase
    monitoring: bool
    recording: bool
    artisan_online: bool
    roast_id: str | None


class AlertMessage(TypedDict):
    v: int
    type: Literal["alert"]
    ts: int
    title: str | None
    text: str


class PingMessage(TypedDict):
    v: int
    type: Literal["ping"]
    ts: int


OutboundMessage = (
    HelloMessage
    | SnapshotMessage
    | SampleMessage
    | EventMessage
    | ControlMessage
    | StateMessage
    | AlertMessage
    | SummaryMessage
    | PingMessage
)


def build_hello(
    ts: int, *, bridge_version: str, bridge_id: str, hostname: str, unit: str
) -> HelloMessage:
    return {
        "v": PROTOCOL_VERSION,
        "type": "hello",
        "ts": ts,
        "bridge_version": bridge_version,
        "bridge_id": bridge_id,
        "hostname": hostname,
        "unit": unit,
        "protocol": PROTOCOL_VERSION,
    }


def build_snapshot(ts: int, state: StateSnapshot) -> SnapshotMessage:
    return {"v": PROTOCOL_VERSION, "type": "snapshot", "ts": ts, "state": state}


def build_sample(
    ts: int,
    *,
    tm: float | None,
    bt: float | None,
    et: float | None,
    bt_ror: float | None,
    et_ror: float | None,
) -> SampleMessage:
    return {
        "v": PROTOCOL_VERSION,
        "type": "sample",
        "ts": ts,
        "tm": tm,
        "bt": bt,
        "et": et,
        "bt_ror": bt_ror,
        "et_ror": et_ror,
    }


def build_event(
    ts: int,
    *,
    event: str,
    tm: float | None,
    bt: float | None,
    et: float | None,
    roast_id: str | None,
) -> EventMessage:
    return {
        "v": PROTOCOL_VERSION,
        "type": "event",
        "ts": ts,
        "event": event,
        "tm": tm,
        "bt": bt,
        "et": et,
        "roast_id": roast_id,
    }


def build_control(ts: int, *, channel: str, value: float | None) -> ControlMessage:
    return {
        "v": PROTOCOL_VERSION,
        "type": "control",
        "ts": ts,
        "channel": channel,
        "value": value,
    }


def build_state(
    ts: int,
    *,
    phase: Phase,
    monitoring: bool,
    recording: bool,
    artisan_online: bool,
    roast_id: str | None,
) -> StateMessage:
    return {
        "v": PROTOCOL_VERSION,
        "type": "state",
        "ts": ts,
        "phase": phase,
        "monitoring": monitoring,
        "recording": recording,
        "artisan_online": artisan_online,
        "roast_id": roast_id,
    }


def build_alert(ts: int, *, text: str, title: str | None) -> AlertMessage:
    return {"v": PROTOCOL_VERSION, "type": "alert", "ts": ts, "title": title, "text": text}


def build_ping(ts: int) -> PingMessage:
    return {"v": PROTOCOL_VERSION, "type": "ping", "ts": ts}


def build_summary(
    ts: int,
    *,
    roast_id: str | None,
    final: bool,
    charge_bt: float | None = None,
    dry_tm: float | None = None,
    dry_bt: float | None = None,
    fcs_tm: float | None = None,
    fcs_bt: float | None = None,
    drop_tm: float | None = None,
    drop_bt: float | None = None,
    dev_time: float | None = None,
    dtr: float | None = None,
    cool_tm: float | None = None,
    title: str | None = None,
    beans: str | None = None,
    weight_in_g: float | None = None,
    weight_out_g: float | None = None,
) -> SummaryMessage:
    return {
        "v": PROTOCOL_VERSION,
        "type": "summary",
        "ts": ts,
        "roast_id": roast_id,
        "final": final,
        "charge_bt": charge_bt,
        "dry_tm": dry_tm,
        "dry_bt": dry_bt,
        "fcs_tm": fcs_tm,
        "fcs_bt": fcs_bt,
        "drop_tm": drop_tm,
        "drop_bt": drop_bt,
        "dev_time": dev_time,
        "dtr": dtr,
        "cool_tm": cool_tm,
        "title": title,
        "beans": beans,
        "weight_in_g": weight_in_g,
        "weight_out_g": weight_out_g,
    }


def dtr_percent(dev_time: float | None, total_tm: float | None) -> float | None:
    """Development time ratio in percent, one decimal."""
    if dev_time is None or total_tm is None or total_tm <= 0:
        return None
    return round(dev_time / total_tm * 100.0, 1)

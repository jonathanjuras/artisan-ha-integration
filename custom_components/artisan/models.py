"""Roast state model and the pure reducer that applies bridge messages to it."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .const import PHASES

type JsonDict = dict[str, Any]

# Envelope keys that carry no roast data (spec 3.2).
_ENVELOPE_KEYS = frozenset({"v", "type"})


@dataclass(frozen=True, slots=True)
class RoastState:
    """Everything Home Assistant knows about the bridge and the current roast.

    Immutable: `apply` returns a new instance so a reader never sees a half-applied
    message.
    """

    # From `hello`.
    bridge_id: str | None = None
    bridge_version: str | None = None
    hostname: str | None = None
    unit: str = "C"
    protocol: int | None = None

    # From `snapshot` / `state`.
    phase: str | None = None
    monitoring: bool | None = None
    recording: bool | None = None
    artisan_online: bool | None = None
    roast_id: str | None = None

    # From `snapshot` / `sample`.
    tm: float | None = None
    bt: float | None = None
    et: float | None = None
    bt_ror: float | None = None
    et_ror: float | None = None
    dev_time: float | None = None
    dtr: float | None = None

    controls: Mapping[str, float | None] = field(default_factory=dict)
    events: Mapping[str, JsonDict] = field(default_factory=dict)
    last_event: JsonDict | None = None
    last_summary: JsonDict | None = None
    # Kept separately because the bridge's `last_summary` is replaced by the
    # non-final DROP summary of the next roast, while `last_roast` must keep
    # pointing at the last completed roast.
    last_final_summary: JsonDict | None = None

    @property
    def charged(self) -> bool:
        """Return True once the current roast has been charged."""
        return "charge" in self.events

    @property
    def roast_time(self) -> float | None:
        """Return seconds since CHARGE, or None before charge."""
        return self.tm if self.charged else None


def _num(value: Any) -> float | None:
    """Coerce a wire number to float; anything else (null, bool, str) is None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _phase(value: Any) -> str | None:
    return value if value in PHASES else None


def _numeric_fields(msg: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Return coerced numbers for the keys present in the message.

    Missing keys keep the previous value; an explicit null clears it.
    """
    return {key: _num(msg[key]) for key in keys if key in msg}


def _controls(value: Any) -> dict[str, float | None]:
    if not isinstance(value, Mapping):
        return {}
    return {str(ch): _num(v) for ch, v in value.items()}


def _events(value: Any) -> dict[str, JsonDict]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): dict(v) for k, v in value.items() if isinstance(v, Mapping)}


def _summary(msg: Mapping[str, Any]) -> JsonDict:
    return {k: v for k, v in msg.items() if k not in _ENVELOPE_KEYS}


def _live_development(state: RoastState) -> RoastState:
    """Derive dev_time and DTR from the current `tm` during development.

    `sample` frames do not carry dev_time/dtr, so the client mirrors the bridge's
    formula (spec 4.3) between snapshots. After DROP the values stay frozen.
    """
    fcs_tm = _num(state.events.get("fcs", {}).get("tm"))
    if state.phase != "development" or fcs_tm is None or state.tm is None:
        return state
    dev_time = round(state.tm - fcs_tm, 1)
    dtr = round(dev_time / state.tm * 100, 1) if state.tm > 0 else None
    return replace(state, dev_time=dev_time, dtr=dtr)


def _apply_hello(state: RoastState, msg: Mapping[str, Any]) -> RoastState:
    unit = msg.get("unit")
    return replace(
        state,
        bridge_id=_opt_str(msg.get("bridge_id")) or state.bridge_id,
        bridge_version=_opt_str(msg.get("bridge_version")),
        hostname=_opt_str(msg.get("hostname")) or state.hostname,
        unit=unit if unit in ("C", "F") else state.unit,
        protocol=msg.get("protocol") if isinstance(msg.get("protocol"), int) else None,
    )


def _apply_snapshot(state: RoastState, msg: Mapping[str, Any]) -> RoastState:
    snap = msg.get("state")
    if not isinstance(snap, Mapping):
        return state
    last_summary = snap.get("last_summary")
    last_summary = dict(last_summary) if isinstance(last_summary, Mapping) else None
    last_final = state.last_final_summary
    if last_summary is not None and last_summary.get("final") is True:
        last_final = last_summary
    last_event = snap.get("last_event")
    # A snapshot is the bridge's complete view; replace every roast field.
    return replace(
        state,
        phase=_phase(snap.get("phase")),
        monitoring=_opt_bool(snap.get("monitoring")),
        recording=_opt_bool(snap.get("recording")),
        artisan_online=_opt_bool(snap.get("artisan_online")),
        roast_id=_opt_str(snap.get("roast_id")),
        tm=_num(snap.get("tm")),
        bt=_num(snap.get("bt")),
        et=_num(snap.get("et")),
        bt_ror=_num(snap.get("bt_ror")),
        et_ror=_num(snap.get("et_ror")),
        dev_time=_num(snap.get("dev_time")),
        dtr=_num(snap.get("dtr")),
        controls=_controls(snap.get("controls")),
        events=_events(snap.get("events")),
        last_event=dict(last_event) if isinstance(last_event, Mapping) else None,
        last_summary=last_summary,
        last_final_summary=last_final,
    )


def _apply_sample(state: RoastState, msg: Mapping[str, Any]) -> RoastState:
    new = replace(state, **_numeric_fields(msg, ("tm", "bt", "et", "bt_ror", "et_ror")))
    return _live_development(new)


def _apply_event(state: RoastState, msg: Mapping[str, Any]) -> RoastState:
    event = _opt_str(msg.get("event"))
    if event is None:
        return state
    record = {key: _num(msg.get(key)) for key in ("tm", "bt", "et")}
    if event == "charge":
        # A new roast starts: forget the previous roast's events and derived data.
        events: dict[str, JsonDict] = {"charge": record}
        derived: dict[str, Any] = {"dev_time": None, "dtr": None}
    elif event == "reset":
        events, derived = {}, {"dev_time": None, "dtr": None}
    else:
        events, derived = {**state.events, event: record}, {}
    roast_id = msg.get("roast_id", state.roast_id)
    return replace(
        state,
        events=events,
        roast_id=_opt_str(roast_id),
        last_event={"event": event, "ts": msg.get("ts")},
        **derived,
    )


def _apply_control(state: RoastState, msg: Mapping[str, Any]) -> RoastState:
    channel = _opt_str(msg.get("channel"))
    if not channel:
        return state
    return replace(state, controls={**state.controls, channel: _num(msg.get("value"))})


def _apply_state(state: RoastState, msg: Mapping[str, Any]) -> RoastState:
    changes: dict[str, Any] = {}
    if "phase" in msg:
        changes["phase"] = _phase(msg["phase"])
    for key in ("monitoring", "recording", "artisan_online"):
        if key in msg:
            changes[key] = _opt_bool(msg[key])
    if "roast_id" in msg:
        changes["roast_id"] = _opt_str(msg["roast_id"])
    return replace(state, **changes)


def _apply_summary(state: RoastState, msg: Mapping[str, Any]) -> RoastState:
    summary = _summary(msg)
    final = summary if summary.get("final") is True else state.last_final_summary
    # The bridge freezes dev_time/DTR at DROP; samples are throttled, so the live values
    # can lag the drop by up to one sample interval. Adopt the authoritative ones.
    derived: dict[str, Any] = {}
    if summary.get("roast_id") == state.roast_id:
        derived = {
            key: value
            for key in ("dev_time", "dtr")
            if (value := _num(summary.get(key))) is not None
        }
    return replace(state, last_summary=summary, last_final_summary=final, **derived)


_REDUCERS = {
    "hello": _apply_hello,
    "snapshot": _apply_snapshot,
    "sample": _apply_sample,
    "event": _apply_event,
    "control": _apply_control,
    "state": _apply_state,
    "summary": _apply_summary,
}


def apply(state: RoastState, msg: Mapping[str, Any]) -> RoastState:
    """Return the state after applying one bridge message.

    Pure: no I/O, no clock. Unknown message types (and `ping`/`alert`, which carry
    no state) return the state unchanged, so additive protocol changes are safe.
    """
    msg_type = msg.get("type")
    reducer = _REDUCERS.get(msg_type) if isinstance(msg_type, str) else None
    return reducer(state, msg) if reducer is not None else state

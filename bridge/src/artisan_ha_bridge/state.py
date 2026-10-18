"""Roast state machine (spec 4.3). Pure logic: no I/O, injectable clock.

Every mutation reports its effect as protocol messages through the listener callback; sockets
and fan-out live elsewhere.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from .protocol import (
    EventName,
    EventRecord,
    LastEvent,
    OutboundMessage,
    Phase,
    StateSnapshot,
    SummaryMessage,
    SummaryNumberField,
    build_alert,
    build_control,
    build_event,
    build_sample,
    build_state,
    build_summary,
    dtr_percent,
)
from .ror import RorCalculator

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[OutboundMessage], None]
Clock = Callable[[], float]

# Event -> (phases it is valid in, phase it moves to).
_TRANSITIONS: Final[dict[str, tuple[frozenset[Phase], Phase]]] = {
    "start": (frozenset({"monitoring"}), "recording"),
    "charge": (frozenset({"monitoring", "recording"}), "drying"),
    "dry": (frozenset({"drying"}), "maillard"),
    "fcs": (frozenset({"drying", "maillard"}), "development"),
    "drop": (frozenset({"drying", "maillard", "development"}), "cooling"),
    "cool": (frozenset({"cooling"}), "finished"),
}
_DEVELOPMENT_MARKS: Final = frozenset({"fce", "scs", "sce"})
_AFTER_DROP: Final[frozenset[Phase]] = frozenset({"cooling", "finished"})
# Alog charge time and bridge charge time may differ by this much and still be the same roast.
ENRICHMENT_MATCH_S: Final = 60.0


@dataclass(frozen=True, slots=True)
class Enrichment:
    """Roast metadata from an Artisan `.alog` file."""

    charge_epoch: float
    title: str | None = None
    beans: str | None = None
    weight_in_g: float | None = None
    weight_out_g: float | None = None
    # Values from the alog `computed` dict, used only where the bridge recorded nothing.
    fills: Mapping[SummaryNumberField, float] = field(default_factory=dict)


def roast_id_for(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RoastStateMachine:
    def __init__(
        self, listener: Listener, *, ror_window_s: float = 30.0, clock: Clock = time.time
    ) -> None:
        self._listener = listener
        self._clock = clock
        self._phase: Phase = "off"
        self._artisan_online = False
        self._roast_id: str | None = None
        self._charge_epoch = 0.0
        self._tm: float | None = None
        self._bt: float | None = None
        self._et: float | None = None
        self._bt_ror_calc = RorCalculator(ror_window_s)
        self._et_ror_calc = RorCalculator(ror_window_s)
        self._bt_ror: float | None = None
        self._et_ror: float | None = None
        # RoR uses monotonic time before charge and roast time (tm) after it.
        self._ror_on_tm = False
        self._dev_time: float | None = None
        self._dtr: float | None = None
        self._controls: dict[str, float | None] = {}
        self._events: dict[str, EventRecord] = {}
        self._last_event: LastEvent | None = None
        # The last summary together with the charge epoch of the roast it describes.
        self._last_summary: tuple[SummaryMessage, float] | None = None
        self._summary_pending = False  # a non-final summary went out, the final one has not
        self._enrichment: Enrichment | None = None

    # --- read-only view ----------------------------------------------------------------------

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def monitoring(self) -> bool:
        return self._phase != "off"

    @property
    def recording(self) -> bool:
        return self._phase not in ("off", "monitoring")

    @property
    def artisan_online(self) -> bool:
        return self._artisan_online

    @property
    def roast_id(self) -> str | None:
        return self._roast_id

    def snapshot(self) -> StateSnapshot:
        return {
            "phase": self._phase,
            "monitoring": self.monitoring,
            "recording": self.recording,
            "artisan_online": self._artisan_online,
            "roast_id": self._roast_id,
            "tm": self._tm,
            "bt": self._bt,
            "et": self._et,
            "bt_ror": self._bt_ror,
            "et_ror": self._et_ror,
            "dev_time": self._dev_time,
            "dtr": self._dtr,
            "controls": dict(self._controls),
            "events": {name: record.copy() for name, record in self._events.items()},
            "last_event": None if self._last_event is None else self._last_event.copy(),
            "last_summary": None if self._last_summary is None else self._last_summary[0].copy(),
        }

    # --- inputs ------------------------------------------------------------------------------

    def on_sample(self, bt: float | None, et: float | None, tm: float | None, now: float) -> None:
        """One WebLCDs frame. `now` is monotonic seconds, used as the RoR x-axis before charge."""
        before = self._status()
        if self._phase == "off":
            self._phase = "monitoring"
        self._artisan_online = True
        self._bt, self._et, self._tm = bt, et, tm
        x = tm if self._ror_on_tm else now
        if x is not None:
            self._bt_ror_calc.add(x, bt)
            self._et_ror_calc.add(x, et)
        self._bt_ror = self._bt_ror_calc.value()
        self._et_ror = self._et_ror_calc.value()
        if self._phase == "development":
            self._update_live_dev()
        self._emit_state_if_changed(before)
        self._emit(
            build_sample(self._ts(), tm=tm, bt=bt, et=et, bt_ror=self._bt_ror, et_ror=self._et_ror)
        )

    def on_event(
        self, event: EventName, bt: float | None, et: float | None, tm: float | None
    ) -> None:
        before = self._status()
        # Artisan sends -1 (already mapped to None) when it is not sampling; use the latest frame.
        bt = self._bt if bt is None else bt
        et = self._et if et is None else et
        tm = self._tm if tm is None else tm
        summary: SummaryMessage | None = None
        if event == "on":
            if self._phase == "off":
                self._phase = "monitoring"
            self._artisan_online = True
        elif event == "off":
            summary = self._handle_off()
        elif event == "reset":
            self._clear_roast()
            self._phase = "monitoring" if self._artisan_online else "off"
        else:
            summary = self._handle_roast_event(event, bt, tm)
        ts = self._ts()
        self._last_event = {"event": event, "ts": ts}
        self._emit(build_event(ts, event=event, tm=tm, bt=bt, et=et, roast_id=self._roast_id))
        self._emit_state_if_changed(before)
        if summary is not None:
            self._publish_summary(summary, self._charge_epoch)

    def on_control(self, channel: str, value: float | None) -> None:
        self._controls[channel] = value
        self._emit(build_control(self._ts(), channel=channel, value=value))

    def on_alert(self, text: str, title: str | None) -> None:
        self._emit(build_alert(self._ts(), text=text, title=title))

    def set_artisan_online(self, online: bool) -> None:
        before = self._status()
        self._artisan_online = online
        self._emit_state_if_changed(before)

    def enrich_summary(self, enrichment: Enrichment) -> bool:
        """Merge `.alog` metadata into the last summary and re-send it if the roast matches."""
        if self._last_summary is None:
            return False
        last, charge_epoch = self._last_summary
        if abs(enrichment.charge_epoch - charge_epoch) > ENRICHMENT_MATCH_S:
            return False
        if self._roast_id is not None and self._charge_epoch == charge_epoch:
            # Still the current roast: later summaries (e.g. the final one at COOL) keep it.
            self._enrichment = enrichment
        summary = _apply_enrichment(last.copy(), enrichment)
        summary["ts"] = self._ts()
        self._publish_summary(summary, charge_epoch)
        return True

    # --- internals ---------------------------------------------------------------------------

    def _handle_off(self) -> SummaryMessage | None:
        summary = self._build_summary(final=True) if self._summary_pending else None
        self._phase = "off"
        self._reset_ror()
        self._ror_on_tm = False
        return summary

    def _handle_roast_event(
        self, event: EventName, bt: float | None, tm: float | None
    ) -> SummaryMessage | None:
        if self._phase == "off" and event in ("start", "charge"):
            # Artisan is clearly sampling; the `on` message was probably dropped on reconnect.
            self._phase = "monitoring"
            self._artisan_online = True
        rule = _TRANSITIONS.get(event)
        transitioned = rule is not None and self._phase in rule[0]
        if rule is not None and transitioned:
            if event == "charge":
                self._new_roast()
            elif event == "fcs":
                if self._phase == "drying":
                    _LOGGER.info("First crack without dry end; inferring maillard was passed")
                self._dev_time, self._dtr = 0.0, 0.0
            self._phase = rule[1]
        elif not (event in _DEVELOPMENT_MARKS and self._phase == "development"):
            if event in self._events or (event == "start" and self._phase == "recording"):
                _LOGGER.debug("Duplicate %s event; overwriting the earlier record", event)
            else:
                _LOGGER.warning(
                    "Out-of-order %s event in phase %s; recorded only", event, self._phase
                )
        if event != "start":
            self._events[event] = {"tm": tm, "bt": bt}
        if self._phase in _AFTER_DROP:
            # Dev time is frozen at drop, but a corrected (duplicate) drop/fcs may refine it.
            self._dev_time, self._dtr = self._frozen_dev()
        if transitioned and event == "drop":
            self._summary_pending = True
            return self._build_summary(final=False)
        if transitioned and event == "cool":
            return self._build_summary(final=True)
        return None

    def _new_roast(self) -> None:
        self._clear_roast()
        self._charge_epoch = self._clock()
        self._roast_id = roast_id_for(self._charge_epoch)
        self._ror_on_tm = True

    def _clear_roast(self) -> None:
        self._roast_id = None
        self._events = {}
        self._dev_time = None
        self._dtr = None
        self._summary_pending = False
        self._enrichment = None
        self._ror_on_tm = False
        self._reset_ror()

    def _reset_ror(self) -> None:
        self._bt_ror_calc.reset()
        self._et_ror_calc.reset()
        self._bt_ror = None
        self._et_ror = None

    def _update_live_dev(self) -> None:
        fcs_tm = self._events["fcs"]["tm"]  # development is only entered via a recorded fcs
        if self._tm is None or fcs_tm is None:
            return
        self._dev_time = round(max(0.0, self._tm - fcs_tm), 1)
        self._dtr = dtr_percent(self._dev_time, self._tm)

    def _frozen_dev(self) -> tuple[float | None, float | None]:
        fcs_tm = self._record("fcs", "tm")
        drop_tm = self._record("drop", "tm")
        if fcs_tm is None or drop_tm is None:
            return None, None
        dev_time = round(drop_tm - fcs_tm, 1)
        return dev_time, dtr_percent(dev_time, drop_tm)

    def _record(self, event: str, key: str) -> float | None:
        record = self._events.get(event)
        if record is None:
            return None
        return record["tm"] if key == "tm" else record["bt"]

    def _build_summary(self, *, final: bool) -> SummaryMessage:
        if final:
            self._summary_pending = False
        dev_time, dtr = self._frozen_dev()
        summary = build_summary(
            self._ts(),
            roast_id=self._roast_id,
            final=final,
            charge_bt=self._record("charge", "bt"),
            dry_tm=self._record("dry", "tm"),
            dry_bt=self._record("dry", "bt"),
            fcs_tm=self._record("fcs", "tm"),
            fcs_bt=self._record("fcs", "bt"),
            drop_tm=self._record("drop", "tm"),
            drop_bt=self._record("drop", "bt"),
            dev_time=dev_time,
            dtr=dtr,
            cool_tm=self._record("cool", "tm"),
        )
        if self._enrichment is not None:
            summary = _apply_enrichment(summary, self._enrichment)
        return summary

    def _publish_summary(self, summary: SummaryMessage, charge_epoch: float) -> None:
        self._last_summary = (summary, charge_epoch)
        self._emit(summary)

    def _status(self) -> tuple[Phase, bool, str | None]:
        return self._phase, self._artisan_online, self._roast_id

    def _emit_state_if_changed(self, before: tuple[Phase, bool, str | None]) -> None:
        if self._status() == before:
            return
        self._emit(
            build_state(
                self._ts(),
                phase=self._phase,
                monitoring=self.monitoring,
                recording=self.recording,
                artisan_online=self._artisan_online,
                roast_id=self._roast_id,
            )
        )

    def _ts(self) -> int:
        return int(self._clock() * 1000)

    def _emit(self, message: OutboundMessage) -> None:
        self._listener(message)


def _apply_enrichment(summary: SummaryMessage, enrichment: Enrichment) -> SummaryMessage:
    for key, value in enrichment.fills.items():
        if summary[key] is None:
            summary[key] = value
    if summary["dtr"] is None:
        summary["dtr"] = dtr_percent(summary["dev_time"], summary["drop_tm"])
    summary["title"] = enrichment.title or summary["title"]
    summary["beans"] = enrichment.beans or summary["beans"]
    summary["weight_in_g"] = enrichment.weight_in_g or summary["weight_in_g"]
    summary["weight_out_g"] = enrichment.weight_out_g or summary["weight_out_g"]
    return summary

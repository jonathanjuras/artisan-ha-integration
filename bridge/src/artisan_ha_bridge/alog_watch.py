"""Optional `.alog` enrichment (spec 4.3, `[watch]` extra).

Artisan writes `.alog` files as a Python literal (`repr` of a dict), so they are parsed with
`ast.literal_eval`, never `eval`.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from .protocol import SummaryNumberField
from .state import Enrichment

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

_LOGGER = logging.getLogger(__name__)

DEBOUNCE_S: Final = 2.0
MAX_ALOG_BYTES: Final = 32 * 1024 * 1024
GRAMS_PER_UNIT: Final[dict[str, float]] = {
    "g": 1.0,
    "kg": 1000.0,
    "lb": 453.59237,
    "oz": 28.349523125,
}
# Artisan `computed` keys -> summary fields they may fill. Times are seconds since CHARGE.
COMPUTED_FILLS: Final[dict[str, SummaryNumberField]] = {
    "CHARGE_BT": "charge_bt",
    "DRY_time": "dry_tm",
    "DRY_BT": "dry_bt",
    "FCs_time": "fcs_tm",
    "FCs_BT": "fcs_bt",
    "DROP_time": "drop_tm",
    "DROP_BT": "drop_bt",
    "finishphasetime": "dev_time",
    "COOL_time": "cool_tm",
}


class AlogError(ValueError):
    """The file is not a usable Artisan profile."""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _charge_epoch(data: dict[str, Any]) -> float | None:
    """Wall-clock time of CHARGE.

    `roastepoch` marks the start of recording and `timex` is relative to it, so CHARGE is
    `roastepoch + timex[timeindex[0]]` (`timeindex[0]` is -1 when CHARGE was never marked).
    """
    offset = 0.0
    timex, timeindex = data.get("timex"), data.get("timeindex")
    if isinstance(timex, list) and isinstance(timeindex, list) and timeindex:
        charge_index = timeindex[0]
        if isinstance(charge_index, int) and 0 <= charge_index < len(timex):
            offset = _number(timex[charge_index]) or 0.0
    epoch = _number(data.get("roastepoch"))
    if epoch is None:
        date, clock = _text(data.get("roastisodate")), _text(data.get("roasttime"))
        if date is None or clock is None:
            return None
        try:
            # Local wall-clock time, as Artisan records it.
            epoch = datetime.fromisoformat(f"{date}T{clock}").astimezone().timestamp()
        except ValueError:
            return None
    return epoch + offset


def _weights(data: dict[str, Any]) -> tuple[float | None, float | None]:
    weight = data.get("weight")
    if not isinstance(weight, list | tuple) or len(weight) < 3:
        return None, None
    unit = weight[2].lower() if isinstance(weight[2], str) else ""
    factor = GRAMS_PER_UNIT.get(unit)
    if factor is None:
        return None, None
    grams: list[float | None] = []
    for raw in weight[:2]:
        value = _number(raw)
        # Artisan stores 0 for "not entered".
        grams.append(round(value * factor, 1) if value is not None and value > 0 else None)
    return grams[0], grams[1]


def parse_alog(text: str) -> Enrichment:
    try:
        data = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as err:
        raise AlogError(f"not a Python literal: {err}") from None
    if not isinstance(data, dict):
        raise AlogError("profile is not a dict")
    charge_epoch = _charge_epoch(data)
    if charge_epoch is None:
        raise AlogError("no roast date in profile")
    fills: dict[SummaryNumberField, float] = {}
    computed = data.get("computed")
    if isinstance(computed, dict):
        for key, field in COMPUTED_FILLS.items():
            value = _number(computed.get(key))
            if value is not None:
                fills[field] = value
    weight_in, weight_out = _weights(data)
    return Enrichment(
        charge_epoch=charge_epoch,
        title=_text(data.get("title")),
        beans=_text(data.get("beans")),
        weight_in_g=weight_in,
        weight_out_g=weight_out,
        fills=fills,
    )


def read_alog(path: Path) -> Enrichment:
    """Blocking: run in a worker thread."""
    if path.stat().st_size > MAX_ALOG_BYTES:
        raise AlogError("file too large")
    return parse_alog(path.read_text(encoding="utf-8", errors="replace"))


class AlogWatcher:
    """Watches `directory` for new or modified `*.alog` files, debounced per file.

    watchdog's observer runs in its own thread; events are handed to the event loop with
    `call_soon_threadsafe` and all further work happens on the loop.
    """

    def __init__(
        self,
        directory: Path,
        on_enrichment: Callable[[Enrichment], None],
        *,
        debounce_s: float = DEBOUNCE_S,
    ) -> None:
        self._directory = directory
        self._on_enrichment = on_enrichment
        self._debounce = debounce_s
        self._timers: dict[Path, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._observer: BaseObserver | None = None

    async def start(self) -> None:
        try:
            # Optional [watch] extra, so imported only when the feature is enabled.
            from watchdog.events import FileSystemEvent, FileSystemEventHandler  # noqa: PLC0415
            from watchdog.observers import Observer  # noqa: PLC0415
        except ImportError:
            _LOGGER.error(  # noqa: TRY400 - a missing extra needs no traceback
                "autosave_dir is set but watchdog is missing; install artisan-ha-bridge[watch]"
            )
            return
        loop = asyncio.get_running_loop()
        notify = self.notify

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent) -> None:
                if event.is_directory or event.event_type not in ("created", "modified", "moved"):
                    return
                raw = event.dest_path if event.event_type == "moved" else event.src_path
                path = Path(raw.decode() if isinstance(raw, bytes) else raw)
                if path.suffix.lower() == ".alog":
                    loop.call_soon_threadsafe(notify, path)

        observer = Observer()
        observer.schedule(_Handler(), str(self._directory), recursive=False)
        observer.start()
        self._observer = observer
        _LOGGER.info("Watching %s for .alog files", self._directory)

    async def stop(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Done-callbacks (`discard`) run on a later loop tick, so an already-finished task can
        # still be in the set here; drop them explicitly.
        self._tasks.difference_update(tasks)
        if self._observer is not None:
            self._observer.stop()
            await asyncio.to_thread(self._observer.join, 5.0)
            self._observer = None

    def notify(self, path: Path) -> None:
        """A file changed. Artisan writes in several steps, so wait for it to settle."""
        timer = self._timers.pop(path, None)
        if timer is not None:
            timer.cancel()
        loop = asyncio.get_running_loop()
        self._timers[path] = loop.call_later(self._debounce, self._settled, path)

    def _settled(self, path: Path) -> None:
        self._timers.pop(path, None)
        task = asyncio.create_task(self._process(path))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, path: Path) -> None:
        try:
            enrichment = await asyncio.to_thread(read_alog, path)
        except (OSError, AlogError) as err:
            _LOGGER.warning("Cannot use %s: %s", path.name, err)
            return
        self._on_enrichment(enrichment)

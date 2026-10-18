"""Replay recorded WebLCDs frames and ingest messages (fixture format in spec section 9)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .ingest import dispatch
from .protocol import IngestError, parse_ingest
from .sources import EventSink
from .weblcds_client import FrameHandler

_LOGGER = logging.getLogger(__name__)

LINGER_TICK_S: Final = 0.5


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    dt: float
    src: Literal["weblcds", "ingest"]
    msg: object


def parse_records(lines: list[str], name: str = "<replay>") -> list[ReplayRecord]:
    records: list[ReplayRecord] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError as err:
            raise ValueError(f"{name}:{number}: invalid JSON: {err}") from None
        if not isinstance(raw, dict):
            raise ValueError(f"{name}:{number}: expected an object")
        dt, src = raw.get("dt"), raw.get("src")
        if isinstance(dt, bool) or not isinstance(dt, int | float):
            raise ValueError(f"{name}:{number}: 'dt' must be a number")
        if src not in ("weblcds", "ingest"):
            raise ValueError(f"{name}:{number}: 'src' must be 'weblcds' or 'ingest'")
        records.append(ReplayRecord(float(dt), src, raw.get("msg")))
    return records


def load_records(path: Path) -> list[ReplayRecord]:
    return parse_records(path.read_text(encoding="utf-8").splitlines(), path.name)


async def replay(
    records: list[ReplayRecord],
    *,
    frames: FrameHandler,
    sink: EventSink,
    speed: float = 1.0,
    linger: bool = False,
) -> None:
    """Feed `records` in real time divided by `speed`.

    Fixture time (`dt`) is the monotonic clock for frames and liveness, which keeps RoR and
    offline detection independent of the speed and of scheduling jitter. With `linger`, keep
    advancing fixture time after the last record (so a recording that just stops, like a crash,
    eventually marks Artisan offline) until canceled.
    """
    if speed <= 0:
        raise ValueError("speed must be positive")
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_dt = 0.0
    for record in records:
        frames.check_liveness(record.dt)
        delay = started + record.dt / speed - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        last_dt = record.dt
        if record.src == "weblcds":
            frames.handle(record.msg, now=record.dt)
            continue
        try:
            dispatch(parse_ingest(record.msg), sink)
        except IngestError as err:
            _LOGGER.warning("Replay: dropping ingest message at dt=%s: %s", record.dt, err)
    _LOGGER.info("Replay finished (%d records)", len(records))
    if not linger:
        return
    ended = loop.time()
    while True:
        await asyncio.sleep(LINGER_TICK_S / speed)
        frames.check_liveness(last_dt + (loop.time() - ended) * speed)

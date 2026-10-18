"""Regenerate the recorded fixtures in tests/fixtures/.

    python tests/make_fixtures.py

The curves are synthetic but shaped like a real 10-minute light-medium roast at 1 Hz: preheated
drum, CHARGE at 185 degC, turning point near 96 degC at 1:30, DRY END at 4:05, FC START at 7:44,
DROP at 10:20, COOL END at 12:20. Frames and ingest messages use Artisan's exact formats.
"""

from __future__ import annotations

import itertools
import json
import math
import random
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent / "fixtures"

START_DT = 30  # START pressed 30 s after ON
CHARGE_DT = 60  # CHARGE 30 s after START
# Roast time (seconds since CHARGE) of each event.
EVENTS_TM = {"dry": 245, "fcs": 464, "drop": 620, "cool": 740, "off": 760}

BT_CURVE = [
    (0, 185.2), (15, 140.0), (30, 118.0), (45, 105.0), (60, 99.0), (75, 96.5), (90, 96.0),
    (105, 98.0), (120, 102.0), (150, 113.0), (180, 125.0), (210, 138.0), (245, 152.1),
    (300, 166.5), (360, 178.5), (420, 188.2), (464, 194.0), (520, 199.8), (580, 205.0),
    (620, 208.4), (635, 190.0), (650, 160.0), (680, 110.0), (710, 75.0), (740, 55.0),
    (760, 48.0),
]  # fmt: skip
ET_CURVE = [
    (0, 232.0), (30, 205.0), (60, 198.0), (120, 205.0), (245, 215.0), (464, 224.0),
    (620, 229.0), (650, 180.0), (700, 120.0), (760, 90.0),
]  # fmt: skip
CONTROLS = {  # roast time -> (channel, value)
    0: [("heat", 70.0), ("fan", 30.0)],
    245: [("heat", 80.0)],
    464: [("heat", 50.0), ("fan", 60.0)],
    620: [("fan", 100.0)],
}


def interpolate(curve: list[tuple[int, float]], x: float) -> float:
    for (x0, y0), (x1, y1) in itertools.pairwise(curve):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


def mmss(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return f"{sign}{seconds // 60:02d}:{seconds % 60:02d}"


def readings(dt: int) -> tuple[float, float, int]:
    """(bt, et, timer seconds) as Artisan displays them at fixture second `dt`."""
    if dt < CHARGE_DT:
        bt = 200.0 + 0.02 * dt
        et = 236.0 - 0.01 * dt
        timer = 0 if dt < START_DT else dt - START_DT
    else:
        tm = dt - CHARGE_DT
        event_time = tm == 0 or tm in EVENTS_TM.values()
        noise = 0.0 if event_time else 0.1 * math.sin(dt * 1.7)
        bt = interpolate(BT_CURVE, tm) + noise
        et = interpolate(ET_CURVE, tm) + noise
        timer = tm
    return round(bt, 1), round(et, 1), timer


class Recording:
    def __init__(self, seed: int) -> None:
        self.lines: list[dict[str, Any]] = []
        self.random = random.Random(seed)

    def frame(self, dt: int, *, et_missing: bool = False, timer: int | None = None) -> None:
        bt, et, shown = readings(dt)
        data = {
            "bt": f"{bt:.1f}",
            "et": "&nbsp;&nbsp;-.-" if et_missing else f"{et:.1f}",
            "time": mmss(shown if timer is None else timer),
        }
        self.lines.append({"dt": dt, "src": "weblcds", "msg": {"data": data}})

    def alert(self, dt: float, text: str) -> None:
        msg = {"alert": {"text": text, "title": "Alarm", "timeout": 10}}
        self.lines.append({"dt": dt, "src": "weblcds", "msg": msg})

    def event(self, dt: float, event: str, *, unknown: bool = False) -> None:
        if unknown:  # ON fires before the first sample: Artisan substitutes -1
            bt = et = tm = -1.0
        else:
            bt, et, timer = readings(int(dt))
            tm = float(timer) if dt >= START_DT else 0.0
        msg = {
            "k": "ev",
            "e": event,
            "bt": bt,
            "et": et,
            "tm": tm,
            "id": self.random.randint(1, 99999),
        }
        self.lines.append({"dt": dt, "src": "ingest", "msg": msg})

    def control(self, dt: float, channel: str, value: float) -> None:
        msg = {"k": "ctl", "ch": channel, "v": value, "id": self.random.randint(1, 99999)}
        self.lines.append({"dt": dt, "src": "ingest", "msg": msg})

    def write(self, name: str) -> None:
        with (OUT / name).open("w", encoding="utf-8") as handle:
            for line in self.lines:
                handle.write(json.dumps(line, separators=(",", ":")) + "\n")


def roast(
    *, with_on: bool, until_dt: int | None = None, reset_at_tm: int | None = None
) -> Recording:
    rec = Recording(seed=17)
    if with_on:
        rec.event(0, "on", unknown=True)
    end_dt = CHARGE_DT + EVENTS_TM["off"]
    by_tm = {tm: name for name, tm in EVENTS_TM.items()}
    for dt in range(end_dt + 1):
        if until_dt is not None and dt > until_dt:
            break
        tm = dt - CHARGE_DT
        if reset_at_tm is not None and tm >= reset_at_tm:
            if tm == reset_at_tm:
                rec.event(dt + 0.4, "reset")
            if tm < reset_at_tm + 20:
                rec.frame(dt + 1, timer=0)  # after RESET Artisan shows 00:00 again
            continue
        if dt == end_dt:
            rec.event(dt + 0.3, "off")  # Artisan stops sampling: no frame after OFF
            break
        rec.frame(dt, et_missing=(tm == 400))
        if dt == START_DT:
            rec.event(dt + 0.3, "start")
        if dt == CHARGE_DT:
            rec.event(dt + 0.3, "charge")
        if tm in by_tm and tm != EVENTS_TM["off"]:
            rec.event(dt + 0.3, by_tm[tm])
        for channel, value in CONTROLS.get(tm, []) if dt >= CHARGE_DT else []:
            rec.control(dt + 0.5, channel, value)
        if tm == 610:
            rec.alert(dt + 0.6, "Drop in 10 s")
    return rec


def alog() -> str:
    """An Artisan profile for the full roast, charged at 2026-10-17T14:03:12Z."""
    samples = range(CHARGE_DT - START_DT + EVENTS_TM["off"] + 1)  # 1 Hz from START
    timex = [float(i) for i in samples]
    temp1 = [readings(i + START_DT)[1] for i in samples]  # ET
    temp2 = [readings(i + START_DT)[0] for i in samples]  # BT
    offset = CHARGE_DT - START_DT
    profile: dict[str, Any] = {
        "recording_version": "3.1.0",
        "recording_revision": "",
        "recording_build": "0",
        "version": "3.1.0",
        "revision": "",
        "build": "0",
        "artisan_os": "Darwin",
        "artisan_os_version": "15.0",
        "artisan_os_arch": "arm64",
        "mode": "C",
        "viewerMode": False,
        "timeindex": [
            offset,
            offset + EVENTS_TM["dry"],
            offset + EVENTS_TM["fcs"],
            0,
            0,
            0,
            offset + EVENTS_TM["drop"],
            offset + EVENTS_TM["cool"],
        ],
        "flavors": [5.0] * 9,
        "flavors_total_correction": 0.0,
        "flavorlabels": [
            "Acidity",
            "Aftertaste",
            "Clean Cup",
            "Head",
            "Fragrance",
            "Sweetness",
            "Aroma",
            "Balance",
            "Body",
        ],
        "title": "Ethiopia Guji",
        "locale": "en",
        "beans": "Ethiopia Guji natural, grade 1",
        "weight": [250.0, 212.5, "g"],
        "volume": [0.0, 0.0, "l"],
        "density": [0.0, "g", 1.0, "l"],
        "roastertype": "Aillio Bullet R1",
        "operator": "",
        "organization": "",
        "drumspeed": "",
        "heavyFC": False,
        "lowFC": False,
        "lightCut": False,
        "darkCut": False,
        "drops": False,
        "oily": False,
        "uneven": False,
        "tipping": False,
        "scorching": False,
        "divots": False,
        "whole_color": 0,
        "ground_color": 0,
        "color_system": "",
        "roastdate": "Sat Oct 17 2026",
        "roastisodate": "2026-10-17",
        "roasttime": "16:02:42",
        "roastepoch": 1792245762,
        "roasttzoffset": -7200,
        "roastbatchnr": 0,
        "roastbatchprefix": "",
        "roastbatchpos": 1,
        "roastUUID": "6f2f1b2e8f8a4c1f9a4c9d5d0b6b1e77",
        "beansize": "0",
        "specialevents": [],
        "specialeventstype": [],
        "specialeventsvalue": [],
        "specialeventsStrings": [],
        "etypes": ["Air", "Drum", "Damper", "Burner", "--"],
        "roastingnotes": "",
        "cuppingnotes": "",
        "timex": timex,
        "temp1": temp1,
        "temp2": temp2,
        "phases": [150, 150, 195, 195],
        "zmax": 100,
        "zmin": 0,
        "ymax": 250,
        "ymin": 0,
        "xmin": -30.0,
        "xmax": 900.0,
        "ambientTemp": 21.0,
        "computed": {
            "CHARGE_ET": 232.0,
            "CHARGE_BT": 185.2,
            "TP_idx": offset + 90,
            "TP_time": 90.0,
            "TP_ET": 201.5,
            "TP_BT": 96.0,
            "DRY_time": 245.0,
            "DRY_ET": 215.0,
            "DRY_BT": 152.1,
            "FCs_time": 464.0,
            "FCs_ET": 224.0,
            "FCs_BT": 194.0,
            "DROP_time": 620.0,
            "DROP_ET": 229.0,
            "DROP_BT": 208.4,
            "COOL_time": 740.0,
            "totaltime": 620.0,
            "dryphasetime": 245.0,
            "midphasetime": 219.0,
            "finishphasetime": 156.0,
            "dry_phase_ror": 13.7,
            "mid_phase_ror": 11.5,
            "finish_phase_ror": 5.5,
            "total_ror": 2.2,
            "fcs_ror": 7.1,
            "weight_loss": 15.0,
            "volume_gain": 0.0,
            "moisture_greens": 0.0,
            "AUC": 0,
            "AUCbegin": "TP",
            "AUCbase": 0.0,
        },
        "anno_positions": [],
        "extradevices": [],
    }
    return repr(profile) + "\n"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    roast(with_on=True).write("full_roast.jsonl")
    roast(with_on=False).write("dropped_on.jsonl")
    roast(with_on=True, reset_at_tm=300).write("reset_mid_roast.jsonl")
    roast(with_on=True, until_dt=CHARGE_DT + 360).write("artisan_crash.jsonl")
    (OUT / "sample_roast.alog").write_text(alog(), encoding="utf-8")


if __name__ == "__main__":
    main()

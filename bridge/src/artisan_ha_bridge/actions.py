"""The Artisan wire-up instructions and action table (spec section 6) for `print-actions`."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

EXECUTABLE: Final = "artisan-ha-bridge"

# (slot label, event name)
EVENT_SLOTS: Final[tuple[tuple[str, str], ...]] = (
    ("ON", "on"),
    ("START", "start"),
    ("CHARGE", "charge"),
    ("DRY END", "dry"),
    ("FC START", "fcs"),
    ("FC END", "fce"),
    ("SC START", "scs"),
    ("SC END", "sce"),
    ("DROP", "drop"),
    ("COOL END", "cool"),
    ("OFF", "off"),
    ("RESET", "reset"),
)
SLIDERS: Final[tuple[tuple[str, str], ...]] = (("Heat slider", "heat"), ("Fan slider", "fan"))


def _quote(arg: str) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline([arg])
    return shlex.quote(arg)


def executable_command() -> str:
    """Absolute command line that starts this bridge (for Artisan's Call Program)."""
    found = shutil.which(EXECUTABLE)
    if found:
        return _quote(os.path.abspath(found))
    argv0 = Path(sys.argv[0])
    if argv0.name.startswith(EXECUTABLE):
        return _quote(str(argv0.absolute()))
    return f"{_quote(sys.executable)} -m artisan_ha_bridge"


def action_rows(command: str) -> list[tuple[str, str, str]]:
    rows = [
        (
            label,
            f"send(dict(k='ev',e='{event}',bt={{BT}},et={{ET}},tm={{t}}))",
            f"{command} emit ev {event} {{BT}} {{ET}} {{t}}",
        )
        for label, event in EVENT_SLOTS
    ]
    rows += [
        (label, f"send(dict(k='ctl',ch='{channel}',v={{}}))", f"{command} emit ctl {channel} {{}}")
        for label, channel in SLIDERS
    ]
    return rows


def render_actions(
    ingest_port: int,
    *,
    fallback: bool,
    weblcds_port: int = 8080,
    config_path: Path | None = None,
    command: str | None = None,
) -> str:
    """`config_path` is added to the fallback commands when it is not the default location."""
    base = (command or executable_command()) if fallback else EXECUTABLE
    if config_path is not None:
        base = f"{base} --config {_quote(str(config_path))}"
    rows = [("Slot", "WebSocket Command", "Call Program fallback"), *action_rows(base)]
    widths = [max(len(row[i]) for row in rows) for i in range(2)]
    table = [
        f"{slot:<{widths[0]}}  {ws:<{widths[1]}}  {program}".rstrip() for slot, ws, program in rows
    ]
    table.insert(1, f"{'-' * widths[0]}  {'-' * widths[1]}  {'-' * len(rows[0][2])}")
    steps = [
        f"Artisan wire-up for artisan-ha-bridge (ingest port {ingest_port})",
        "",
        f"1. Config > Curves > UI tab: enable WebLCDs, port {weblcds_port}. Tick Alarm Popups to"
        " forward alarms.",
        f"2. Config > Port > WebSocket tab: host 127.0.0.1, port {ingest_port}, path artisan."
        " If the roaster already uses the WebSocket device, use the Call Program column instead.",
        "3. Config > Events > Buttons: for each slot, Action = WebSocket Command with the string"
        " below. If a slot already drives the roaster, move the original action to a hidden"
        " custom button, add a second hidden button carrying the WebSocket Command, and set the"
        " slot to Multiple Event for both.",
        "4. Config > Events > Sliders: for sliders not already bound to roaster control,"
        " Action = WebSocket Command with the slider string.",
        "5. Optional: Config > Alarms with a Pop Up action; these reach HA as `alert`.",
        "6. Help > Save Settings.",
        "",
        *table,
        "",
    ]
    if not fallback:
        steps.append(
            "On Windows, Call Program needs the absolute path: run `artisan-ha-bridge"
            " print-actions --fallback`."
        )
    return "\n".join(steps)

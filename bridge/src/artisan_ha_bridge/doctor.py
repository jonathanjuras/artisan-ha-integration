"""`doctor`: checks a configured, running bridge from the outside."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import aiohttp

from .config import Config, ConfigError, load_config
from .discovery import WILDCARD_BINDS
from .ha_server import PATH

TIMEOUT_S: Final = 3.0
# A client seen within this window counts as "recent" even if it is not connected right now.
RECENT_CLIENT_S: Final = 15 * 60.0


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def _local_host(bind: str) -> str:
    if bind == "::":
        return "[::1]"
    return "127.0.0.1" if bind in WILDCARD_BINDS else bind


async def _check_weblcds(session: aiohttp.ClientSession, config: Config) -> Check:
    url = config.weblcds.url
    try:
        async with session.ws_connect(url):
            pass
    except (aiohttp.ClientError, OSError, TimeoutError) as err:
        return Check(
            "WebLCDs",
            False,
            f"cannot connect to {url} ({err or type(err).__name__}); is Artisan running with"
            " WebLCDs enabled (Config > Curves > UI)?",
        )
    return Check("WebLCDs", True, f"reachable at {url}")


async def _fetch_status(
    session: aiohttp.ClientSession, config: Config
) -> tuple[Check, dict[str, Any] | None]:
    url = f"http://127.0.0.1:{config.ingest.port}/status"
    try:
        async with session.get(url) as response:
            status = await response.json() if response.status == 200 else None
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
        return (
            Check(
                "Ingest",
                False,
                f"nothing usable on 127.0.0.1:{config.ingest.port}; is `artisan-ha-bridge run`"
                " running?",
            ),
            None,
        )
    if not isinstance(status, dict) or "bridge_id" not in status:
        return (
            Check("Ingest", False, f"port {config.ingest.port} is used by another program"),
            None,
        )
    if status["bridge_id"] != config.bridge_id:
        return (
            Check("Ingest", False, "a bridge with a different config is running on that port"),
            None,
        )
    return Check("Ingest", True, f"bridge answering on 127.0.0.1:{config.ingest.port}"), status


async def _check_ha_port(session: aiohttp.ClientSession, config: Config) -> Check:
    host = _local_host(config.ha.bind)
    url = f"http://{host}:{config.ha.port}{PATH}"
    try:
        async with session.get(url) as response:
            code = response.status
    except (aiohttp.ClientError, OSError, TimeoutError) as err:
        return Check("HA server", False, f"nothing listening at {url} ({type(err).__name__})")
    if code != 401:
        return Check("HA server", False, f"{url} answered {code}, expected 401 without a token")
    return Check("HA server", True, f"listening on {config.ha.bind}:{config.ha.port}{PATH}")


def _check_client(status: dict[str, Any] | None, now: float) -> Check:
    if status is None:
        return Check("HA client", False, "unknown: the bridge is not reachable")
    clients = status.get("ha_clients")
    if isinstance(clients, int) and clients > 0:
        return Check("HA client", True, f"{clients} connected")
    seen = status.get("ha_last_seen")
    if isinstance(seen, int | float) and now - seen <= RECENT_CLIENT_S:
        return Check("HA client", True, f"none connected now, last seen {now - seen:.0f} s ago")
    return Check(
        "HA client",
        False,
        "no Home Assistant connection recently; add the integration in HA with the token from"
        " `artisan-ha-bridge show-token` and check the firewall",
    )


async def run_checks(
    config_path: Path, *, limit_s: float = TIMEOUT_S, clock: Callable[[], float] = time.time
) -> list[Check]:
    try:
        config = load_config(config_path)
    except ConfigError as err:
        return [Check("Config", False, str(err))]
    problems = config.problems()
    checks = [
        Check("Config", not problems, "; ".join(problems) or f"{config_path} looks sane"),
    ]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=limit_s)) as session:
        weblcds, (ingest, status), ha_port = await asyncio.gather(
            _check_weblcds(session, config),
            _fetch_status(session, config),
            _check_ha_port(session, config),
        )
    checks += [weblcds, ingest, ha_port, _check_client(status, clock())]
    return checks


def format_checks(checks: list[Check]) -> str:
    lines = [f"[{' OK ' if check.ok else 'FAIL'}] {check.name}: {check.detail}" for check in checks]
    failed = sum(not check.ok for check in checks)
    lines.append("All checks passed." if not failed else f"{failed} check(s) failed.")
    return "\n".join(lines)

"""Command-line interface (spec 4.4)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import signal
import sys
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any, Final

import aiohttp

from . import __version__
from .actions import render_actions
from .app import AdvertiserFactory, Bridge, no_advertiser, zeroconf_advertiser
from .config import Config, ConfigError, IngestConfig, default_config_path, load_config, save_config
from .doctor import format_checks, run_checks
from .logging_setup import setup_logging
from .replay import load_records, replay

_LOGGER = logging.getLogger("artisan_ha_bridge")

EMIT_TIMEOUT_S: Final = 1.0

Handler = Callable[[argparse.Namespace, Path], int]


# --- emit ------------------------------------------------------------------------------------


def _emit_number(text: str | None, *, sentinel: bool) -> float | None:
    """Artisan may substitute -1, -1.0 or an empty/garbage string; all become null."""
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or (sentinel and value == -1):
        return None
    return value


def build_emit_payload(values: Sequence[str]) -> dict[str, Any] | None:
    if not values:
        return None
    padded = [*values, *([None] * 4)]
    if values[0] == "ev" and len(values) >= 2:
        bt, et, tm = (_emit_number(v, sentinel=True) for v in padded[2:5])
        return {"k": "ev", "e": values[1], "bt": bt, "et": et, "tm": tm}
    if values[0] == "ctl" and len(values) >= 2:
        return {"k": "ctl", "ch": values[1], "v": _emit_number(padded[2], sentinel=False)}
    return None


async def post_emit(
    port: int, payload: dict[str, Any], limit_s: float = EMIT_TIMEOUT_S
) -> str | None:
    """POST to the ingest. Returns an error description, or None on success."""
    url = f"http://127.0.0.1:{port}/emit"
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=limit_s)) as session,
            session.post(url, json=payload) as response,
        ):
            if response.status == 204:
                return None
            body = (await response.text()).strip()
            return f"bridge rejected the message ({response.status} {body})"
    except (aiohttp.ClientError, OSError, TimeoutError) as err:
        return f"cannot reach the bridge at {url} ({err or type(err).__name__})"


def cmd_emit(args: argparse.Namespace, config_path: Path) -> int:
    # Always exit 0: a failing Call Program must never disturb Artisan.
    payload = build_emit_payload(args.values)
    if payload is None:
        print(
            "usage: artisan-ha-bridge emit ev <event> <bt> <et> <tm> | emit ctl <channel> <value>",
            file=sys.stderr,
        )
        return 0
    port = IngestConfig().port
    with contextlib.suppress(ConfigError):
        port = load_config(config_path).ingest.port
    try:
        error = asyncio.run(post_emit(port, payload))
    except Exception as err:  # last resort, see above
        error = f"unexpected error: {err}"
    if error:
        print(f"artisan-ha-bridge emit: {error}", file=sys.stderr)
    return 0


# --- config commands -------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, config_path: Path) -> int:
    bridge_id: str | None = None
    if config_path.exists():
        if not args.force:
            print(
                f"Config already exists at {config_path}; use --force to replace it.",
                file=sys.stderr,
            )
            return 1
        # Keep the bridge identity so Home Assistant sees the same device; only the token changes.
        with contextlib.suppress(ConfigError):
            bridge_id = load_config(config_path).bridge_id
    config = Config.generate(bridge_id)
    try:
        save_config(config, config_path)
    except OSError as err:
        print(f"Cannot write {config_path}: {err}", file=sys.stderr)
        return 1
    print(f"Config written to {config_path}")
    print(f"Token: {config.ha.token}")
    print("Paste the token into Home Assistant when adding the Artisan integration.")
    return 0


def _load_or_report(config_path: Path) -> Config | None:
    try:
        return load_config(config_path)
    except ConfigError as err:
        print(f"artisan-ha-bridge: {err}", file=sys.stderr)
        return None


def cmd_show_token(_args: argparse.Namespace, config_path: Path) -> int:
    config = _load_or_report(config_path)
    if config is None:
        return 1
    print(config.ha.token)
    return 0


def cmd_print_actions(args: argparse.Namespace, config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as err:
        print(f"Note: {err}; showing defaults.", file=sys.stderr)
        config = Config(bridge_id="")
    print(
        render_actions(
            config.ingest.port,
            weblcds_port=config.weblcds.port,
            fallback=args.fallback,
            config_path=args.config,
        )
    )
    return 0


def cmd_doctor(_args: argparse.Namespace, config_path: Path) -> int:
    checks = asyncio.run(run_checks(config_path))
    print(format_checks(checks))
    return 0 if all(check.ok for check in checks) else 1


# --- long-running commands -------------------------------------------------------------------


async def serve(bridge: Bridge, job: Callable[[], Awaitable[None]] | None = None) -> None:
    """Run `bridge` until SIGINT/SIGTERM, or until `job` finishes when one is given."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not available on Windows; Ctrl+C then arrives as KeyboardInterrupt instead.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    await bridge.start()
    waiters: list[asyncio.Task[Any]] = [asyncio.create_task(stop.wait())]
    if job is not None:

        async def run_job() -> None:
            await job()

        waiters.append(asyncio.create_task(run_job()))
    try:
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # surface errors from the job
    finally:
        for task in waiters:
            task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        await bridge.stop()
        _LOGGER.info("Stopped")
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)


def _prepare(config_path: Path) -> Config | None:
    config = _load_or_report(config_path)
    if config is None:
        return None
    problems = config.problems()
    if problems:
        for problem in problems:
            print(f"artisan-ha-bridge: config problem: {problem}", file=sys.stderr)
        return None
    setup_logging(config.log.level, secrets=[config.ha.token])
    return config


def _run_until_stopped(main: Coroutine[Any, Any, None]) -> int:
    try:
        asyncio.run(main)
    except KeyboardInterrupt:
        pass
    except OSError as err:
        _LOGGER.error("Cannot start: %s", err)  # noqa: TRY400 - the message is the useful part
        return 1
    return 0


def _advertiser(args: argparse.Namespace) -> AdvertiserFactory:
    # Plain `artisan-ha-bridge` (no subcommand) has no --no-discovery attribute.
    return no_advertiser if getattr(args, "no_discovery", False) else zeroconf_advertiser


def cmd_run(args: argparse.Namespace, config_path: Path) -> int:
    config = _prepare(config_path)
    if config is None:
        return 2
    _LOGGER.info("artisan-ha-bridge %s starting (config %s)", __version__, config_path)
    return _run_until_stopped(serve(Bridge(config, advertiser_factory=_advertiser(args))))


def cmd_replay(args: argparse.Namespace, config_path: Path) -> int:
    config = _prepare(config_path)
    if config is None:
        return 2
    try:
        records = load_records(args.file)
    except (OSError, ValueError) as err:
        print(f"artisan-ha-bridge: cannot replay {args.file}: {err}", file=sys.stderr)
        return 2
    bridge = Bridge(
        config,
        telemetry=False,
        ingest=False,
        alog=False,
        advertiser_factory=_advertiser(args),
    )

    def job() -> Awaitable[None]:
        return replay(
            records,
            frames=bridge.frames,
            sink=bridge.state,
            speed=args.speed,
            linger=not args.exit_when_done,
        )

    return _run_until_stopped(serve(bridge, job))


# --- parser ----------------------------------------------------------------------------------


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not value > 0 or not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def build_parser() -> argparse.ArgumentParser:
    config_help = f"config file (default: {default_config_path()})"
    # Subcommands accept --config too; SUPPRESS keeps them from overwriting a global value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=config_help)

    parser = argparse.ArgumentParser(
        prog="artisan-ha-bridge",
        description="Bridge between the Artisan roaster scope and Home Assistant.",
    )
    parser.add_argument("--config", type=Path, default=None, metavar="PATH", help=config_help)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    no_discovery = argparse.ArgumentParser(add_help=False)
    no_discovery.add_argument(
        "--no-discovery", action="store_true", help="do not advertise the bridge via zeroconf"
    )
    sub.add_parser("run", parents=[common, no_discovery], help="run the bridge (default)")
    init = sub.add_parser("init", parents=[common], help="create the config with a new token")
    init.add_argument("--force", action="store_true", help="replace an existing config")
    sub.add_parser("show-token", parents=[common], help="print the Home Assistant token")
    emit = sub.add_parser(
        "emit",
        parents=[common],
        help="send one event or control value (Artisan Call Program fallback)",
        usage="%(prog)s ev <event> <bt> <et> <tm> | ctl <channel> <value>",
    )
    emit.add_argument("values", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    actions = sub.add_parser(
        "print-actions", parents=[common], help="print the Artisan event/slider actions"
    )
    actions.add_argument("--fallback", action="store_true", help="use the absolute executable path")
    sub.add_parser("doctor", parents=[common], help="check that everything is wired up")
    replay_parser = sub.add_parser(
        "replay",
        parents=[common, no_discovery],
        help="replay a recorded roast (JSONL) to Home Assistant",
    )
    replay_parser.add_argument("file", type=Path)
    replay_parser.add_argument("--speed", type=_positive_float, default=1.0, metavar="N")
    replay_parser.add_argument(
        "--exit", dest="exit_when_done", action="store_true", help="exit when the replay ends"
    )
    return parser


HANDLERS: Final[dict[str, Handler]] = {
    "run": cmd_run,
    "init": cmd_init,
    "show-token": cmd_show_token,
    "emit": cmd_emit,
    "print-actions": cmd_print_actions,
    "doctor": cmd_doctor,
    "replay": cmd_replay,
}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(arguments)
    except SystemExit as exc:
        if "emit" in arguments and exc.code not in (0, None):
            return 0  # emit never fails, even on bad arguments
        raise
    config_path: Path = args.config or default_config_path()
    return HANDLERS[args.command or "run"](args, config_path)


if __name__ == "__main__":
    sys.exit(main())

"""CLI commands: init, show-token, emit, print-actions, doctor, replay, run."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from artisan_ha_bridge import __main__ as cli
from artisan_ha_bridge import __version__, actions
from artisan_ha_bridge.config import Config, load_config, save_config
from artisan_ha_bridge.ingest import IngestServer
from artisan_ha_bridge.state import RoastStateMachine

from helpers import FIXTURES, Recorder


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """`run`/`replay` reconfigure the root logger; put pytest's handlers back afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "config.yaml"


def saved_config(path: Path, **changes: Any) -> Config:
    config = Config.generate()
    config.ha.bind = "127.0.0.1"
    config.ha.port = free_port()
    config.ingest.port = free_port()
    config.weblcds.port = free_port()
    config.ha.sample_interval_s = 0.05
    for key, value in changes.items():
        section, _, name = key.partition("__")
        target = getattr(config, section) if name else config
        setattr(target, name or key, value)
    save_config(config, path)
    return config


# --- init / show-token -------------------------------------------------------------------


def test_init(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--config", str(config_path), "init"]) == 0
    out = capsys.readouterr().out
    config = load_config(config_path)
    assert f"Config written to {config_path}" in out
    assert f"Token: {config.ha.token}" in out
    assert config.problems() == []

    assert cli.main(["init", "--config", str(config_path)]) == 1
    assert "use --force" in capsys.readouterr().err
    assert load_config(config_path) == config

    assert cli.main(["--config", str(config_path), "init", "--force"]) == 0
    replaced = load_config(config_path)
    assert replaced.bridge_id == config.bridge_id
    assert replaced.ha.token != config.ha.token


def test_init_force_over_broken_config(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("garbage: [")
    assert cli.main(["--config", str(config_path), "init", "--force"]) == 0
    assert load_config(config_path).problems() == []


def test_init_unwritable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert cli.main(["--config", str(blocker / "config.yaml"), "init"]) == 1
    assert "Cannot write" in capsys.readouterr().err


def test_show_token(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--config", str(config_path), "show-token"]) == 1
    assert "run `artisan-ha-bridge init`" in capsys.readouterr().err
    config = saved_config(config_path)
    assert cli.main(["show-token", "--config", str(config_path)]) == 0
    assert capsys.readouterr().out == f"{config.ha.token}\n"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_bad_arguments_exit_2() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["replay"])
    assert excinfo.value.code == 2


# --- print-actions -----------------------------------------------------------------------


def test_print_actions(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    saved_config(config_path, ingest__port=9876, weblcds__port=8181)
    assert cli.main(["--config", str(config_path), "print-actions"]) == 0
    out = capsys.readouterr().out
    assert "host 127.0.0.1, port 9876, path artisan" in out
    assert "enable WebLCDs, port 8181" in out
    assert "send(dict(k='ev',e='charge',bt={BT},et={ET},tm={t}))" in out
    assert "artisan-ha-bridge --config" in out
    assert "send(dict(k='ctl',ch='heat',v={}))" in out
    for label, event in actions.EVENT_SLOTS:
        assert f"emit ev {event} {{BT}} {{ET}} {{t}}" in out
        assert label in out
    assert "emit ctl fan {}" in out
    assert "print-actions --fallback" in out
    # No literal `_` or `$` in commands (Artisan substitutes them) and no `{...}` dict syntax.
    for _label, ws, _program in actions.action_rows("x"):
        assert "_" not in ws
        assert "$" not in ws
        assert "{'" not in ws


def test_print_actions_defaults_without_config(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--config", str(config_path), "print-actions"]) == 0
    captured = capsys.readouterr()
    assert "port 8766, path artisan" in captured.out
    assert "showing defaults" in captured.err


def test_print_actions_fallback(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = "/opt/pipx bin/artisan-ha-bridge"
    monkeypatch.setattr("shutil.which", lambda _name: exe)
    assert cli.main(["print-actions", "--fallback"]) == 0
    out = capsys.readouterr().out
    assert "'/opt/pipx bin/artisan-ha-bridge' emit ev drop {BT} {ET} {t}" in out
    assert "--fallback`" not in out


def test_executable_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/artisan-ha-bridge", "print-actions"])
    assert actions.executable_command() == "/usr/local/bin/artisan-ha-bridge"
    monkeypatch.setattr(sys, "argv", ["/src/artisan_ha_bridge/__main__.py"])
    assert actions.executable_command().endswith(" -m artisan_ha_bridge")
    monkeypatch.setattr("sys.platform", "win32")
    assert actions._quote(r"C:\Program Files\x.exe") == r'"C:\Program Files\x.exe"'


# --- emit --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["ev", "charge", "185.2", "220.1", "0"], {"k": "ev", "e": "charge", "bt": 185.2,
                                                   "et": 220.1, "tm": 0.0}),
        (["ev", "on", "-1", "-1.0", "-1"], {"k": "ev", "e": "on", "bt": None, "et": None,
                                            "tm": None}),
        (["ev", "dry", "{BT}", "nan", ""], {"k": "ev", "e": "dry", "bt": None, "et": None,
                                            "tm": None}),
        (["ev", "drop"], {"k": "ev", "e": "drop", "bt": None, "et": None, "tm": None}),
        (["ctl", "heat", "70"], {"k": "ctl", "ch": "heat", "v": 70.0}),
        (["ctl", "heat", "-1"], {"k": "ctl", "ch": "heat", "v": -1.0}),
        (["ctl", "fan", "x"], {"k": "ctl", "ch": "fan", "v": None}),
        (["ctl", "fan"], {"k": "ctl", "ch": "fan", "v": None}),
        ([], None),
        (["ev"], None),
        (["zz", "a"], None),
    ],
)  # fmt: skip
def test_build_emit_payload(values: list[str], expected: dict[str, Any] | None) -> None:
    assert cli.build_emit_payload(values) == expected


@pytest.fixture
def threaded_ingest() -> Iterator[tuple[IngestServer, RoastStateMachine]]:
    """An ingest server on its own loop thread, since `emit` runs its own `asyncio.run`."""
    loop = asyncio.new_event_loop()
    sm = RoastStateMachine(Recorder())
    server = IngestServer(0, sm)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    asyncio.run_coroutine_threadsafe(server.start(), loop).result(5)
    yield server, sm
    asyncio.run_coroutine_threadsafe(server.stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


def test_emit_end_to_end(
    config_path: Path,
    threaded_ingest: tuple[IngestServer, RoastStateMachine],
    capsys: pytest.CaptureFixture[str],
) -> None:
    server, sm = threaded_ingest
    saved_config(config_path, ingest__port=server.port)
    base = ["--config", str(config_path), "emit"]
    assert cli.main([*base, "ev", "charge", "185.2", "220.1", "-1"]) == 0
    assert cli.main([*base, "ctl", "heat", "70"]) == 0
    assert capsys.readouterr().err == ""
    assert sm.phase == "drying"
    assert sm.snapshot()["events"]["charge"] == {"tm": None, "bt": 185.2}
    assert sm.snapshot()["controls"] == {"heat": 70.0}

    assert cli.main([*base, "ev", "explode", "1", "2", "3"]) == 0
    assert "bridge rejected the message (400" in capsys.readouterr().err


def test_emit_never_fails(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    saved_config(config_path)  # nothing listens on the ingest port
    started = time.monotonic()
    assert cli.main(["--config", str(config_path), "emit", "ev", "drop", "1", "2", "3"]) == 0
    assert time.monotonic() - started < 3
    assert "cannot reach the bridge" in capsys.readouterr().err
    assert cli.main(["emit"]) == 0
    assert "usage:" in capsys.readouterr().err
    assert cli.main(["emit", "--bogus-flag"]) == 0


def test_emit_unexpected_error(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_coro: Any) -> None:
        _coro.close()
        raise RuntimeError("kaput")

    monkeypatch.setattr("asyncio.run", boom)
    assert cli.main(["--config", str(config_path), "emit", "ctl", "fan", "1"]) == 0
    assert "unexpected error: kaput" in capsys.readouterr().err


def test_emit_timeout() -> None:
    async def scenario() -> str | None:
        async def stall(_r: asyncio.StreamReader, _w: asyncio.StreamWriter) -> None:
            await asyncio.sleep(5)

        server = await asyncio.start_server(stall, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await cli.post_emit(port, {"k": "ev", "e": "on"}, limit_s=0.2)
        finally:
            server.close()

    error = asyncio.run(scenario())
    assert error is not None
    assert "cannot reach the bridge" in error


# --- doctor ------------------------------------------------------------------------------


def test_doctor_cli_failure(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--config", str(config_path), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] Config" in out
    assert "1 check(s) failed." in out


# --- replay / run ------------------------------------------------------------------------


def test_replay_cli(config_path: Path, tmp_path: Path) -> None:
    saved_config(config_path)
    args = ["--config", str(config_path), "replay", "--no-discovery", "--exit", "--speed", "5000"]
    assert cli.main([*args, str(FIXTURES / "full_roast.jsonl")]) == 0
    assert cli.main([*args, str(tmp_path / "missing.jsonl")]) == 2
    bad = tmp_path / "bad.jsonl"
    bad.write_text("nope\n")
    assert cli.main([*args, str(bad)]) == 2


def test_replay_cli_rejects_bad_speed(config_path: Path) -> None:
    for speed in ("0", "-2", "fast", "inf"):
        with pytest.raises(SystemExit):
            cli.main(["--config", str(config_path), "replay", "x.jsonl", "--speed", speed])


def test_run_refuses_bad_config(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--config", str(config_path), "run"]) == 2
    saved_config(config_path, unit="K")
    assert cli.main(["--config", str(config_path)]) == 2
    assert "config problem: unit must be C or F" in capsys.readouterr().err
    assert cli.main(["--config", str(config_path), "replay", "x.jsonl"]) == 2


def test_run_until_sigterm(config_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX signals")
    config = saved_config(config_path)
    target = ("127.0.0.1", config.ha.port)

    def stop_when_listening() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(target, timeout=0.2).close()
            except OSError:
                time.sleep(0.05)
                continue
            time.sleep(0.1)  # let the bridge finish starting
            os.kill(os.getpid(), signal.SIGTERM)
            return

    killer = threading.Thread(target=stop_when_listening)
    killer.start()
    try:
        assert cli.main(["--config", str(config_path), "run", "--no-discovery"]) == 0
    finally:
        killer.join()


def test_run_port_in_use(config_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        saved_config(config_path, ha__port=busy.getsockname()[1])
        assert cli.main(["--config", str(config_path), "run", "--no-discovery"]) == 1


def test_run_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(coro: Any) -> None:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr("asyncio.run", interrupted)

    async def nothing() -> None:
        return None

    assert cli._run_until_stopped(nothing()) == 0

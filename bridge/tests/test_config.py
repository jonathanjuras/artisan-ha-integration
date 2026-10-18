"""Config load/save, generation and sanity checks."""

from __future__ import annotations

import logging
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from artisan_ha_bridge.config import (
    Config,
    ConfigError,
    config_from_dict,
    default_config_path,
    load_config,
    save_config,
)
from artisan_ha_bridge.logging_setup import REDACTED, RedactingFilter, setup_logging


def test_generate_round_trip(tmp_path: Path) -> None:
    config = Config.generate()
    uuid.UUID(config.bridge_id)
    assert len(config.ha.token) >= 40
    path = tmp_path / "nested" / "config.yaml"
    save_config(config, path)
    assert load_config(path) == config
    assert config.problems() == []
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = yaml.safe_load(path.read_text())
    assert list(raw) == [
        "bridge_id", "unit", "weblcds", "ingest", "ha",
        "ror_window_s", "offline_after_s", "autosave_dir", "log",
    ]  # fmt: skip
    assert raw["weblcds"] == {"host": "127.0.0.1", "port": 8080, "path": "/websocket"}
    assert raw["ha"]["bind"] == "0.0.0.0"
    assert raw["ha"]["port"] == 8765
    assert raw["ha"]["sample_interval_s"] == 1.0


def test_generate_keeps_given_bridge_id() -> None:
    assert Config.generate("abc").bridge_id == "abc"


def test_token_not_in_repr() -> None:
    config = Config.generate()
    assert config.ha.token not in repr(config)


def test_defaults_filled_and_ints_accepted(tmp_path: Path) -> None:
    config = config_from_dict(
        {
            "bridge_id": str(uuid.uuid4()),
            "ha": {"token": "t" * 32, "sample_interval_s": 2},
            "autosave_dir": str(tmp_path),
            "ror_window_s": 20,
        }
    )
    assert config.ha.sample_interval_s == 2.0
    assert config.ror_window_s == 20.0
    assert config.ha.port == 8765
    assert config.autosave_dir == tmp_path
    assert config.weblcds.url == "ws://127.0.0.1:8080/websocket"
    assert config.problems() == []


def test_default_path() -> None:
    path = default_config_path()
    assert path.name == "config.yaml"
    assert "artisan-ha-bridge" in str(path)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ([], "YAML mapping"),
        ({}, "bridge_id is missing"),
        ({"bridge_id": "x"}, "ha.token is missing"),
        ({"bridge_id": "x", "ha": {"token": ""}}, "ha.token is missing"),
        ({"bridge_id": "x", "ha": "nope"}, "ha must be a mapping"),
        ({"bridge_id": "x", "ha": {"token": "t", "port": "8765"}}, "ha.port must be int"),
        ({"bridge_id": "x", "ha": {"token": "t", "port": True}}, "ha.port must be int"),
        ({"bridge_id": "x", "ha": {"token": "t"}, "unit": 1}, "unit must be str"),
        ({"bridge_id": "x", "ha": {"token": "t"}, "autosave_dir": 3}, "autosave_dir"),
        ({"bridge_id": "x", "ha": {"token": "t"}, "offline_after_s": "x"}, "must be float"),
    ],
)
def test_structural_errors(raw: Any, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        config_from_dict(raw)


def test_null_sections_use_defaults() -> None:
    config = config_from_dict({"bridge_id": "x", "ha": {"token": "t"}, "log": None})
    assert config.log.level == "INFO"


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="run `artisan-ha-bridge init`"):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path)  # a directory


def test_problems(tmp_path: Path) -> None:
    config = Config.generate()
    config.unit = "K"
    config.bridge_id = "not-a-uuid"
    config.ha.token = "short"
    config.ha.port = 70000
    config.ingest.port = 8080
    config.ha.sample_interval_s = 0
    config.ror_window_s = -1
    config.offline_after_s = 0
    config.weblcds.path = "websocket"
    config.log.level = "LOUD"
    config.autosave_dir = tmp_path / "missing"
    problems = config.problems()
    joined = "\n".join(problems)
    for expected in (
        "unit must be C or F",
        "bridge_id is not a UUID",
        "ha.token must be at least",
        "ha.port 70000 is outside",
        "weblcds.port collides",
        "weblcds.path must start",
        "ha.sample_interval_s must be positive",
        "ror_window_s must be positive",
        "offline_after_s must be positive",
        "log.level must be one of",
        "is not a directory",
    ):
        assert expected in joined
    config = Config.generate()
    config.ingest.port = config.ha.port
    assert config.problems() == ["ingest.port and ha.port must differ"]


def test_redacting_filter() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token %s here", ("s3cret",), None)
    assert RedactingFilter(["s3cret", ""]).filter(record)
    assert record.getMessage() == f"token {REDACTED} here"
    clean = logging.LogRecord("x", logging.INFO, __file__, 1, "nothing %d", (1,), None)
    RedactingFilter(["s3cret"]).filter(clean)
    assert clean.args == (1,)


def test_setup_logging(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        setup_logging("debug", secrets=["s3cret"])
        logging.getLogger("test").debug("leak s3cret?")
        assert root.level == logging.DEBUG
        err = capsys.readouterr().err
        assert "leak **REDACTED**?" in err
        assert "s3cret" not in err
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)

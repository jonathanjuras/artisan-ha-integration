"""Bridge configuration (spec 4.2): YAML file under the platform user config directory."""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

import platformdirs
import yaml

APP_NAME: Final = "artisan-ha-bridge"
UNITS: Final = ("C", "F")
LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MIN_TOKEN_LENGTH: Final = 16

_T = TypeVar("_T", str, int, float)


class ConfigError(Exception):
    """The config file is missing or structurally invalid."""


def default_config_path() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False)) / "config.yaml"


@dataclass
class WeblcdsConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/websocket"

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}{self.path}"


@dataclass
class IngestConfig:
    port: int = 8766


@dataclass
class HAConfig:
    bind: str = "0.0.0.0"
    port: int = 8765
    token: str = field(default="", repr=False)  # never in reprs or logs
    sample_interval_s: float = 1.0


@dataclass
class LogConfig:
    level: str = "INFO"


@dataclass
class Config:
    bridge_id: str
    unit: str = "C"
    weblcds: WeblcdsConfig = field(default_factory=WeblcdsConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    ha: HAConfig = field(default_factory=HAConfig)
    ror_window_s: float = 30.0
    offline_after_s: float = 10.0
    autosave_dir: Path | None = None
    log: LogConfig = field(default_factory=LogConfig)

    @classmethod
    def generate(cls, bridge_id: str | None = None) -> Config:
        return cls(
            bridge_id=bridge_id or str(uuid.uuid4()),
            ha=HAConfig(token=secrets.token_urlsafe(32)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_id": self.bridge_id,
            "unit": self.unit,
            "weblcds": {
                "host": self.weblcds.host,
                "port": self.weblcds.port,
                "path": self.weblcds.path,
            },
            "ingest": {"port": self.ingest.port},
            "ha": {
                "bind": self.ha.bind,
                "port": self.ha.port,
                "token": self.ha.token,
                "sample_interval_s": self.ha.sample_interval_s,
            },
            "ror_window_s": self.ror_window_s,
            "offline_after_s": self.offline_after_s,
            "autosave_dir": None if self.autosave_dir is None else str(self.autosave_dir),
            "log": {"level": self.log.level},
        }

    def problems(self) -> list[str]:
        """Sanity issues that would make the bridge misbehave. Empty when the config is usable."""
        issues: list[str] = []
        if self.unit not in UNITS:
            issues.append(f"unit must be C or F, not {self.unit!r}")
        try:
            uuid.UUID(self.bridge_id)
        except ValueError:
            issues.append("bridge_id is not a UUID")
        if len(self.ha.token) < MIN_TOKEN_LENGTH:
            issues.append(f"ha.token must be at least {MIN_TOKEN_LENGTH} characters")
        for name, port in (
            ("weblcds.port", self.weblcds.port),
            ("ingest.port", self.ingest.port),
            ("ha.port", self.ha.port),
        ):
            if not 1 <= port <= 65535:
                issues.append(f"{name} {port} is outside 1-65535")
        if self.ingest.port == self.ha.port:
            issues.append("ingest.port and ha.port must differ")
        if self.weblcds.port in (self.ingest.port, self.ha.port) and self.weblcds.host in (
            "127.0.0.1",
            "localhost",
        ):
            issues.append("weblcds.port collides with a port the bridge listens on")
        if not self.weblcds.path.startswith("/"):
            issues.append("weblcds.path must start with '/'")
        for name, value in (
            ("ha.sample_interval_s", self.ha.sample_interval_s),
            ("ror_window_s", self.ror_window_s),
            ("offline_after_s", self.offline_after_s),
        ):
            if value <= 0:
                issues.append(f"{name} must be positive")
        if self.log.level.upper() not in LOG_LEVELS:
            issues.append(f"log.level must be one of {', '.join(LOG_LEVELS)}")
        if self.autosave_dir is not None and not self.autosave_dir.is_dir():
            issues.append(f"autosave_dir {self.autosave_dir} is not a directory")
        return issues


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _get(section: dict[str, Any], key: str, kind: type[_T], default: _T, where: str) -> _T:
    value: object = section.get(key, default)
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, kind):
        raise ConfigError(f"{where}{key} must be {kind.__name__}")
    return value


def config_from_dict(raw: object) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a YAML mapping")
    bridge_id = raw.get("bridge_id")
    if not isinstance(bridge_id, str) or not bridge_id:
        raise ConfigError("bridge_id is missing; run `artisan-ha-bridge init`")
    weblcds, ingest, ha, log = (_section(raw, key) for key in ("weblcds", "ingest", "ha", "log"))
    token = ha.get("token")
    if not isinstance(token, str) or not token:
        raise ConfigError("ha.token is missing; run `artisan-ha-bridge init`")
    autosave = raw.get("autosave_dir")
    if autosave is not None and not isinstance(autosave, str):
        raise ConfigError("autosave_dir must be a path or null")
    defaults = Config(bridge_id=bridge_id)
    return Config(
        bridge_id=bridge_id,
        unit=_get(raw, "unit", str, defaults.unit, ""),
        weblcds=WeblcdsConfig(
            host=_get(weblcds, "host", str, defaults.weblcds.host, "weblcds."),
            port=_get(weblcds, "port", int, defaults.weblcds.port, "weblcds."),
            path=_get(weblcds, "path", str, defaults.weblcds.path, "weblcds."),
        ),
        ingest=IngestConfig(port=_get(ingest, "port", int, defaults.ingest.port, "ingest.")),
        ha=HAConfig(
            bind=_get(ha, "bind", str, defaults.ha.bind, "ha."),
            port=_get(ha, "port", int, defaults.ha.port, "ha."),
            token=token,
            sample_interval_s=_get(
                ha, "sample_interval_s", float, defaults.ha.sample_interval_s, "ha."
            ),
        ),
        ror_window_s=_get(raw, "ror_window_s", float, defaults.ror_window_s, ""),
        offline_after_s=_get(raw, "offline_after_s", float, defaults.offline_after_s, ""),
        autosave_dir=None if autosave is None else Path(autosave).expanduser(),
        log=LogConfig(level=_get(log, "level", str, defaults.log.level, "log.")),
    )


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"no config at {path}; run `artisan-ha-bridge init`") from None
    except OSError as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"{path} is not valid YAML: {err}") from err
    return config_from_dict(raw)


def save_config(config: Config, path: Path) -> None:
    """Write the config readable by the owner only, since it holds the token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(config.to_dict(), sort_keys=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        path.chmod(0o600)  # O_CREAT's mode does not apply to an existing file
    except OSError:  # pragma: no cover - e.g. filesystems without POSIX permissions
        logging.getLogger(__name__).debug("Could not restrict permissions on %s", path)

"""Logging configuration with a last-line-of-defense secret redactor."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

REDACTED = "**REDACTED**"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RedactingFilter(logging.Filter):
    """Replaces secrets in formatted log messages. The code never logs the token on purpose;
    this guards against it slipping in through a library or an exception message."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, REDACTED)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def setup_logging(level: str, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    # aiohttp's access log is noise for a WebSocket-only service.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

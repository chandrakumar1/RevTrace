"""Logging configuration.

JSON lines in non-development environments, human-readable in development.
All timestamps are UTC and ISO-8601 — RevTrace reconstructs timelines across
delayed and out-of-order events, so local time is never acceptable.

Structured extras are passed through `redact()` so a payload logged for
debugging can never carry a secret into the log stream.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.security import redact

#: LogRecord attributes that are not caller-supplied "extra" fields.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "stacklevel",
        "thread",
        "threadName",
        "taskName",
    }
)


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="milliseconds")


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with secrets redacted."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        extras = _extras(record)
        if extras:
            payload["context"] = redact(extras)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    """Readable single-line output for local development."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{_utc_iso(record.created)} {record.levelname:<8} {record.name}: {record.getMessage()}"
        )

        extras = _extras(record)
        if extras:
            base += f" | {json.dumps(redact(extras), default=str)}"

        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)

        return base


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Install RevTrace's root logging configuration. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())
    root.addHandler(handler)

    # SQLAlchemy echo is controlled by db_echo, not by the root level.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

"""JSON log formatter — ADR-029 compliant.

Produces one JSON object per line with required fields:
  timestamp, level, logger, message
Optional field injected by RequestIdFilter: request_id
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        # Let the base class handle exc_info / stack_info formatting into record.message
        record.message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)

        obj: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3]
            + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }

        # Inject request_id when the RequestIdFilter has set it.
        request_id = getattr(record, "request_id", None)
        if request_id:
            obj["request_id"] = request_id

        # Include exception text if present.
        if record.exc_text:
            obj["exc_info"] = record.exc_text

        # Include any extra structured fields attached by the caller
        # (i.e. fields passed as extra={...} to logger.info()).
        _skip = {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "message",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "exc_info",
            "exc_text",
            "request_id",
        }
        for key, val in record.__dict__.items():
            if key not in _skip and not key.startswith("_"):
                obj[key] = val

        return json.dumps(obj, default=str)

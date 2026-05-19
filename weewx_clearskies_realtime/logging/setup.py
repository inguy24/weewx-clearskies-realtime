"""Logging bootstrap — ADR-029.

Called twice at startup:
  1. setup_logging("INFO")   — immediately after process start (bootstrap level)
  2. setup_logging(level)    — after config is loaded (operator-configured level)

The second call replaces the first.  Uvicorn's own loggers are reconfigured to
propagate to the root so they use the same JSON formatter.
"""

from __future__ import annotations

import logging
import sys

from weewx_clearskies_realtime.logging.json_formatter import JsonFormatter
from weewx_clearskies_realtime.logging.redaction_filter import RedactionFilter, RequestIdFilter

# Uvicorn logger names that we re-wire to propagate through the root handler.
_UVICORN_LOGGERS = (
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
)


def setup_logging(level: str) -> None:
    """Configure root logger with JSON-to-stdout output.

    Clears any existing handlers, installs a single StreamHandler(sys.stdout)
    with JsonFormatter, and attaches RedactionFilter + RequestIdFilter at root.

    Uvicorn loggers are reconfigured to propagate so they share the same handler.
    """
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(numeric_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    # Filters run on the root handler so every propagated record goes through them.
    # Clear first to avoid stacking on the second call.
    root.filters.clear()
    root.addFilter(RedactionFilter())
    root.addFilter(RequestIdFilter())

    # Re-wire uvicorn loggers.
    for name in _UVICORN_LOGGERS:
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
        uv_logger.setLevel(numeric_level)

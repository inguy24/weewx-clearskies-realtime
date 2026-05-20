"""Redaction and request-ID injection filters — ADR-029.

RedactionFilter: regex-replaces patterns that look like secrets in log messages
and in extra={} structured fields attached to log records.
RequestIdFilter: injects request_id from a ContextVar into every log record.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from typing import Any

# ContextVar populated by SSE middleware / ASGI request hooks.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Patterns that suggest a credential value in a log message.
# Each pattern is applied to the rendered message string.
_REDACT_PATTERNS: list[re.Pattern[str]] = [
    # Authorization: Bearer <token>
    re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE),
    # Authorization: Basic <base64>
    re.compile(r"(Authorization:\s*Basic\s+)\S+", re.IGNORECASE),
    # password=<value> or password: <value> in URLs / query strings
    re.compile(r"(password[=:]\s*)\S+", re.IGNORECASE),
    # api_key= / apikey=
    re.compile(r"(api[-_]?key[=:]\s*)\S+", re.IGNORECASE),
    # token= / access_token= / auth_token=
    re.compile(r"([a-z_]*token[=:]\s*)\S+", re.IGNORECASE),
]

# Standard LogRecord attributes that must not be treated as extra fields.
# Mirrors the skip-set used by JsonFormatter so redaction covers the same surface.
_STANDARD_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "created", "filename", "funcName", "levelname",
        "levelno", "lineno", "module", "msecs", "message", "pathname",
        "process", "processName", "relativeCreated", "stack_info", "thread",
        "threadName", "exc_info", "exc_text", "request_id",
        # Python 3.12+ adds taskName; include for forward-compat.
        "taskName",
    }
)


class RedactionFilter(logging.Filter):
    """Strip credential-like substrings from log records before emission.

    Covers three surfaces on each LogRecord:
    - record.msg  — the message template string.
    - record.args — format arguments (tuple or dict).
    - Extra structured fields set via extra={} — may themselves be dicts;
      _redact recurses into nested dicts.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the message template.
        record.msg = _redact(record.msg)

        # Redact format arguments.
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _redact(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_redact(a) for a in record.args)

        # Redact extra structured fields (those passed via extra={} to the
        # logger call).  JsonFormatter emits these directly, so they must be
        # cleaned here before the record reaches any handler.
        for key, val in list(record.__dict__.items()):
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                redacted = _redact(val)
                if redacted is not val:
                    setattr(record, key, redacted)

        return True


class RequestIdFilter(logging.Filter):
    """Attach the current request_id ContextVar value to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()  # type: ignore[attr-defined]
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact(value: Any) -> Any:  # noqa: ANN401
    """Apply redaction patterns to a value.

    - str: apply all credential patterns, return cleaned string.
    - dict: return a new dict with _redact applied recursively to each value.
    - list: return a new list with _redact applied to each element.
    - anything else: return unchanged.
    """
    if isinstance(value, str):
        for pattern in _REDACT_PATTERNS:
            value = pattern.sub(r"\1[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value

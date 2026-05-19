"""Redaction and request-ID injection filters — ADR-029.

RedactionFilter: regex-replaces patterns that look like secrets in log messages.
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


class RedactionFilter(logging.Filter):
    """Strip credential-like substrings from log records before emission."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _redact(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_redact(a) for a in record.args)
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
    """Apply all redaction patterns to a string value; return non-strings unchanged."""
    if not isinstance(value, str):
        return value
    for pattern in _REDACT_PATTERNS:
        value = pattern.sub(r"\1[REDACTED]", value)
    return value

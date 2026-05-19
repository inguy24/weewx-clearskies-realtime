"""Tests for logging — JsonFormatter, RedactionFilter, RequestIdFilter."""

from __future__ import annotations

import json
import logging

from weewx_clearskies_realtime.logging.json_formatter import JsonFormatter
from weewx_clearskies_realtime.logging.redaction_filter import (
    RedactionFilter,
    RequestIdFilter,
    _redact,
    request_id_var,
)

# ---------------------------------------------------------------------------
# JsonFormatter
# ---------------------------------------------------------------------------


def _make_record(
    msg: str = "hello",
    level: int = logging.INFO,
    name: str = "test.logger",
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    return record


def test_json_formatter_required_fields() -> None:
    fmt = JsonFormatter()
    record = _make_record("test message")
    output = fmt.format(record)
    obj = json.loads(output)
    assert "timestamp" in obj
    assert "level" in obj
    assert "logger" in obj
    assert "message" in obj


def test_json_formatter_timestamp_format() -> None:
    """Timestamp must be ISO 8601 UTC with Z suffix."""
    fmt = JsonFormatter()
    obj = json.loads(fmt.format(_make_record()))
    ts = obj["timestamp"]
    assert ts.endswith("Z")
    assert "T" in ts


def test_json_formatter_level_name() -> None:
    fmt = JsonFormatter()
    obj = json.loads(fmt.format(_make_record(level=logging.WARNING)))
    assert obj["level"] == "WARNING"


def test_json_formatter_extra_fields() -> None:
    """Extra fields passed as extra={} appear in the JSON output."""
    fmt = JsonFormatter()
    record = _make_record()
    record.broker_host = "mqtt.local"  # type: ignore[attr-defined]
    obj = json.loads(fmt.format(record))
    assert obj.get("broker_host") == "mqtt.local"


def test_json_formatter_request_id_included() -> None:
    fmt = JsonFormatter()
    record = _make_record()
    record.request_id = "abc-123"  # type: ignore[attr-defined]
    obj = json.loads(fmt.format(record))
    assert obj["request_id"] == "abc-123"


def test_json_formatter_no_request_id_when_none() -> None:
    fmt = JsonFormatter()
    record = _make_record()
    obj = json.loads(fmt.format(record))
    assert "request_id" not in obj


def test_json_formatter_exception_included() -> None:
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname="", lineno=0,
            msg="err", args=(), exc_info=sys.exc_info(),
        )
    obj = json.loads(fmt.format(record))
    assert "exc_info" in obj
    assert "ValueError" in obj["exc_info"]


def test_json_formatter_single_line() -> None:
    """Each formatted record must be a single line (no embedded newlines)."""
    fmt = JsonFormatter()
    record = _make_record("line one\nline two")
    output = fmt.format(record)
    assert "\n" not in output


# ---------------------------------------------------------------------------
# RedactionFilter
# ---------------------------------------------------------------------------


def test_redact_authorization_bearer() -> None:
    result = _redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "[REDACTED]" in result
    assert "eyJ" not in result


def test_redact_authorization_basic() -> None:
    result = _redact("Authorization: Basic dXNlcjpwYXNz")
    assert "[REDACTED]" in result


def test_redact_password_equals() -> None:
    result = _redact("password=hunter2")
    assert "[REDACTED]" in result
    assert "hunter2" not in result


def test_redact_token_equals() -> None:
    result = _redact("access_token=abc123")
    assert "[REDACTED]" in result


def test_redact_non_string_passthrough() -> None:
    assert _redact(42) == 42
    assert _redact(None) is None


def test_redaction_filter_on_record() -> None:
    filt = RedactionFilter()
    record = _make_record("Authorization: Bearer secret-token")
    filt.filter(record)
    assert "secret-token" not in record.msg
    assert "[REDACTED]" in record.msg


def test_redaction_filter_dict_args() -> None:
    filt = RedactionFilter()
    record = _make_record("url: %(url)s")
    record.args = {"url": "https://api.example.com?api_key=deadbeef"}  # type: ignore[assignment]
    filt.filter(record)
    args = record.args
    assert isinstance(args, dict)
    assert "[REDACTED]" in args["url"]


# ---------------------------------------------------------------------------
# RequestIdFilter
# ---------------------------------------------------------------------------


def test_request_id_filter_injects_from_contextvar() -> None:
    token = request_id_var.set("req-xyz")
    filt = RequestIdFilter()
    record = _make_record()
    filt.filter(record)
    assert record.request_id == "req-xyz"  # type: ignore[attr-defined]
    request_id_var.reset(token)


def test_request_id_filter_none_when_not_set() -> None:
    token = request_id_var.set(None)
    filt = RequestIdFilter()
    record = _make_record()
    filt.filter(record)
    assert record.request_id is None  # type: ignore[attr-defined]
    request_id_var.reset(token)

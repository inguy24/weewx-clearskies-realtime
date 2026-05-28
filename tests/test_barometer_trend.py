"""Acceptance tests for barometer_trend enrichment — AC 2.1 through 2.11.

Tests map directly to the acceptance criteria in the task brief (round 2,
2026-05-28).  AC 2.5–2.8 cover dashboard display only and are excluded per
brief (they test the dashboard, not this module).

The enrichment function imports ``get_upstream_client`` at call time inside
the ``try`` block, so patching ``weewx_clearskies_realtime.proxy.get_upstream_client``
intercepts every invocation.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from weewx_clearskies_realtime.enrichment.barometer_trend import (
    TREND_TIME_DELTA,
    TREND_TIME_GRACE,
    _epoch_to_iso,
    enrich_barometer_trend,
)

# ---------------------------------------------------------------------------
# Timestamps used across tests.
# Using a fixed "current" timestamp makes grace-window arithmetic explicit.
# ---------------------------------------------------------------------------

_TS_CURRENT: int = 1700000000  # arbitrary fixed epoch second
_TS_HISTORICAL_EXACT: int = _TS_CURRENT - TREND_TIME_DELTA  # exactly 3 h ago


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _current_envelope(barometer: float | None = 30.10, date_time: int = _TS_CURRENT) -> dict[str, Any]:
    """Minimal /current response envelope.

    The observation carries its time as ``timestamp`` (UTC ISO-8601), matching
    the real API contract — there is no epoch ``dateTime`` field on the wire.
    """
    return {
        "data": {
            "barometer": barometer,
            "timestamp": _epoch_to_iso(date_time),
        },
    }


def _archive_response(barometer: float = 30.05, date_time: int = _TS_HISTORICAL_EXACT) -> dict[str, Any]:
    """Minimal /archive response envelope with a single record.

    Matches the real API contract: records live under the ``data`` key and each
    record's time is an ISO-8601 ``timestamp`` (not an epoch ``dateTime``).
    """
    return {
        "data": [
            {"barometer": barometer, "timestamp": _epoch_to_iso(date_time)},
        ],
    }


def _make_mock_client(archive_json: dict[str, Any], status_code: int = 200) -> MagicMock:
    """Return a mock httpx.AsyncClient whose .get() returns the given JSON."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = archive_json

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


_UPSTREAM_URL = "http://upstream-api:8765"


# ---------------------------------------------------------------------------
# AC 2.1 — Rising pressure: archive lower than current → positive trend
# ---------------------------------------------------------------------------


async def test_barometer_trend_rising_pressure() -> None:
    """AC 2.1: archive 30.05 → current 30.10 yields barometerTrend = +0.05."""
    data = _current_envelope(barometer=30.10)
    archive = _archive_response(barometer=30.05, date_time=_TS_HISTORICAL_EXACT)
    mock_client = _make_mock_client(archive)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(mock_client, _UPSTREAM_URL),
    ):
        result = await enrich_barometer_trend(data)

    assert result["barometerTrend"] == pytest.approx(0.05, abs=1e-9)


# ---------------------------------------------------------------------------
# AC 2.2 — Falling pressure: archive higher than current → negative trend
# ---------------------------------------------------------------------------


async def test_barometer_trend_falling_pressure() -> None:
    """AC 2.2: archive 30.10 → current 30.05 yields barometerTrend = -0.05."""
    data = _current_envelope(barometer=30.05)
    archive = _archive_response(barometer=30.10, date_time=_TS_HISTORICAL_EXACT)
    mock_client = _make_mock_client(archive)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(mock_client, _UPSTREAM_URL),
    ):
        result = await enrich_barometer_trend(data)

    assert result["barometerTrend"] == pytest.approx(-0.05, abs=1e-9)


# ---------------------------------------------------------------------------
# AC 2.3 — No archive record → trend null (empty records list)
# ---------------------------------------------------------------------------


async def test_barometer_trend_null_when_no_archive_records() -> None:
    """AC 2.3: empty records list from archive yields barometerTrend = None."""
    data = _current_envelope(barometer=30.10)
    archive = {"data": []}  # archive returned nothing
    mock_client = _make_mock_client(archive, status_code=200)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(mock_client, _UPSTREAM_URL),
    ):
        result = await enrich_barometer_trend(data)

    assert result["barometerTrend"] is None


async def test_barometer_trend_null_when_archive_returns_404() -> None:
    """AC 2.3 (404 variant): 404 from archive endpoint yields barometerTrend = None."""
    data = _current_envelope(barometer=30.10)
    mock_client = _make_mock_client({}, status_code=404)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(mock_client, _UPSTREAM_URL),
    ):
        result = await enrich_barometer_trend(data)

    assert result["barometerTrend"] is None


# ---------------------------------------------------------------------------
# AC 2.4 — Current barometer null → trend null
# ---------------------------------------------------------------------------


async def test_barometer_trend_null_when_current_barometer_missing() -> None:
    """AC 2.4: current barometer = None yields barometerTrend = None without
    querying the archive.
    """
    data = _current_envelope(barometer=None)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
    ) as mock_get_client:
        result = await enrich_barometer_trend(data)

    # Must not have called the upstream at all.
    mock_get_client.assert_not_called()
    assert result["barometerTrend"] is None


# ---------------------------------------------------------------------------
# AC 2.9 — Constants and grace-window enforcement
# ---------------------------------------------------------------------------


def test_trend_time_delta_constant() -> None:
    """AC 2.9: TREND_TIME_DELTA equals 10800 (3 hours in seconds)."""
    assert TREND_TIME_DELTA == 10800


def test_trend_time_grace_constant() -> None:
    """AC 2.9: TREND_TIME_GRACE equals 300 (5 minutes in seconds)."""
    assert TREND_TIME_GRACE == 300


async def test_barometer_trend_null_when_archive_record_outside_grace_window() -> None:
    """AC 2.9: archive record whose timestamp is more than TREND_TIME_GRACE seconds
    away from the target historical timestamp causes barometerTrend = None.

    Target historical ts = _TS_CURRENT - TREND_TIME_DELTA.
    Record ts = target + TREND_TIME_GRACE + 1 → just outside the window.
    """
    data = _current_envelope(barometer=30.10)
    outside_ts = _TS_HISTORICAL_EXACT + TREND_TIME_GRACE + 1  # one second past grace
    archive = _archive_response(barometer=30.05, date_time=outside_ts)
    mock_client = _make_mock_client(archive)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(mock_client, _UPSTREAM_URL),
    ):
        result = await enrich_barometer_trend(data)

    assert result["barometerTrend"] is None


async def test_barometer_trend_not_null_when_archive_record_within_grace_window() -> None:
    """AC 2.9 (boundary): archive record exactly at the grace-window edge is accepted."""
    data = _current_envelope(barometer=30.10)
    edge_ts = _TS_HISTORICAL_EXACT + TREND_TIME_GRACE  # exactly at the boundary (inclusive)
    archive = _archive_response(barometer=30.05, date_time=edge_ts)
    mock_client = _make_mock_client(archive)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(mock_client, _UPSTREAM_URL),
    ):
        result = await enrich_barometer_trend(data)

    # Exactly at the boundary: abs(edge_ts - ts_historical) == TREND_TIME_GRACE,
    # which is NOT > TREND_TIME_GRACE, so the record is accepted.
    assert result["barometerTrend"] is not None
    assert result["barometerTrend"] == pytest.approx(0.05, abs=1e-9)


# ---------------------------------------------------------------------------
# AC 2.10 — Enrichment failure does not raise; returns barometerTrend: None
# ---------------------------------------------------------------------------


async def test_barometer_trend_returns_none_on_upstream_exception() -> None:
    """AC 2.10: when get_upstream_client() raises, the function returns
    barometerTrend = None and does NOT propagate the exception.
    """
    import httpx

    data = _current_envelope(barometer=30.10)

    # Simulate a network-level failure.
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(mock_client, _UPSTREAM_URL),
    ):
        result = await enrich_barometer_trend(data)  # must not raise

    assert result["barometerTrend"] is None
    # Original observation data must still be present in the returned dict.
    assert result["data"]["barometer"] == 30.10


async def test_barometer_trend_returns_none_when_client_is_none() -> None:
    """AC 2.10 (no-client variant): get_upstream_client() returning (None, '')
    yields barometerTrend = None without raising.
    """
    data = _current_envelope(barometer=30.10)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(None, ""),
    ):
        result = await enrich_barometer_trend(data)

    assert result["barometerTrend"] is None


# ---------------------------------------------------------------------------
# AC 2.11 — Added latency < 200 ms (mocked upstream, measures enrichment overhead)
# ---------------------------------------------------------------------------


async def test_barometer_trend_latency_under_200ms() -> None:
    """AC 2.11: enrich_barometer_trend() with an instant mock archive response
    completes in under 200 ms, confirming the enrichment itself adds no
    meaningful latency beyond the upstream round-trip.
    """
    data = _current_envelope(barometer=30.10)
    archive = _archive_response(barometer=30.05, date_time=_TS_HISTORICAL_EXACT)
    mock_client = _make_mock_client(archive)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(mock_client, _UPSTREAM_URL),
    ):
        t0 = time.perf_counter()
        await enrich_barometer_trend(data)
        elapsed_ms = (time.perf_counter() - t0) * 1000

    assert elapsed_ms < 200, (
        f"enrich_barometer_trend() took {elapsed_ms:.1f} ms — expected < 200 ms. "
        "Enrichment overhead (excluding upstream I/O) should be negligible."
    )


# ---------------------------------------------------------------------------
# Archive query contract — ISO-8601 from/to window + correct field names
# ---------------------------------------------------------------------------


async def test_barometer_trend_queries_archive_with_iso_window() -> None:
    """The archive query must bound a ±grace ISO-8601 window around the 3h-ago
    target and request the ``timestamp`` field (not ``dateTime``).

    Regression guard: a bare ``to=<target>&limit=1`` returns the OLDEST record
    at or before the target (ascending order), which the grace check then
    rejects → always-null. The fix bounds from/to around the target.
    """
    data = _current_envelope(barometer=30.10)
    archive = _archive_response(barometer=30.05, date_time=_TS_HISTORICAL_EXACT)
    mock_client = _make_mock_client(archive)

    with patch(
        "weewx_clearskies_realtime.proxy.get_upstream_client",
        return_value=(mock_client, _UPSTREAM_URL),
    ):
        await enrich_barometer_trend(data)

    params = mock_client.get.call_args.kwargs["params"]
    assert params["fields"] == "barometer,timestamp"
    assert params["limit"] == 1
    assert params["from"] == _epoch_to_iso(_TS_HISTORICAL_EXACT - TREND_TIME_GRACE)
    assert params["to"] == _epoch_to_iso(_TS_HISTORICAL_EXACT + TREND_TIME_GRACE)

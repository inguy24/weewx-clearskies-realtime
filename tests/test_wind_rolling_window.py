"""Tests for enrichment/wind_rolling_window.py — T3a.5 acceptance criteria.

Test cases:
  1. Coverage guard    — <60 s of data → get_wind_avg() / get_gust_max() → None
  2. Mean correctness  — known windSpeed values over >60 s → correct arithmetic mean
  3. Max-gust          — known windGust values over >60 s → correct maximum
  4. Eviction          — data spanning >600 s → old entries removed, recent only
  5. None/non-numeric skipped — None, "calm", missing fields → no crash, ignored
  6. Dict value extraction — windSpeed as {value: 12.5, …} → extracts 12.5
  7. reset() isolation — feed data, reset, buffers empty
  8. Packet not mutated — process_packet must not modify the packet dict

All tests call wind_rolling_window.reset() before use to ensure module-level
buffer state does not leak between tests.
"""

from __future__ import annotations

import time

import pytest

from weewx_clearskies_realtime.enrichment import wind_rolling_window
from weewx_clearskies_realtime.enrichment.wind_rolling_window import (  # noqa: I001
    MIN_COVERAGE_SECONDS,
    WINDOW_SECONDS,
    TimeWindowedBuffer,
    get_gust_max,
    get_wind_avg,
    process_packet,
    reset,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _make_packet(
    wind_speed: object = None,
    wind_gust: object = None,
    date_time: float | None = None,
) -> dict:  # type: ignore[type-arg]
    """Build a minimal loop packet."""
    pkt: dict = {}  # type: ignore[type-arg]
    if wind_speed is not None:
        pkt["windSpeed"] = wind_speed
    if wind_gust is not None:
        pkt["windGust"] = wind_gust
    if date_time is not None:
        pkt["dateTime"] = date_time
    return pkt


# ---------------------------------------------------------------------------
# 1. Coverage guard
# ---------------------------------------------------------------------------


def test_coverage_guard_returns_none_before_min_coverage() -> None:
    """Feed <60 s of data → get_wind_avg() and get_gust_max() both return None.

    Two packets 30 seconds apart give a span of 30 s, below MIN_COVERAGE_SECONDS.
    """
    reset()
    t0 = _now()
    process_packet(_make_packet(wind_speed=10.0, wind_gust=15.0, date_time=t0))
    process_packet(_make_packet(wind_speed=12.0, wind_gust=18.0, date_time=t0 + 30))

    assert get_wind_avg() is None
    assert get_gust_max() is None


def test_coverage_guard_returns_value_after_min_coverage() -> None:
    """Span just exceeding MIN_COVERAGE_SECONDS → values returned (not None)."""
    reset()
    t0 = _now()
    process_packet(_make_packet(wind_speed=10.0, wind_gust=15.0, date_time=t0))
    process_packet(_make_packet(wind_speed=12.0, wind_gust=18.0,
                                date_time=t0 + MIN_COVERAGE_SECONDS + 1))

    assert get_wind_avg() is not None
    assert get_gust_max() is not None


# ---------------------------------------------------------------------------
# 2. Mean correctness
# ---------------------------------------------------------------------------


def test_mean_correctness() -> None:
    """Known windSpeed values over >60 s → arithmetic mean is correct.

    Feed five packets spread over 65 seconds with values [5, 10, 15, 20, 25].
    Expected mean = (5+10+15+20+25) / 5 = 15.0.
    """
    reset()
    t0 = _now()
    values = [5.0, 10.0, 15.0, 20.0, 25.0]
    for i, v in enumerate(values):
        process_packet(_make_packet(wind_speed=v, date_time=t0 + i * 16.25))
    # span = 4 * 16.25 = 65 s > MIN_COVERAGE_SECONDS

    result = get_wind_avg()
    assert result is not None
    assert result == pytest.approx(15.0, rel=1e-9)


def test_mean_single_value_not_emitted_before_coverage() -> None:
    """A single data point has span=0 → below MIN_COVERAGE_SECONDS → None."""
    reset()
    process_packet(_make_packet(wind_speed=8.0, date_time=_now()))
    assert get_wind_avg() is None


# ---------------------------------------------------------------------------
# 3. Max-gust correctness
# ---------------------------------------------------------------------------


def test_gust_max_correctness() -> None:
    """Known windGust values over >60 s → max is the maximum value.

    Feed values [2, 7, 3, 9, 1] with the 9 somewhere in the middle.
    Expected max = 9.0.
    """
    reset()
    t0 = _now()
    gusts = [2.0, 7.0, 3.0, 9.0, 1.0]
    for i, g in enumerate(gusts):
        process_packet(_make_packet(wind_gust=g, date_time=t0 + i * 20.0))
    # span = 4 * 20 = 80 s > MIN_COVERAGE_SECONDS

    result = get_gust_max()
    assert result is not None
    assert result == pytest.approx(9.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 4. Eviction
# ---------------------------------------------------------------------------


def test_eviction_removes_old_entries() -> None:
    """Data spanning >600 s: entries older than WINDOW_SECONDS are evicted.

    Feed an 'old' packet timestamped WINDOW_SECONDS + 10 s ago, then a cluster
    of 'recent' packets over the last 90 s.  After process_packet runs evict(),
    only recent entries remain.
    """
    reset()
    now = _now()

    # Old packet — will be evicted.
    old_ts = now - WINDOW_SECONDS - 10
    process_packet(_make_packet(wind_speed=999.0, wind_gust=999.0, date_time=old_ts))

    # Recent packets spanning 90 s.
    recent_values = [5.0, 10.0, 15.0]
    recent_gusts  = [8.0, 12.0, 6.0]
    for i, (s, g) in enumerate(zip(recent_values, recent_gusts, strict=True)):
        ts = now - 90 + i * 45
        process_packet(_make_packet(wind_speed=s, wind_gust=g, date_time=ts))

    avg = get_wind_avg()
    gust = get_gust_max()

    # The old 999.0 entries must have been evicted.
    assert avg is not None
    assert avg == pytest.approx(sum(recent_values) / len(recent_values), rel=1e-6)
    assert gust is not None
    assert gust == pytest.approx(max(recent_gusts), rel=1e-6)


# ---------------------------------------------------------------------------
# 5. None / non-numeric values skipped
# ---------------------------------------------------------------------------


def test_none_value_skipped() -> None:
    """Packet with windSpeed=None → not added to the buffer; no crash."""
    reset()
    # Only None values — buffer stays empty, no exception.
    process_packet(_make_packet(wind_speed=None, wind_gust=None))
    assert get_wind_avg() is None
    assert get_gust_max() is None


def test_string_calm_skipped() -> None:
    """Packet with windSpeed='calm' → not added to the buffer; no crash."""
    reset()
    process_packet({"windSpeed": "calm", "windGust": "calm"})
    assert get_wind_avg() is None
    assert get_gust_max() is None


def test_missing_wind_fields_no_crash() -> None:
    """Packet with no wind fields at all → no crash, buffers stay empty."""
    reset()
    process_packet({"outTemp": 72.0, "outHumidity": 55.0})
    assert get_wind_avg() is None
    assert get_gust_max() is None


def test_mixed_valid_and_none_packets() -> None:
    """None values are skipped; valid values accumulate normally.

    Feed alternating None and 10.0 packets over >60 s.
    Mean of the valid values is 10.0.
    """
    reset()
    t0 = _now()
    # 5 valid + 2 None packets over 80 s.
    schedule = [
        (t0 + 0,   10.0),
        (t0 + 16,  None),
        (t0 + 32,  10.0),
        (t0 + 48,  None),
        (t0 + 64,  10.0),
        (t0 + 80,  10.0),
    ]
    for ts, v in schedule:
        process_packet(_make_packet(wind_speed=v, date_time=ts))

    avg = get_wind_avg()
    assert avg is not None
    assert avg == pytest.approx(10.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 6. Dict value extraction
# ---------------------------------------------------------------------------


def test_dict_value_extraction() -> None:
    """windSpeed as {value: 12.5, label: ' mph', formatted: '13 mph'} → extracts 12.5."""
    reset()
    t0 = _now()
    converted_value = {"value": 12.5, "label": " mph", "formatted": "13 mph"}
    process_packet(_make_packet(wind_speed=converted_value, date_time=t0))
    process_packet(_make_packet(wind_speed=converted_value,
                                date_time=t0 + MIN_COVERAGE_SECONDS + 1))

    avg = get_wind_avg()
    assert avg is not None
    assert avg == pytest.approx(12.5, rel=1e-9)


def test_dict_with_none_value_skipped() -> None:
    """Dict with value=None → treated as missing; no crash."""
    reset()
    process_packet({"windSpeed": {"value": None, "label": " mph", "formatted": "N/A"}})
    assert get_wind_avg() is None


# ---------------------------------------------------------------------------
# 7. reset() isolation
# ---------------------------------------------------------------------------


def test_reset_clears_buffers() -> None:
    """Feed data, call reset(), verify buffers are empty (get_wind_avg → None)."""
    reset()
    t0 = _now()
    for i in range(5):
        process_packet(_make_packet(wind_speed=10.0, wind_gust=12.0,
                                    date_time=t0 + i * 20))

    # Verify something was buffered.
    wind_rolling_window._speed_buffer.add(t0, 10.0)  # ensure non-empty
    wind_rolling_window._gust_buffer.add(t0, 12.0)

    reset()

    # After reset, both buffers must be empty.
    assert wind_rolling_window._speed_buffer.mean() is None
    assert wind_rolling_window._gust_buffer.mean() is None
    assert get_wind_avg() is None
    assert get_gust_max() is None


# ---------------------------------------------------------------------------
# 8. Packet not mutated
# ---------------------------------------------------------------------------


def test_packet_not_mutated() -> None:
    """process_packet must not modify the packet dict in any way."""
    reset()
    original = {
        "windSpeed": 15.0,
        "windGust":  20.0,
        "dateTime":  _now(),
        "outTemp":   72.0,
    }
    snapshot = dict(original)

    process_packet(original)

    assert original == snapshot, (
        f"process_packet mutated the packet.\n"
        f"Before: {snapshot}\n"
        f"After:  {original}"
    )


# ---------------------------------------------------------------------------
# TimeWindowedBuffer unit tests
# ---------------------------------------------------------------------------


def test_buffer_span_empty() -> None:
    """span() returns 0.0 when the buffer has fewer than two entries."""
    buf = TimeWindowedBuffer()
    assert buf.span() == 0.0
    buf.add(1000.0, 5.0)
    assert buf.span() == 0.0


def test_buffer_span_two_entries() -> None:
    """span() = newest_ts - oldest_ts when two or more entries exist."""
    buf = TimeWindowedBuffer()
    buf.add(1000.0, 5.0)
    buf.add(1070.0, 8.0)
    assert buf.span() == pytest.approx(70.0, rel=1e-9)


def test_buffer_mean_and_max_val() -> None:
    """mean() and max_val() return correct values; None on empty buffer."""
    buf = TimeWindowedBuffer()
    assert buf.mean() is None
    assert buf.max_val() is None

    for v in [3.0, 6.0, 9.0]:
        buf.add(float(1000 + v), v)

    assert buf.mean() == pytest.approx(6.0, rel=1e-9)
    assert buf.max_val() == pytest.approx(9.0, rel=1e-9)


def test_buffer_eviction() -> None:
    """evict(now) removes entries older than now - window_seconds."""
    buf = TimeWindowedBuffer(window_seconds=100)
    buf.add(500.0, 1.0)  # old: will be evicted when now=700
    buf.add(650.0, 2.0)  # recent
    buf.add(680.0, 3.0)  # recent

    buf.evict(now=700.0)

    # 500.0 < 700 - 100 = 600 → evicted.
    assert buf.mean() == pytest.approx(2.5, rel=1e-9)
    assert buf.max_val() == pytest.approx(3.0, rel=1e-9)


def test_buffer_reset() -> None:
    """reset() empties the buffer; subsequent mean() and max_val() return None."""
    buf = TimeWindowedBuffer()
    buf.add(1000.0, 7.0)
    buf.add(1060.0, 14.0)
    assert buf.mean() is not None

    buf.reset()
    assert buf.mean() is None
    assert buf.max_val() is None
    assert buf.span() == 0.0

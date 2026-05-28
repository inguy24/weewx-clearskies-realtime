"""Tests for enrichment.input_smoother — ADR-044 §8.

Every test calls input_smoother.reset() first to clear all ring buffers so
each test starts with a clean module state.

Buffer capacities (from module):
  appTemp, dewpoint, outTemp, heatindex, windchill: 120 samples (10 min at 5s interval)
  windSpeed, windGust: 60 samples (5 min)
  rainRate: 24 samples (2 min)

Minimum samples required before get_smoothed() returns a value: 10.
"""

from __future__ import annotations

from weewx_clearskies_realtime.enrichment import input_smoother


def _make_packet(**kwargs) -> dict:  # type: ignore[type-arg]
    """Build a minimal loop packet with the given field values."""
    return dict(kwargs)


# ---------------------------------------------------------------------------
# process_packet feeds buffers and get_smoothed returns mean
# ---------------------------------------------------------------------------


def test_process_packet_feeds_buffers_and_returns_mean() -> None:
    """Feeding 15 packets with appTemp=70.0 → get_smoothed('appTemp') ≈ 70.0.

    15 samples well exceeds the 10-sample minimum, so the mean must be returned.
    """
    input_smoother.reset()
    for _ in range(15):
        input_smoother.process_packet(_make_packet(appTemp=70.0))
    result = input_smoother.get_smoothed("appTemp")
    assert result is not None
    assert abs(result - 70.0) < 0.001


def test_get_smoothed_returns_none_when_fewer_than_10_samples() -> None:
    """Fewer than 10 samples → get_smoothed() returns None (insufficient data).

    The minimum sample count is 10; 5 samples must not produce a result.
    """
    input_smoother.reset()
    for _ in range(5):
        input_smoother.process_packet(_make_packet(appTemp=70.0))
    result = input_smoother.get_smoothed("appTemp")
    assert result is None


def test_get_smoothed_returns_none_for_unknown_field() -> None:
    """Querying a field not tracked by any buffer → None."""
    input_smoother.reset()
    result = input_smoother.get_smoothed("nonexistent_field")
    assert result is None


def test_null_values_are_skipped_and_do_not_count() -> None:
    """Packets with None values for a field are skipped; buffer stays empty.

    After 10 packets where appTemp=None, the buffer has 0 samples → None.
    """
    input_smoother.reset()
    for _ in range(10):
        input_smoother.process_packet(_make_packet(appTemp=None))
    result = input_smoother.get_smoothed("appTemp")
    assert result is None


def test_reset_clears_all_buffers() -> None:
    """After reset(), all buffers are empty and get_smoothed() returns None."""
    input_smoother.reset()
    # Fill appTemp buffer past the minimum threshold
    for _ in range(15):
        input_smoother.process_packet(_make_packet(appTemp=70.0))
    # Confirm data was recorded
    assert input_smoother.get_smoothed("appTemp") is not None
    # Now reset and verify cleared
    input_smoother.reset()
    result = input_smoother.get_smoothed("appTemp")
    assert result is None


def test_apptemp_buffer_capacity_is_120() -> None:
    """appTemp ring buffer holds at most 120 samples (oldest evicted when full).

    Feeding 200 values must not increase the count beyond the 120-sample cap.
    The mean should reflect only the most recent 120 values.
    """
    input_smoother.reset()
    # Feed 100 packets with value=0.0 (will be evicted), then 120 with value=100.0
    for _ in range(100):
        input_smoother.process_packet(_make_packet(appTemp=0.0))
    for _ in range(120):
        input_smoother.process_packet(_make_packet(appTemp=100.0))
    # After 220 total, the ring buffer holds exactly 120 samples all equal to 100.0
    result = input_smoother.get_smoothed("appTemp")
    assert result is not None
    assert abs(result - 100.0) < 0.001


def test_mixed_field_packets_populate_multiple_buffers() -> None:
    """A packet with multiple fields feeds all corresponding buffers simultaneously."""
    input_smoother.reset()
    for _ in range(15):
        input_smoother.process_packet(_make_packet(
            appTemp=72.0,
            dewpoint=60.0,
            outTemp=74.0,
            windSpeed=5.0,
        ))
    assert abs(input_smoother.get_smoothed("appTemp") - 72.0) < 0.001
    assert abs(input_smoother.get_smoothed("dewpoint") - 60.0) < 0.001
    assert abs(input_smoother.get_smoothed("outTemp") - 74.0) < 0.001
    assert abs(input_smoother.get_smoothed("windSpeed") - 5.0) < 0.001


def test_non_numeric_values_are_skipped() -> None:
    """Non-numeric values in packet fields are silently skipped (no exception raised)."""
    input_smoother.reset()
    # First, populate past minimum threshold with valid data
    for _ in range(10):
        input_smoother.process_packet(_make_packet(appTemp=70.0))
    # Now send bad values — must not raise and must not corrupt the buffer
    for _ in range(5):
        input_smoother.process_packet(_make_packet(appTemp="bad_value"))
    # get_smoothed should still return a valid mean from the 10 good samples
    result = input_smoother.get_smoothed("appTemp")
    assert result is not None
    assert abs(result - 70.0) < 0.001


def test_exactly_10_samples_returns_mean() -> None:
    """Exactly 10 samples (the minimum threshold) → get_smoothed() returns mean.

    The boundary condition: count < _MIN_SAMPLES means 10 samples is the first
    count where a result is returned.
    """
    input_smoother.reset()
    for _ in range(10):
        input_smoother.process_packet(_make_packet(appTemp=65.0))
    result = input_smoother.get_smoothed("appTemp")
    assert result is not None
    assert abs(result - 65.0) < 0.001


def test_exactly_9_samples_returns_none() -> None:
    """9 samples (one below minimum) → get_smoothed() returns None."""
    input_smoother.reset()
    for _ in range(9):
        input_smoother.process_packet(_make_packet(appTemp=65.0))
    result = input_smoother.get_smoothed("appTemp")
    assert result is None

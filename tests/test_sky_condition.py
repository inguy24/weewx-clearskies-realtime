"""Tests for sky_condition module (ADR-044).

Each test calls sky_condition.reset() to clear the rolling buffer before
running — the module uses intentional module-level state that would otherwise
bleed between tests.
"""

from __future__ import annotations

import time

import pytest

from weewx_clearskies_realtime import sky_condition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _feed(kc_values: list[float], max_solar: float = 500.0) -> None:
    """Insert kc_values into the buffer as synthetic readings.

    Timestamps are spread evenly across 60 seconds so they all fall inside
    the 30-minute window. max_solar is chosen to keep maxSolarRad above the
    night guard (50 W/m²).
    """
    now = time.time()
    for i, kc in enumerate(kc_values):
        # radiation = kc * max_solar keeps the maths clean.
        radiation = kc * max_solar
        ts = now - (len(kc_values) - i - 1) * 2  # 2-second spacing
        sky_condition.update(radiation, max_solar, timestamp=ts)


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------


def test_clear_sky() -> None:
    """Steady high kc (0.92) with low variance → 'Clear'."""
    sky_condition.reset()
    _feed([0.92] * 35)
    assert sky_condition.classify() == "Clear"


def test_overcast() -> None:
    """All kc values near 0.2 (well below 0.40 threshold) → 'Overcast'."""
    sky_condition.reset()
    _feed([0.20] * 35)
    assert sky_condition.classify() == "Overcast"


def test_partly_cloudy() -> None:
    """Alternating kc 0.3/0.9 — mean ≈ 0.6, high sigma → 'Partly Cloudy'."""
    sky_condition.reset()
    # Alternating creates mean ≈ 0.6 (in 0.40–0.85 band) with high sigma.
    values = [0.3 if i % 2 == 0 else 0.9 for i in range(36)]
    _feed(values)
    result = sky_condition.classify()
    assert result == "Partly Cloudy"


def test_mostly_cloudy() -> None:
    """Steady kc at 0.60, low sigma → 'Mostly Cloudy'."""
    sky_condition.reset()
    _feed([0.60] * 35)
    assert sky_condition.classify() == "Mostly Cloudy"


def test_mostly_clear() -> None:
    """Mean kc ≥ 0.85 but with some variability (sigma ≥ 0.10) → 'Mostly Clear'."""
    sky_condition.reset()
    # Alternate between 0.75 and 1.0: mean ≈ 0.875, sigma > 0.10.
    values = [0.75 if i % 2 == 0 else 1.0 for i in range(36)]
    _feed(values)
    result = sky_condition.classify()
    assert result == "Mostly Clear"


def test_insufficient_data() -> None:
    """Fewer than 30 samples → None (not enough statistical power)."""
    sky_condition.reset()
    _feed([0.90] * 20)  # 20 < _MIN_SAMPLES (30)
    assert sky_condition.classify() is None


def test_exactly_min_samples() -> None:
    """Exactly 30 samples should return a classification (not None)."""
    sky_condition.reset()
    _feed([0.90] * 30)
    assert sky_condition.classify() is not None


def test_night_skipped() -> None:
    """maxSolarRad < 50 W/m² → reading not added to buffer."""
    sky_condition.reset()
    now = time.time()
    # Night readings: maxSolarRad well below 50 W/m².
    for i in range(40):
        sky_condition.update(10.0, 30.0, timestamp=now - i * 2)
    # Buffer should be empty → classify returns None.
    assert sky_condition.classify() is None


def test_night_exactly_at_threshold_skipped() -> None:
    """maxSolarRad = 49.9 (< 50) is night — skipped."""
    sky_condition.reset()
    now = time.time()
    for i in range(40):
        sky_condition.update(40.0, 49.9, timestamp=now - i * 2)
    assert sky_condition.classify() is None


def test_daytime_at_threshold_accepted() -> None:
    """maxSolarRad exactly = 50 is NOT night (≥ 50) — accepted into buffer."""
    sky_condition.reset()
    now = time.time()
    for i in range(30):
        # kc = 40/50 = 0.8 (inside 0.40–0.85 band)
        sky_condition.update(40.0, 50.0, timestamp=now - i * 2)
    # Should have 30 samples and return a classification.
    assert sky_condition.classify() is not None


def test_buffer_eviction() -> None:
    """Entries older than 30 minutes are evicted from the buffer."""
    sky_condition.reset()
    now = time.time()
    old_ts = now - 1900  # 31.7 minutes ago — outside the 30-min window.

    # Inject 35 old readings (all at old_ts, equally old).
    for i in range(35):
        sky_condition.update(460.0, 500.0, timestamp=old_ts - i)

    # Now inject one fresh reading at `now`.  update() evicts entries older
    # than (now - 1800). All the old readings will be purged.
    sky_condition.update(460.0, 500.0, timestamp=now)

    # Only the single fresh entry remains — below _MIN_SAMPLES → None.
    assert sky_condition.classify() is None


def test_radiation_none_skipped() -> None:
    """radiation=None → no entry added."""
    sky_condition.reset()
    now = time.time()
    for i in range(40):
        sky_condition.update(None, 500.0, timestamp=now - i * 2)
    assert sky_condition.classify() is None


def test_negative_radiation_skipped() -> None:
    """radiation < 0 (sensor fault) → no entry added."""
    sky_condition.reset()
    now = time.time()
    for i in range(40):
        sky_condition.update(-5.0, 500.0, timestamp=now - i * 2)
    assert sky_condition.classify() is None


def test_kc_clamped_above_1_2() -> None:
    """kc > 1.2 (extreme cloud-edge) is clamped to 1.2 — still classifies."""
    sky_condition.reset()
    # radiation much greater than max_solar → kc > 1.2 without clamping.
    _feed([1.5] * 35, max_solar=100.0)  # raw kc = 150/100 = 1.5, clamped to 1.2
    # mean_kc = 1.2 ≥ 0.85, sigma = 0 < 0.10 → Clear.
    assert sky_condition.classify() == "Clear"


def test_is_daytime_true() -> None:
    """is_daytime() returns True when buffer has a recent reading."""
    sky_condition.reset()
    sky_condition.update(400.0, 500.0, timestamp=time.time())
    assert sky_condition.is_daytime() is True


def test_is_daytime_false_empty() -> None:
    """is_daytime() returns False when the buffer is empty."""
    sky_condition.reset()
    assert sky_condition.is_daytime() is False


def test_reset_clears_buffer() -> None:
    """reset() makes classify() return None immediately."""
    sky_condition.reset()
    _feed([0.90] * 35)
    assert sky_condition.classify() == "Clear"
    sky_condition.reset()
    assert sky_condition.classify() is None

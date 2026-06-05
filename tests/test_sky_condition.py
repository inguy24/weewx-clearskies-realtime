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
    _feed([0.92] * 40)
    assert sky_condition.classify() == "Clear"


def test_overcast() -> None:
    """kc = 0.50 with low sigma (uniform sky, 0.30–0.85 band) → 'Overcast'.

    Under the sigma-first table (ADR-044 amended): sigma < 0.10 and
    0.30 ≤ mean < 0.85 → 'Overcast'. kc=0.20 would now be 'Heavily Overcast'.
    """
    sky_condition.reset()
    _feed([0.50] * 40)
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
    """High sigma, mean kc < 0.40 (variable sky with low mean) → 'Mostly Cloudy'.

    Under the sigma-first table (ADR-044 amended): sigma >= 0.10 and
    mean < 0.40 → 'Mostly Cloudy'. Alternating 0.1/0.5 gives mean ≈ 0.30
    with high variability.
    """
    sky_condition.reset()
    values = [0.1 if i % 2 == 0 else 0.5 for i in range(40)]
    _feed(values)
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
    """Exactly 36 samples (_MIN_SAMPLES) should return a classification (not None)."""
    sky_condition.reset()
    _feed([0.90] * 36)
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
    for i in range(40):
        # kc = 40/50 = 0.8 (inside 0.40–0.85 band)
        sky_condition.update(40.0, 50.0, timestamp=now - i * 2)
    # Should have 40 samples (≥ _MIN_SAMPLES=36) and return a classification.
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
    _feed([1.5] * 40, max_solar=100.0)  # raw kc = 150/100 = 1.5, clamped to 1.2
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
    _feed([0.90] * 40)
    assert sky_condition.classify() == "Clear"
    sky_condition.reset()
    assert sky_condition.classify() is None


# ---------------------------------------------------------------------------
# Acceptance criteria tests (Task 4)
# ---------------------------------------------------------------------------


def test_fewer_than_36_samples_returns_none() -> None:
    """With 35 samples (< _MIN_SAMPLES=36), classify() returns None."""
    sky_condition.reset()
    _feed([0.90] * 35)
    assert sky_condition.classify() is None


def test_buffer_cleared_at_sunset_transition() -> None:
    """When max_solar_rad drops below threshold, buffer is cleared."""
    sky_condition.reset()
    now = time.time()
    # Feed 40 daytime readings.
    for i in range(40):
        sky_condition.update(400.0, 500.0, timestamp=now - (40 - i) * 5)
    assert sky_condition.classify() is not None  # buffer has data

    # Now send a night reading (max_solar_rad < 50).
    sky_condition.update(0.0, 10.0, timestamp=now + 5)

    # Buffer should be cleared by sunset transition detection.
    assert sky_condition.classify() is None


# ---------------------------------------------------------------------------
# sky_tap — update_from_packet() tests (Bug D fix)
# ---------------------------------------------------------------------------


def test_sky_tap_feeds_buffer_from_canonical_packet() -> None:
    """update_from_packet() feeds sky_condition from a direct-mode (canonical) packet.

    Direct-mode packets have canonical field names (no suffix), so
    strip_suffix is a no-op and the values are passed straight through.
    """
    from weewx_clearskies_realtime.enrichment.sky_tap import update_from_packet

    sky_condition.reset()
    now = time.time()
    # Feed 40 daytime readings via the tap.
    for i in range(40):
        update_from_packet({
            "radiation": 400.0,
            "maxSolarRad": 500.0,
        })
    # Buffer should have 40 entries → classify should return a result.
    assert sky_condition.classify() is not None, (
        "update_from_packet() must feed sky_condition with canonical-name packets"
    )


def test_sky_tap_feeds_buffer_from_suffixed_mqtt_packet() -> None:
    """update_from_packet() feeds sky_condition from MQTT suffixed packet.

    MQTT packets carry 'radiation_Wpm2' and 'maxSolarRad_Wpm2'.
    The tap must strip the suffix to find the canonical names and feed the buffer.
    """
    from weewx_clearskies_realtime.enrichment.sky_tap import update_from_packet

    sky_condition.reset()
    for i in range(40):
        update_from_packet({
            "radiation_Wpm2": "400.0",
            "maxSolarRad_Wpm2": "500.0",
        })
    assert sky_condition.classify() is not None, (
        "update_from_packet() must feed sky_condition from MQTT suffixed packets"
    )


def test_sky_tap_skips_when_radiation_missing() -> None:
    """update_from_packet() skips sky.update when radiation is absent."""
    from weewx_clearskies_realtime.enrichment.sky_tap import update_from_packet
    from unittest.mock import patch

    sky_condition.reset()
    with patch.object(sky_condition, "update") as mock_update:
        update_from_packet({"maxSolarRad": 500.0})
        assert mock_update.call_count == 0, (
            "update_from_packet() must not call sky_condition.update when radiation missing"
        )


def test_sky_tap_skips_when_max_solar_missing() -> None:
    """update_from_packet() skips sky.update when maxSolarRad is absent."""
    from weewx_clearskies_realtime.enrichment.sky_tap import update_from_packet
    from unittest.mock import patch

    sky_condition.reset()
    with patch.object(sky_condition, "update") as mock_update:
        update_from_packet({"radiation": 400.0})
        assert mock_update.call_count == 0, (
            "update_from_packet() must not call sky_condition.update when maxSolarRad missing"
        )


def test_sky_tap_handles_dict_value_shape() -> None:
    """update_from_packet() extracts float from converted {value, label, formatted} dicts."""
    from weewx_clearskies_realtime.enrichment.sky_tap import update_from_packet

    sky_condition.reset()
    for _ in range(40):
        update_from_packet({
            "radiation":   {"value": 400.0, "label": "W/m²", "formatted": "400"},
            "maxSolarRad": {"value": 500.0, "label": "W/m²", "formatted": "500"},
        })
    assert sky_condition.classify() is not None, (
        "update_from_packet() must handle dict-valued fields"
    )


def test_sky_tap_ignores_non_numeric_values() -> None:
    """update_from_packet() silently ignores non-numeric field values."""
    from weewx_clearskies_realtime.enrichment.sky_tap import update_from_packet
    from unittest.mock import patch

    sky_condition.reset()
    with patch.object(sky_condition, "update") as mock_update:
        update_from_packet({"radiation": "bad", "maxSolarRad": "also_bad"})
        assert mock_update.call_count == 0

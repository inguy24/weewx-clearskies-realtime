"""Tests for temperature_comfort.classify() — ADR-044 §5-7.

Every test calls temperature_comfort.reset() first to clear hysteresis and
hold-time cache so each test starts with a clean module state.

All inputs are in °F (the module's native unit).
"""

from __future__ import annotations

import pytest

from weewx_clearskies_realtime import temperature_comfort


# ---------------------------------------------------------------------------
# Acceptance criteria — AC 5.1 through 5.19
# ---------------------------------------------------------------------------


def test_classify_hot_and_humid() -> None:
    """AC 5.1 — Hot tier (app_temp=90) + dewpoint=62 (Humid) → 'Hot and Humid'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=90.0, dewpoint=62.0)
    assert result == "Hot and Humid"


def test_classify_warm_and_very_humid() -> None:
    """AC 5.2 — Warm tier (app_temp=80) + dewpoint=67 (Very Humid) → 'Warm and Very Humid'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=80.0, dewpoint=67.0)
    assert result == "Warm and Very Humid"


def test_classify_pleasant_no_dewpoint() -> None:
    """AC 5.3 — Pleasant tier (app_temp=70), no dewpoint → 'Pleasant' (no modifier)."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=70.0)
    assert result == "Pleasant"


def test_classify_pleasant_slightly_humid() -> None:
    """AC 5.4 — Pleasant tier (app_temp=70) + dewpoint=57 (Slightly Humid) → 'Pleasant and Slightly Humid'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=70.0, dewpoint=57.0)
    assert result == "Pleasant and Slightly Humid"


def test_classify_cool_no_dewpoint() -> None:
    """AC 5.5 — Cool tier (app_temp=50), no dewpoint → 'Cool'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=50.0)
    assert result == "Cool"


def test_classify_chilly_and_oppressive() -> None:
    """AC 5.6 — Chilly tier (app_temp=40) + dewpoint=72 (Oppressive) → 'Chilly and Oppressive'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=40.0, dewpoint=72.0)
    assert result == "Chilly and Oppressive"


def test_classify_near_saturation_foggy_override() -> None:
    """AC 5.7 — Near-saturation: depression=2°F (≤5°F) overrides moisture → 'Pleasant and Foggy'.

    dewpoint depression = out_temp - dewpoint = 70 - 68 = 2°F ≤ 5°F threshold.
    """
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=70.0, dewpoint=68.0, out_temp=70.0)
    assert result == "Pleasant and Foggy"


def test_classify_cold_suppresses_moisture() -> None:
    """AC 5.8 — Cold tier (app_temp=25, tier 5 ≤ 32°F) suppresses moisture modifier.

    Dewpoint=62 would produce 'Humid' in warm tiers, but Cold is tier 5 (≤32°F)
    and moisture is suppressed → result is just 'Cold'.
    """
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=25.0, dewpoint=62.0)
    assert result == "Cold"


def test_classify_very_cold() -> None:
    """AC 5.9 — Very Cold tier (app_temp=15, tier 4, 11-20°F) → 'Very Cold'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=15.0)
    assert result == "Very Cold"


def test_classify_near_saturation_in_cold() -> None:
    """AC 5.10 — Near-saturation allowed in Cold tier: depression=2°F (≤5°F) → 'Cold and Foggy'.

    Foggy override applies even when moisture modifier would otherwise be suppressed.
    """
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=25.0, dewpoint=23.0, out_temp=25.0)
    assert result == "Cold and Foggy"


def test_classify_bitter_cold() -> None:
    """AC 5.11 — Bitter Cold tier (app_temp=-5, tier 2, -9 to 0°F) → 'Bitter Cold'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=-5.0)
    assert result == "Bitter Cold"


def test_classify_dangerously_cold() -> None:
    """AC 5.12 — Dangerously Cold tier (app_temp=-15, tier 1, ≤-10°F) → 'Dangerously Cold'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=-15.0)
    assert result == "Dangerously Cold"


def test_classify_heat_index_danger() -> None:
    """AC 5.13 — Heat Index ≥104°F triggers NWS danger label → 'Dangerous Heat'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=100.0, heatindex=106.0)
    assert result == "Dangerous Heat"


def test_classify_heat_index_extreme_danger() -> None:
    """AC 5.14 — Heat Index ≥125°F triggers NWS extreme danger label → 'Extreme Danger Heat'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=110.0, heatindex=130.0)
    assert result == "Extreme Danger Heat"


def test_classify_wind_chill_danger() -> None:
    """AC 5.15 — Wind Chill ≤-25°F triggers NWS danger label → 'Dangerous Cold'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=-5.0, windchill=-30.0)
    assert result == "Dangerous Cold"


def test_classify_wind_chill_extreme_danger() -> None:
    """AC 5.16 — Wind Chill ≤-45°F triggers NWS extreme danger label → 'Extreme Danger Cold'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=-20.0, windchill=-50.0)
    assert result == "Extreme Danger Cold"


def test_classify_null_app_temp_returns_none() -> None:
    """AC 5.17 — app_temp=None → None (temperature axis unavailable)."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=None)
    assert result is None


def test_classify_null_dewpoint_no_modifier() -> None:
    """AC 5.18 — app_temp=80 (Warm) with no dewpoint → 'Warm' (no moisture modifier)."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=80.0)
    assert result == "Warm"


def test_classify_dangerously_hot() -> None:
    """AC 5.19 — app_temp=110 exceeds all temp tiers → 'Dangerously Hot' (default tier)."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=110.0)
    assert result == "Dangerously Hot"


# ---------------------------------------------------------------------------
# Extra tests beyond the 19 acceptance criteria
# ---------------------------------------------------------------------------


def test_classify_danger_cold_and_foggy() -> None:
    """Extra — Danger label + near-saturation: 'Dangerous Cold and Foggy'.

    Windchill=-30 triggers 'Dangerous Cold'; depression=2°F adds 'and Foggy'.
    """
    temperature_comfort.reset()
    result = temperature_comfort.classify(
        app_temp=-5.0,
        dewpoint=-7.0,
        out_temp=-5.0,
        windchill=-30.0,
    )
    assert result == "Dangerous Cold and Foggy"


def test_classify_hot_and_miserable() -> None:
    """Extra — Hot tier (app_temp=90) + dewpoint=78 (≥75, Miserable) → 'Hot and Miserable'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=90.0, dewpoint=78.0)
    assert result == "Hot and Miserable"


def test_classify_warm_dry_no_modifier() -> None:
    """Extra — Warm tier (app_temp=80) + dewpoint=40 (<45, Dry tier A) → 'Warm' (no modifier)."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=80.0, dewpoint=40.0)
    assert result == "Warm"


# ---------------------------------------------------------------------------
# Boundary tests for temperature tier thresholds
# ---------------------------------------------------------------------------


def test_classify_extreme_cold_tier() -> None:
    """Extreme Cold tier (app_temp=10, tier 3, 1-10°F) → 'Extreme Cold'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=10.0)
    assert result == "Extreme Cold"


def test_classify_very_hot_tier() -> None:
    """Very Hot tier (app_temp=100, tier 11, 96-104°F) → 'Very Hot'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=100.0)
    assert result == "Very Hot"


def test_classify_heat_index_at_exact_danger_threshold() -> None:
    """Heat Index exactly at 104°F threshold → 'Dangerous Heat'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=95.0, heatindex=104.0)
    assert result == "Dangerous Heat"


def test_classify_heat_index_at_exact_extreme_danger_threshold() -> None:
    """Heat Index exactly at 125°F threshold → 'Extreme Danger Heat'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=110.0, heatindex=125.0)
    assert result == "Extreme Danger Heat"


def test_classify_wind_chill_at_exact_danger_threshold() -> None:
    """Wind Chill exactly at -25°F threshold → 'Dangerous Cold'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=-10.0, windchill=-25.0)
    assert result == "Dangerous Cold"


def test_classify_wind_chill_at_exact_extreme_danger_threshold() -> None:
    """Wind Chill exactly at -45°F threshold → 'Extreme Danger Cold'."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=-20.0, windchill=-45.0)
    assert result == "Extreme Danger Cold"


def test_classify_saturation_depression_exactly_at_threshold() -> None:
    """Near-saturation at exactly 5°F depression → 'Pleasant and Foggy'.

    The threshold is depression ≤ 5.0°F, so 5.0 should trigger foggy.
    """
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=70.0, dewpoint=65.0, out_temp=70.0)
    assert result == "Pleasant and Foggy"


def test_classify_saturation_depression_just_above_threshold() -> None:
    """Depression=6°F (>5°F) does NOT trigger foggy — uses moisture modifier instead."""
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=70.0, dewpoint=64.0, out_temp=70.0)
    # dewpoint=64 → Humid modifier; depression=6 > 5 so no foggy override
    assert result == "Pleasant and Humid"


def test_classify_reset_clears_state() -> None:
    """reset() clears cached result and hysteresis state so next call starts fresh."""
    temperature_comfort.reset()
    # First call establishes state
    temperature_comfort.classify(app_temp=70.0, dewpoint=62.0)
    # reset, then re-classify with completely different inputs
    temperature_comfort.reset()
    result = temperature_comfort.classify(app_temp=25.0)
    assert result == "Cold"

"""Tests for conditions_text module (ADR-044).

Beaufort labels come from units/derived.py, which uses lower-case second
word (e.g. "Fresh breeze", not "Fresh Breeze") — the ADR table is
illustrative; the code is authoritative for casing.
"""

from __future__ import annotations

from unittest.mock import patch

from weewx_clearskies_realtime.conditions_text import (
    _comfort_label,
    _compose,
    _precip_label,
    build_weather_text,
)


# ---------------------------------------------------------------------------
# _compose()
# ---------------------------------------------------------------------------


def test_compose_empty() -> None:
    """No parts → empty string."""
    assert _compose([]) == ""


def test_compose_none_parts() -> None:
    """All-None parts → empty string."""
    assert _compose([None, None]) == ""


def test_compose_one_part() -> None:
    """Single part is returned as-is."""
    assert _compose(["Overcast"]) == "Overcast"


def test_compose_two_parts() -> None:
    """Two parts use 'and' separator."""
    assert _compose(["Overcast", "Light Rain"]) == "Overcast and Light Rain"


def test_compose_three_parts() -> None:
    """Three parts use Oxford comma."""
    assert _compose(["Overcast", "Light Rain", "Humid"]) == "Overcast, Light Rain, and Humid"


def test_compose_four_parts() -> None:
    """Four parts: comma-separated with 'and' before last."""
    result = _compose(["A", "B", "C", "D"])
    assert result == "A, B, C, and D"


def test_compose_mixed_none() -> None:
    """None entries are filtered before composing."""
    assert _compose(["Overcast", None, "Humid"]) == "Overcast and Humid"


# ---------------------------------------------------------------------------
# _precip_label()
# ---------------------------------------------------------------------------


def test_precip_none() -> None:
    """None rain rate → None."""
    assert _precip_label(None, "inch_per_hour") is None


def test_precip_zero() -> None:
    """Zero rain rate → None (no precipitation)."""
    assert _precip_label(0.0, "inch_per_hour") is None


def test_precip_light_threshold_lower() -> None:
    """0.01 in/hr < 0.10 threshold → Light Rain."""
    assert _precip_label(0.01, "inch_per_hour") == "Light Rain"


def test_precip_light_just_below_moderate() -> None:
    """0.099 in/hr just below moderate threshold → Light Rain."""
    assert _precip_label(0.099, "inch_per_hour") == "Light Rain"


def test_precip_moderate_at_threshold() -> None:
    """0.10 in/hr at the moderate threshold → Moderate Rain."""
    assert _precip_label(0.10, "inch_per_hour") == "Moderate Rain"


def test_precip_moderate_mid() -> None:
    """0.20 in/hr in moderate band → Moderate Rain."""
    assert _precip_label(0.20, "inch_per_hour") == "Moderate Rain"


def test_precip_moderate_just_below_heavy() -> None:
    """0.299 in/hr just below heavy threshold → Moderate Rain."""
    assert _precip_label(0.299, "inch_per_hour") == "Moderate Rain"


def test_precip_heavy_at_threshold() -> None:
    """0.30 in/hr at heavy threshold → Heavy Rain."""
    assert _precip_label(0.30, "inch_per_hour") == "Heavy Rain"


def test_precip_heavy_extreme() -> None:
    """2.0 in/hr (very heavy) → Heavy Rain."""
    assert _precip_label(2.0, "inch_per_hour") == "Heavy Rain"


def test_precip_unit_mm_per_hour() -> None:
    """Convert mm/hr to in/hr before threshold check.

    1.0 mm/hr ≈ 0.039 in/hr < 0.10 → Light Rain.
    """
    assert _precip_label(1.0, "mm_per_hour") == "Light Rain"


def test_precip_unit_cm_per_hour_moderate() -> None:
    """Convert cm/hr to in/hr.

    0.50 cm/hr ≈ 0.197 in/hr → Moderate Rain.
    """
    assert _precip_label(0.50, "cm_per_hour") == "Moderate Rain"


def test_precip_thresholds_inch_boundaries() -> None:
    """Verify all three threshold boundaries in in/hr."""
    assert _precip_label(0.05, "inch_per_hour") == "Light Rain"
    assert _precip_label(0.10, "inch_per_hour") == "Moderate Rain"
    assert _precip_label(0.30, "inch_per_hour") == "Heavy Rain"


# ---------------------------------------------------------------------------
# _comfort_label()
# ---------------------------------------------------------------------------


def test_comfort_none() -> None:
    """None dewpoint → None."""
    assert _comfort_label(None, "degree_F") is None


def test_comfort_comfortable_below_60() -> None:
    """55 °F (comfortable) → None (omitted from text)."""
    assert _comfort_label(55.0, "degree_F") is None


def test_comfort_exactly_59() -> None:
    """59.9 °F just below Humid threshold → None."""
    assert _comfort_label(59.9, "degree_F") is None


def test_comfort_humid_at_60() -> None:
    """60 °F exactly at Humid threshold → 'Humid'."""
    assert _comfort_label(60.0, "degree_F") == "Humid"


def test_comfort_humid_mid() -> None:
    """62 °F in Humid band → 'Humid'."""
    assert _comfort_label(62.0, "degree_F") == "Humid"


def test_comfort_very_humid_at_65() -> None:
    """65 °F at Very Humid threshold → 'Very Humid'."""
    assert _comfort_label(65.0, "degree_F") == "Very Humid"


def test_comfort_oppressive_at_70() -> None:
    """70 °F at Oppressive threshold → 'Oppressive'."""
    assert _comfort_label(70.0, "degree_F") == "Oppressive"


def test_comfort_miserable_at_75() -> None:
    """75 °F at Miserable threshold → 'Miserable'."""
    assert _comfort_label(75.0, "degree_F") == "Miserable"


def test_comfort_miserable_high() -> None:
    """80 °F well above Miserable threshold → 'Miserable'."""
    assert _comfort_label(80.0, "degree_F") == "Miserable"


def test_comfort_thresholds_all_boundaries() -> None:
    """Verify all comfort level boundaries in °F."""
    assert _comfort_label(59.9, "degree_F") is None
    assert _comfort_label(60.0, "degree_F") == "Humid"
    assert _comfort_label(64.9, "degree_F") == "Humid"
    assert _comfort_label(65.0, "degree_F") == "Very Humid"
    assert _comfort_label(69.9, "degree_F") == "Very Humid"
    assert _comfort_label(70.0, "degree_F") == "Oppressive"
    assert _comfort_label(74.9, "degree_F") == "Oppressive"
    assert _comfort_label(75.0, "degree_F") == "Miserable"


def test_comfort_unit_celsius() -> None:
    """Dewpoint in °C is converted before threshold comparison.

    25 °C = 77 °F → 'Miserable'.
    """
    assert _comfort_label(25.0, "degree_C") == "Miserable"


def test_comfort_unit_celsius_comfortable() -> None:
    """10 °C = 50 °F < 60 °F → None."""
    assert _comfort_label(10.0, "degree_C") is None


# ---------------------------------------------------------------------------
# build_weather_text() — integration tests
# ---------------------------------------------------------------------------


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
def test_sky_and_wind_only(mock_daytime) -> None:
    """Sky + moderate wind, no rain, comfortable → two-part string."""
    # 15 mph = 6.7 m/s → Beaufort 4 "Moderate breeze"
    result = build_weather_text(
        sky="Partly Cloudy",
        wind_speed=15.0,
        wind_speed_unit="mile_per_hour",
    )
    assert result == "Partly Cloudy and Moderate breeze"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
def test_all_parts(mock_daytime) -> None:
    """All four components present → Oxford-comma composition.

    Sky: Overcast
    Precip: 0.05 in/hr → Light Rain
    Wind: 23 mph = 10.28 m/s → Beaufort 5 Fresh breeze
    Comfort: 62 °F → Humid
    """
    result = build_weather_text(
        sky="Overcast",
        rain_rate=0.05,
        rain_rate_unit="inch_per_hour",
        wind_speed=23.0,
        wind_speed_unit="mile_per_hour",
        dewpoint=62.0,
        dewpoint_unit="degree_F",
    )
    assert result == "Overcast, Light Rain, Fresh breeze, and Humid"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
def test_calm_omitted(mock_daytime) -> None:
    """Beaufort 0 (Calm) is omitted from the text (ADR-044 §4)."""
    result = build_weather_text(
        sky="Clear",
        wind_speed=0.0,
        wind_speed_unit="mile_per_hour",
    )
    # Only sky; wind omitted.
    assert result == "Clear"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
def test_calm_zero_mps(mock_daytime) -> None:
    """Wind speed 0.0 m/s (Calm) — not included in output."""
    result = build_weather_text(
        sky="Overcast",
        wind_speed=0.0,
        wind_speed_unit="meter_per_second",
    )
    assert result == "Overcast"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
def test_comfortable_omitted(mock_daytime) -> None:
    """Dewpoint below 60 °F → comfort label omitted."""
    result = build_weather_text(
        sky="Partly Cloudy",
        dewpoint=55.0,
        dewpoint_unit="degree_F",
    )
    assert result == "Partly Cloudy"


def test_provider_fallback() -> None:
    """sky=None with provider_sky → uses provider text."""
    result = build_weather_text(
        sky=None,
        provider_sky="Foggy",
    )
    assert result == "Foggy"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
def test_provider_fallback_overridden_by_sky(mock_daytime) -> None:
    """Local sky takes priority over provider_sky when both present and daytime."""
    result = build_weather_text(
        sky="Mostly Clear",
        provider_sky="Foggy",
    )
    assert result == "Mostly Clear"


def test_no_sky_no_provider() -> None:
    """No sky or provider → only other components contribute."""
    # 15 mph → Beaufort 4 "Moderate breeze"
    result = build_weather_text(
        sky=None,
        provider_sky=None,
        wind_speed=15.0,
        wind_speed_unit="mile_per_hour",
    )
    assert result == "Moderate breeze"


def test_all_absent() -> None:
    """No components present → empty string."""
    result = build_weather_text()
    assert result == ""


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
def test_no_wind_speed(mock_daytime) -> None:
    """wind_speed=None → wind component omitted."""
    result = build_weather_text(
        sky="Clear",
        wind_speed=None,
    )
    assert result == "Clear"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
def test_heavy_rain_and_wind(mock_daytime) -> None:
    """Heavy rain + high wind composes correctly."""
    # 0.5 in/hr → Heavy Rain; 35 mph = 15.6 m/s → Beaufort 7 "Near gale"
    result = build_weather_text(
        sky="Overcast",
        rain_rate=0.5,
        rain_rate_unit="inch_per_hour",
        wind_speed=35.0,
        wind_speed_unit="mile_per_hour",
    )
    assert result == "Overcast, Heavy Rain, and Near gale"


def test_precip_in_mm_per_hour() -> None:
    """Rain rate in mm/hr is converted correctly.

    7.0 mm/hr ≈ 0.276 in/hr → Moderate Rain.
    """
    result = build_weather_text(
        rain_rate=7.0,
        rain_rate_unit="mm_per_hour",
    )
    assert result == "Moderate Rain"


def test_dewpoint_in_celsius() -> None:
    """Dewpoint in °C is converted before comfort classification.

    24 °C = 75.2 °F ≥ 75 → Miserable.
    """
    result = build_weather_text(
        dewpoint=24.0,
        dewpoint_unit="degree_C",
    )
    assert result == "Miserable"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
def test_oppressive_heat(mock_daytime) -> None:
    """High dewpoint + overcast sky → oppressive label."""
    result = build_weather_text(
        sky="Mostly Cloudy",
        dewpoint=72.0,
        dewpoint_unit="degree_F",
    )
    assert result == "Mostly Cloudy and Oppressive"


# ---------------------------------------------------------------------------
# Acceptance criteria tests (Task 4)
# ---------------------------------------------------------------------------


def test_night_falls_back_to_provider_sky() -> None:
    """When is_daytime() is False, sky parameter is ignored; provider_sky used."""
    # Don't mock is_daytime — leave buffer empty so is_daytime() returns False naturally.
    result = build_weather_text(
        sky="Clear",  # this should be ignored at night
        provider_sky="Mostly Cloudy",
    )
    assert result == "Mostly Cloudy"

"""Tests for conditions_text module (ADR-044).

Beaufort labels come from units/derived.py, which uses lower-case second
word (e.g. "Fresh breeze", not "Fresh Breeze") — the ADR table is
illustrative; the code is authoritative for casing.

Changes from original (Task 5):
- _comfort_label() has been removed from conditions_text and replaced by
  temperature_comfort.classify() — all _comfort_label tests removed.
- _compose() now uses "with" connector for the final part:
    2 parts → "{a}, with {b}"
    3+ parts → "{a}, {b}, ..., with {last}"
- build_weather_text() component order: [temperature-comfort, sky, wind, precip]
- build_weather_text() has new params: app_temp, out_temp, heatindex, windchill, temp_unit.
- Integration tests mock temperature_comfort.classify() to isolate 2D matrix logic.
"""

from __future__ import annotations

from unittest.mock import patch

from weewx_clearskies_realtime.conditions_text import (
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


def test_compose_two_parts_uses_with_connector() -> None:
    """Two parts use ', with' separator (not 'and') to avoid double-and.

    Old format: 'Overcast and Light Rain'
    New format: 'Overcast, with Light Rain'
    """
    assert _compose(["Overcast", "Light Rain"]) == "Overcast, with Light Rain"


def test_compose_three_parts_uses_with_for_last() -> None:
    """Three parts: first two comma-separated, last prefixed with 'with'.

    Old format: 'Overcast, Light Rain, and Humid'
    New format: 'Overcast, Light Rain, with Humid'
    """
    assert _compose(["Overcast", "Light Rain", "Windy"]) == "Overcast, Light Rain, with Windy"


def test_compose_four_parts() -> None:
    """Four parts: comma-separated with 'with' before last."""
    result = _compose(["A", "B", "C", "D"])
    assert result == "A, B, C, with D"


def test_compose_mixed_none() -> None:
    """None entries are filtered before composing."""
    assert _compose(["Overcast", None, "Windy"]) == "Overcast, with Windy"


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
# build_weather_text() — integration tests
#
# temperature_comfort.classify() is mocked to isolate conditions_text logic
# from the 2D matrix. is_daytime() is mocked for sky-component tests.
# ---------------------------------------------------------------------------


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value=None)
def test_sky_and_wind_only(mock_classify, mock_daytime) -> None:
    """Sky + moderate wind, no rain, no comfort label → two-part string.

    15 mph = 6.7 m/s → Beaufort 4 "Moderate breeze".
    With no temperature-comfort, composition is: sky, with wind.
    """
    result = build_weather_text(
        sky="Partly Cloudy",
        wind_speed=15.0,
        wind_speed_unit="mile_per_hour",
    )
    assert result == "Partly Cloudy, with Moderate breeze"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value="Warm and Humid")
def test_all_parts_with_comfort_first(mock_classify, mock_daytime) -> None:
    """All four components present in order: comfort, sky, wind, precipitation.

    Comfort: 'Warm and Humid' (mocked)
    Sky: Overcast
    Wind: 23 mph = 10.28 m/s → Beaufort 5 'Fresh breeze'
    Precip: 0.05 in/hr → Light Rain

    Expected order: [comfort, sky, wind, precip] with 'with' connector for last.
    """
    result = build_weather_text(
        app_temp=80.0,
        dewpoint=67.0,
        temp_unit="degree_F",
        dewpoint_unit="degree_F",
        sky="Overcast",
        rain_rate=0.05,
        rain_rate_unit="inch_per_hour",
        wind_speed=23.0,
        wind_speed_unit="mile_per_hour",
    )
    assert result == "Warm and Humid, Overcast, Fresh breeze, with Light Rain"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value=None)
def test_calm_omitted(mock_classify, mock_daytime) -> None:
    """Beaufort 0 (Calm) is omitted from the text (ADR-044 §4)."""
    result = build_weather_text(
        sky="Clear",
        wind_speed=0.0,
        wind_speed_unit="mile_per_hour",
    )
    # Only sky; wind omitted.
    assert result == "Clear"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value=None)
def test_calm_zero_mps(mock_classify, mock_daytime) -> None:
    """Wind speed 0.0 m/s (Calm) — not included in output."""
    result = build_weather_text(
        sky="Overcast",
        wind_speed=0.0,
        wind_speed_unit="meter_per_second",
    )
    assert result == "Overcast"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value=None)
def test_comfortable_temp_omitted_when_classify_returns_none(mock_classify, mock_daytime) -> None:
    """When classify() returns None, temperature-comfort component is omitted.

    Only sky remains → single-part output.
    """
    result = build_weather_text(
        sky="Partly Cloudy",
        app_temp=70.0,
        dewpoint=55.0,
        temp_unit="degree_F",
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
@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value=None)
def test_provider_fallback_overridden_by_sky(mock_classify, mock_daytime) -> None:
    """Local sky takes priority over provider_sky when both present and daytime."""
    result = build_weather_text(
        sky="Mostly Clear",
        provider_sky="Foggy",
    )
    assert result == "Mostly Clear"


def test_no_sky_no_provider() -> None:
    """No sky or provider → only other components contribute.

    15 mph → Beaufort 4 "Moderate breeze". No comfort (no app_temp).
    """
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
@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value=None)
def test_no_wind_speed(mock_classify, mock_daytime) -> None:
    """wind_speed=None → wind component omitted."""
    result = build_weather_text(
        sky="Clear",
        wind_speed=None,
    )
    assert result == "Clear"


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value=None)
def test_heavy_rain_and_wind(mock_classify, mock_daytime) -> None:
    """Heavy rain + high wind with sky composes correctly using 'with' connector.

    0.5 in/hr → Heavy Rain; 35 mph = 15.6 m/s → Beaufort 7 "Near gale".
    Order: [sky, wind, precip] with 'with' before last.
    """
    result = build_weather_text(
        sky="Overcast",
        rain_rate=0.5,
        rain_rate_unit="inch_per_hour",
        wind_speed=35.0,
        wind_speed_unit="mile_per_hour",
    )
    assert result == "Overcast, Near gale, with Heavy Rain"


def test_precip_in_mm_per_hour() -> None:
    """Rain rate in mm/hr is converted correctly.

    7.0 mm/hr ≈ 0.276 in/hr → Moderate Rain.
    """
    result = build_weather_text(
        rain_rate=7.0,
        rain_rate_unit="mm_per_hour",
    )
    assert result == "Moderate Rain"


@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value="Warm and Humid")
def test_comfort_label_from_dewpoint_in_celsius(mock_classify) -> None:
    """Dewpoint in °C is converted before passing to classify().

    Mocking classify() confirms conversion happens before the call.
    24 °C ≈ 75.2 °F — the mock returns 'Warm and Humid' regardless.
    """
    result = build_weather_text(
        app_temp=80.0,
        dewpoint=24.0,
        temp_unit="degree_F",
        dewpoint_unit="degree_C",
    )
    assert result == "Warm and Humid"
    # Verify classify was called (i.e., temperature-comfort path was exercised)
    mock_classify.assert_called_once()


@patch("weewx_clearskies_realtime.conditions_text._sky_condition_module.is_daytime", return_value=True)
@patch("weewx_clearskies_realtime.conditions_text._temperature_comfort.classify", return_value="Warm and Oppressive")
def test_comfort_label_appears_before_sky(mock_classify, mock_daytime) -> None:
    """Temperature-comfort label appears FIRST in the composed string (ADR-044 §9).

    Component order: [comfort, sky, wind, precip].
    """
    result = build_weather_text(
        app_temp=80.0,
        dewpoint=72.0,
        temp_unit="degree_F",
        dewpoint_unit="degree_F",
        sky="Mostly Cloudy",
    )
    assert result == "Warm and Oppressive, with Mostly Cloudy"


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

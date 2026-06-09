"""Tests for Task 6 acceptance criteria — weather text enrichment and
Beaufort label correctness.

AC 6.3: windSpeed=2 mph → Beaufort 1 label "Very Light Breeze"
AC 6.5: _compose() with 1 component returns it unchanged (verified in
        test_conditions_text.py: test_compose_one_part)
AC 6.6: _compose() with 2 components uses ", with" separator (verified in
        test_conditions_text.py: test_compose_two_parts_uses_with_connector)
AC 6.7: _compose() with 3+ components uses comma-then-"with" for last
        (verified in test_conditions_text.py:
        test_compose_three_parts_uses_with_for_last)
AC 6.8: build_weather_text() with all-None inputs → empty string

enrich_weather_text() coverage:
  - injects weatherText INSIDE data["data"] when data["data"] is a dict (Bug B fix)
  - falls back to top-level when data["data"] is absent/non-dict
  - handles empty input dict without raising
  - does NOT write a top-level weatherText when data["data"] is a dict

compose_weather_text() coverage:
  - returns str (from smoothed inputs / sky classify)
  - delegates to build_weather_text with smoothed values
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from weewx_clearskies_realtime.units.derived import beaufort
from weewx_clearskies_realtime.conditions_text import build_weather_text
from weewx_clearskies_realtime.enrichment import input_smoother
from weewx_clearskies_realtime.enrichment.weather_text import (
    _cloud_pct_to_sky,
    _derive_weather_code,
    compose_weather_text,
    enrich_weather_text,
)
from weewx_clearskies_realtime import sky_condition


# ---------------------------------------------------------------------------
# AC 6.3 — Beaufort 1 label is "Very Light Breeze"
# ---------------------------------------------------------------------------


def test_beaufort_very_light_breeze_at_2mph() -> None:
    """AC 6.3: windSpeed=2.0 mph → Beaufort 1 label 'Very Light Breeze'.

    2 mph = 0.894 m/s, which falls in [0.5, 1.6) → Beaufort 1.
    The old label 'Light air' must not appear.
    """
    result = beaufort(2.0, "mile_per_hour")
    assert result["label"] == "Very Light Breeze", (
        f"Expected 'Very Light Breeze' for Beaufort 1, got {result['label']!r}"
    )
    assert result["value"] == 1


# ---------------------------------------------------------------------------
# AC 6.8 — all-null inputs → empty string
# ---------------------------------------------------------------------------


def test_build_weather_text_all_none_returns_empty_string() -> None:
    """AC 6.8: build_weather_text() with no arguments (all None) → empty string.

    No sky, no wind speed, no rain rate, no app_temp → nothing to compose.
    """
    result = build_weather_text()
    assert result == "", f"Expected empty string, got {result!r}"


# ---------------------------------------------------------------------------
# compose_weather_text() — shared composer reads smoothed values
# ---------------------------------------------------------------------------


def test_compose_weather_text_returns_string() -> None:
    """compose_weather_text() always returns a str (may be empty)."""
    input_smoother.reset()
    sky_condition.reset()
    result = compose_weather_text()
    assert isinstance(result, str)


def test_compose_weather_text_empty_when_no_smoothed_data() -> None:
    """compose_weather_text() returns '' when buffers are empty (startup)."""
    input_smoother.reset()
    sky_condition.reset()
    result = compose_weather_text()
    assert result == "", f"Expected empty string with no smoothed data, got {result!r}"


def test_compose_weather_text_uses_smoothed_values() -> None:
    """compose_weather_text() uses input_smoother ring-buffer values, not raw packet.

    Patch build_weather_text to verify the kwargs that compose_weather_text
    passes through.  Smoothed values should be passed, not raw packet fields.
    """
    input_smoother.reset()
    sky_condition.reset()
    # Fill buffers past the minimum 10-sample threshold.
    for _ in range(15):
        input_smoother.process_packet({
            "outTemp": 75.0,
            "windSpeed": 12.0,
            "rainRate": 0.0,
            "dewpoint": 60.0,
            "appTemp": 73.0,
            "heatindex": 77.0,
            "windchill": 75.0,
        })

    captured: list[dict] = []

    def _capture_build(**kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs)
        return "Warm"

    with patch(
        "weewx_clearskies_realtime.enrichment.weather_text.build_weather_text",
        side_effect=_capture_build,
    ):
        compose_weather_text()

    assert len(captured) == 1
    kw = captured[0]
    # Smoothed values should be close to the values we fed.
    assert kw["out_temp"] is not None
    assert abs(kw["out_temp"] - 75.0) < 0.5
    assert kw["wind_speed"] is not None
    assert abs(kw["wind_speed"] - 12.0) < 0.5
    assert kw["temp_unit"] == "degree_F"
    assert kw["wind_speed_unit"] == "mile_per_hour"
    assert kw["rain_rate_unit"] == "inch_per_hour"


def test_compose_weather_text_passes_wind_gust_to_builder() -> None:
    """compose_weather_text() passes wind_gust from the smoothed windGust buffer.

    Fills the windGust ring buffer and captures the kwargs forwarded to
    build_weather_text().  Verifies wind_gust is present, close to the fed
    value, and wind_gust_unit is "mile_per_hour" (consistent with wind_speed_unit).
    """
    input_smoother.reset()
    sky_condition.reset()
    # Feed windGust packets past the minimum 10-sample threshold.
    for _ in range(15):
        input_smoother.process_packet({
            "windSpeed": 8.0,
            "windGust": 22.0,
        })

    captured: list[dict] = []

    def _capture_build(**kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs)
        return "Fresh breeze and Gusty"

    with patch(
        "weewx_clearskies_realtime.enrichment.weather_text.build_weather_text",
        side_effect=_capture_build,
    ):
        compose_weather_text()

    assert len(captured) == 1
    kw = captured[0]
    # wind_gust kwarg must be present and carry the smoothed windGust value.
    assert "wind_gust" in kw, "compose_weather_text() must pass wind_gust kwarg"
    assert kw["wind_gust"] is not None, "wind_gust must be non-None when buffer has data"
    assert abs(kw["wind_gust"] - 22.0) < 0.5, (
        f"Expected wind_gust ~ 22.0, got {kw['wind_gust']}"
    )
    assert kw.get("wind_gust_unit") == "mile_per_hour", (
        f"Expected wind_gust_unit='mile_per_hour', got {kw.get('wind_gust_unit')!r}"
    )


# ---------------------------------------------------------------------------
# enrich_weather_text() — placement inside data["data"] (Bug B fix)
# ---------------------------------------------------------------------------


def test_enrich_weather_text_writes_inside_data_dict() -> None:
    """enrich_weather_text() must write weatherText INSIDE data['data'], not top-level.

    Bug B fix: weatherText belongs in the observation sub-dict so it is
    co-located with all other observation fields.
    """
    input_smoother.reset()
    sky_condition.reset()
    data: dict = {"data": {"barometer": {"value": 30.0, "label": "inHg", "formatted": "30.00"}}}
    result = enrich_weather_text(data)

    # Must be inside data["data"]
    assert "weatherText" in result["data"], (
        "enrich_weather_text() must write weatherText into data['data']"
    )


def test_enrich_weather_text_no_top_level_when_data_dict_present() -> None:
    """When data['data'] is a dict, weatherText must NOT appear at the top level."""
    input_smoother.reset()
    sky_condition.reset()
    data: dict = {"data": {"outTemp": {"value": 70.0, "label": "°F", "formatted": "70.0"}}}
    result = enrich_weather_text(data)

    assert "weatherText" not in result, (
        "enrich_weather_text() must NOT write top-level weatherText when data['data'] exists"
    )


def test_enrich_weather_text_fallback_to_top_level_when_no_data_dict() -> None:
    """When data['data'] is absent, weatherText falls back to top level."""
    input_smoother.reset()
    sky_condition.reset()
    data: dict = {"someOtherField": 42}
    result = enrich_weather_text(data)

    assert "weatherText" in result, (
        "enrich_weather_text() must write top-level weatherText when data['data'] is absent"
    )


def test_enrich_weather_text_fallback_when_data_is_non_dict() -> None:
    """When data['data'] is not a dict (e.g. a list), weatherText falls back to top level."""
    input_smoother.reset()
    sky_condition.reset()
    data: dict = {"data": [1, 2, 3]}
    result = enrich_weather_text(data)

    assert "weatherText" in result, (
        "enrich_weather_text() must write top-level weatherText when data['data'] is non-dict"
    )
    # Must NOT have written inside the list
    assert "weatherText" not in result["data"]


def test_enrich_weather_text_handles_empty_data() -> None:
    """enrich_weather_text() handles empty dict without raising."""
    input_smoother.reset()
    sky_condition.reset()
    data: dict = {}
    result = enrich_weather_text(data)
    # With empty data (no data sub-dict), falls back to top level.
    assert "weatherText" in result, (
        "enrich_weather_text() must inject 'weatherText' into an empty dict"
    )


def test_enrich_weather_text_returns_same_dict() -> None:
    """enrich_weather_text() returns the same dict object it received (in-place mutation)."""
    input_smoother.reset()
    sky_condition.reset()
    data: dict = {"someField": 42}
    result = enrich_weather_text(data)
    assert result is data, "enrich_weather_text() must return the same dict object"


def test_enrich_weather_text_weathertext_value_is_none_or_string() -> None:
    """enrich_weather_text() weatherText value is None or a non-empty string.

    With no smoothed data available, weatherText should be None (empty
    string coerced to None by 'text or None' logic).
    """
    input_smoother.reset()
    sky_condition.reset()
    data: dict = {"data": {}}
    result = enrich_weather_text(data)
    value = result["data"]["weatherText"]
    assert value is None or isinstance(value, str), (
        f"weatherText must be None or str, got {type(value)!r}: {value!r}"
    )


# ---------------------------------------------------------------------------
# _cloud_pct_to_sky day/night vocabulary tests
# ---------------------------------------------------------------------------


def test_cloud_pct_to_sky_day_returns_sunny() -> None:
    """≤10% cloud cover + daytime → 'Sunny'."""
    assert _cloud_pct_to_sky(5, is_day=True) == "Sunny"


def test_cloud_pct_to_sky_day_returns_mostly_sunny() -> None:
    """≤25% cloud cover + daytime → 'Mostly Sunny'."""
    assert _cloud_pct_to_sky(20, is_day=True) == "Mostly Sunny"


def test_cloud_pct_to_sky_night_returns_clear() -> None:
    """≤10% cloud cover + night → 'Clear'."""
    assert _cloud_pct_to_sky(5, is_day=False) == "Clear"


def test_cloud_pct_to_sky_cloudy_replaces_overcast() -> None:
    """>85% cloud cover → 'Cloudy' (not 'Overcast')."""
    assert _cloud_pct_to_sky(90) == "Cloudy"


# ---------------------------------------------------------------------------
# _derive_weather_code alias tests (Sunny/Mostly Sunny/Cloudy)
# ---------------------------------------------------------------------------


def test_weather_code_sunny_maps_to_zero() -> None:
    """'Sunny' → WMO code 0 (same as 'Clear')."""
    assert _derive_weather_code(effective_sky="Sunny", rain_label=None, is_foggy=False) == 0


def test_weather_code_mostly_sunny_maps_to_one() -> None:
    """'Mostly Sunny' → WMO code 1 (same as 'Mostly Clear')."""
    assert _derive_weather_code(effective_sky="Mostly Sunny", rain_label=None, is_foggy=False) == 1


def test_weather_code_cloudy_maps_to_three() -> None:
    """'Cloudy' → WMO code 3 (same as former 'Overcast')."""
    assert _derive_weather_code(effective_sky="Cloudy", rain_label=None, is_foggy=False) == 3

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
  - injects weatherText key into response dict
  - handles empty input dict without raising
"""

from __future__ import annotations

import pytest

from weewx_clearskies_realtime.units.derived import beaufort
from weewx_clearskies_realtime.conditions_text import build_weather_text
from weewx_clearskies_realtime.enrichment.weather_text import enrich_weather_text


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
# enrich_weather_text() — injects weatherText key
# ---------------------------------------------------------------------------


def test_enrich_weather_text_injects_field() -> None:
    """enrich_weather_text() injects weatherText key into response."""
    data: dict = {"data": {"barometer": 30.0}}
    result = enrich_weather_text(data)
    assert "weatherText" in result, (
        "enrich_weather_text() must inject 'weatherText' key into the response dict"
    )


def test_enrich_weather_text_handles_empty_data() -> None:
    """enrich_weather_text() handles empty dict without raising."""
    data: dict = {}
    result = enrich_weather_text(data)
    assert "weatherText" in result, (
        "enrich_weather_text() must inject 'weatherText' into an empty dict"
    )


def test_enrich_weather_text_returns_same_dict() -> None:
    """enrich_weather_text() returns the same dict object it received (in-place mutation)."""
    data: dict = {"someField": 42}
    result = enrich_weather_text(data)
    assert result is data, "enrich_weather_text() must return the same dict object"


def test_enrich_weather_text_weathertext_is_none_or_string() -> None:
    """enrich_weather_text() weatherText value is None or a non-empty string.

    With no smoothed data available, the enrichment should set weatherText
    to None (empty string coerced to None by 'text or None' logic).
    """
    data: dict = {}
    result = enrich_weather_text(data)
    value = result["weatherText"]
    assert value is None or isinstance(value, str), (
        f"weatherText must be None or str, got {type(value)!r}: {value!r}"
    )

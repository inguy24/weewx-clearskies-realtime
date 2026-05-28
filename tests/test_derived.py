"""Tests for units/derived.py — Beaufort scale and comfort index.

Numeric values are verified against WMO Beaufort scale thresholds (m/s) and
NWS heat-index / wind-chill applicability thresholds (50 °F / 80 °F).
"""

from __future__ import annotations

import pytest

from weewx_clearskies_realtime.units.derived import beaufort, comfort_index


# ---------------------------------------------------------------------------
# beaufort()
# ---------------------------------------------------------------------------


def test_beaufort_calm() -> None:
    """0.0 mph (0 m/s) → Beaufort 0 Calm."""
    result = beaufort(0.0, "mile_per_hour")
    assert result["value"] == 0
    assert result["label"] == "Calm"
    assert result["formatted"] == "0"


def test_beaufort_gentle_breeze_mph() -> None:
    """10 mph = 4.4704 m/s → Beaufort 3 Gentle breeze (3.4–5.5)."""
    result = beaufort(10.0, "mile_per_hour")
    assert result["value"] == 3
    assert result["label"] == "Gentle breeze"


def test_beaufort_strong_breeze_kph() -> None:
    """50 km/h = 13.89 m/s → Beaufort 6 Strong breeze (10.8–13.9)."""
    result = beaufort(50.0, "km_per_hour")
    assert result["value"] == 6
    assert result["label"] == "Strong breeze"


def test_beaufort_hurricane() -> None:
    """80 mph = 35.76 m/s → Beaufort 12 Hurricane (≥32.7)."""
    result = beaufort(80.0, "mile_per_hour")
    assert result["value"] == 12
    assert result["label"] == "Hurricane"
    assert result["formatted"] == "12"


def test_beaufort_from_mps() -> None:
    """5.0 m/s → Beaufort 3 (threshold 3.4–5.5); source_unit short-circuit."""
    result = beaufort(5.0, "meter_per_second")
    assert result["value"] == 3
    assert result["label"] == "Gentle breeze"


def test_beaufort_from_knot() -> None:
    """30 knots = 15.43 m/s → Beaufort 7 Near gale (13.9–17.2)."""
    result = beaufort(30.0, "knot")
    assert result["value"] == 7
    assert result["label"] == "Near gale"


def test_beaufort_upper_boundary_calm() -> None:
    """Exactly 0.5 m/s is NOT calm (< 0.5 is calm); 0.5 → Beaufort 1."""
    result = beaufort(0.5, "meter_per_second")
    assert result["value"] == 1
    assert result["label"] == "Very Light Breeze"


def test_beaufort_returns_dict_keys() -> None:
    """Return dict always has value, label, formatted."""
    result = beaufort(5.0, "meter_per_second")
    assert set(result.keys()) == {"value", "label", "formatted"}


def test_beaufort_zero_mps() -> None:
    """Exactly 0 m/s → Beaufort 0 Calm (< 0.5)."""
    result = beaufort(0.0, "meter_per_second")
    assert result["value"] == 0


def test_beaufort_all_scale_points() -> None:
    """Spot-check each Beaufort number using canonical m/s mid-points."""
    # Use values safely within each band, not on the boundary.
    cases = [
        (0.2,  0),   # Calm
        (1.0,  1),   # Light air
        (2.5,  2),   # Light breeze
        (4.5,  3),   # Gentle breeze
        (7.0,  4),   # Moderate breeze
        (9.5,  5),   # Fresh breeze
        (12.0, 6),   # Strong breeze
        (15.5, 7),   # Near gale
        (18.0, 8),   # Gale
        (22.0, 9),   # Strong gale
        (26.0, 10),  # Storm
        (31.0, 11),  # Violent storm
        (35.0, 12),  # Hurricane
    ]
    for mps, expected_number in cases:
        result = beaufort(mps, "meter_per_second")
        assert result["value"] == expected_number, (
            f"Expected Beaufort {expected_number} at {mps} m/s, "
            f"got {result['value']} ({result['label']})"
        )


# ---------------------------------------------------------------------------
# comfort_index()
# ---------------------------------------------------------------------------


def test_comfort_index_cold() -> None:
    """40 °F ≤ 50 °F → windChill."""
    assert comfort_index(40.0, "degree_F") == "windChill"


def test_comfort_index_hot() -> None:
    """90 °F ≥ 80 °F → heatIndex."""
    assert comfort_index(90.0, "degree_F") == "heatIndex"


def test_comfort_index_moderate() -> None:
    """65 °F is between thresholds → none."""
    assert comfort_index(65.0, "degree_F") == "none"


def test_comfort_boundary_50f() -> None:
    """Exactly 50 °F is inclusive for windChill (≤ 50)."""
    assert comfort_index(50.0, "degree_F") == "windChill"


def test_comfort_boundary_80f() -> None:
    """Exactly 80 °F is inclusive for heatIndex (≥ 80)."""
    assert comfort_index(80.0, "degree_F") == "heatIndex"


def test_comfort_index_celsius_cold() -> None:
    """5 °C = 41 °F → windChill."""
    assert comfort_index(5.0, "degree_C") == "windChill"


def test_comfort_index_celsius_hot() -> None:
    """30 °C = 86 °F → heatIndex."""
    assert comfort_index(30.0, "degree_C") == "heatIndex"


def test_comfort_index_celsius_moderate() -> None:
    """18 °C = 64.4 °F → none."""
    assert comfort_index(18.0, "degree_C") == "none"


def test_comfort_index_degree_f_short_circuit() -> None:
    """degree_F source_unit skips convert(); result is identical."""
    # 49 °F → windChill without any conversion call.
    assert comfort_index(49.0, "degree_F") == "windChill"


# ---------------------------------------------------------------------------
# Integration: UnitTransformer.transform_record() includes derived fields
# ---------------------------------------------------------------------------


def test_transform_record_includes_beaufort() -> None:
    """transform_record() adds 'beaufort' when windSpeed is present."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(
        target_units={
            "group_temperature": "degree_C",
            "group_speed": "km_per_hour",
        }
    )
    # US units (code 1): outTemp in °F, windSpeed in mph
    record = {"outTemp": 90.0, "windSpeed": 15.0, "usUnits": 1}
    result = t.transform_record(record, us_units=1)

    assert "beaufort" in result
    beaufort_val = result["beaufort"]
    assert isinstance(beaufort_val, dict)
    assert isinstance(beaufort_val["value"], int)
    assert beaufort_val["value"] >= 0


def test_transform_record_includes_comfort_index() -> None:
    """transform_record() adds 'comfortIndex' when outTemp is present."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(
        target_units={
            "group_temperature": "degree_C",
            "group_speed": "km_per_hour",
        }
    )
    record = {"outTemp": 90.0, "windSpeed": 15.0, "usUnits": 1}
    result = t.transform_record(record, us_units=1)

    assert "comfortIndex" in result
    # 90 °F ≥ 80 °F → heatIndex
    assert result["comfortIndex"] == "heatIndex"


def test_transform_record_comfort_cold() -> None:
    """40 °F outTemp in US record → windChill."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(target_units={"group_temperature": "degree_C"})
    record = {"outTemp": 40.0}
    result = t.transform_record(record, us_units=1)
    assert result["comfortIndex"] == "windChill"


def test_transform_record_no_wind_no_beaufort() -> None:
    """If windSpeed is absent, beaufort is not added."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(target_units={"group_temperature": "degree_C"})
    record = {"outTemp": 70.0}
    result = t.transform_record(record, us_units=1)
    assert "beaufort" not in result


def test_transform_record_no_temp_no_comfort() -> None:
    """If outTemp is absent, comfortIndex is not added."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(target_units={"group_speed": "km_per_hour"})
    record = {"windSpeed": 10.0}
    result = t.transform_record(record, us_units=1)
    assert "comfortIndex" not in result


# ---------------------------------------------------------------------------
# Integration: UnitTransformer.add_derived_fields() (MQTT path)
# ---------------------------------------------------------------------------


def test_add_derived_fields_basic() -> None:
    """add_derived_fields() populates beaufort and comfortIndex."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(
        target_units={
            "group_temperature": "degree_F",
            "group_speed": "mile_per_hour",
        }
    )
    record: dict = {
        "windSpeed": {"value": 10.0, "label": " mph", "formatted": "10"},
        "outTemp":   {"value": 40.0, "label": "°F",   "formatted": "40.0"},
    }
    t.add_derived_fields(record)

    assert "beaufort" in record
    # 10 mph = 4.47 m/s → Beaufort 3 Gentle breeze
    assert record["beaufort"]["value"] == 3
    assert record["beaufort"]["label"] == "Gentle breeze"

    assert "comfortIndex" in record
    # 40 °F ≤ 50 °F → windChill
    assert record["comfortIndex"] == "windChill"


def test_add_derived_fields_hot() -> None:
    """add_derived_fields() with high temp → heatIndex."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(
        target_units={
            "group_temperature": "degree_F",
            "group_speed": "mile_per_hour",
        }
    )
    record: dict = {
        "windSpeed": {"value": 5.0,  "label": " mph", "formatted": "5"},
        "outTemp":   {"value": 95.0, "label": "°F",   "formatted": "95.0"},
    }
    t.add_derived_fields(record)

    assert record["comfortIndex"] == "heatIndex"


def test_add_derived_fields_missing_wind() -> None:
    """add_derived_fields() skips beaufort when windSpeed is absent."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(
        target_units={
            "group_temperature": "degree_F",
            "group_speed": "mile_per_hour",
        }
    )
    record: dict = {
        "outTemp": {"value": 65.0, "label": "°F", "formatted": "65.0"},
    }
    t.add_derived_fields(record)
    assert "beaufort" not in record
    assert record["comfortIndex"] == "none"


def test_add_derived_fields_none_value() -> None:
    """add_derived_fields() skips fields whose value is None."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(
        target_units={
            "group_temperature": "degree_F",
            "group_speed": "mile_per_hour",
        }
    )
    record: dict = {
        "windSpeed": {"value": None, "label": " mph", "formatted": "N/A"},
        "outTemp":   {"value": None, "label": "°F",   "formatted": "N/A"},
    }
    t.add_derived_fields(record)
    # Neither derived field should be added when source values are None.
    assert "beaufort" not in record
    assert "comfortIndex" not in record


def test_add_derived_fields_metric_display_units() -> None:
    """add_derived_fields() works correctly when display units are Metric."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    t = UnitTransformer(
        target_units={
            "group_temperature": "degree_C",
            "group_speed": "km_per_hour",
        }
    )
    # 50 km/h = 13.89 m/s → Beaufort 6 Strong breeze
    # 30 °C = 86 °F → heatIndex
    record: dict = {
        "windSpeed": {"value": 50.0, "label": " km/h", "formatted": "50"},
        "outTemp":   {"value": 30.0, "label": "°C",    "formatted": "30.0"},
    }
    t.add_derived_fields(record)

    assert record["beaufort"]["value"] == 6
    assert record["comfortIndex"] == "heatIndex"

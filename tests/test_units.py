"""Tests for the units/ package.

All numeric comparisons use pytest.approx(rel=1e-6) to match weewx's own
floating-point precision without requiring exact bit equality.

Test numbering follows the brief spec (1–26) for auditor cross-reference.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_realtime.units import (
    OBS_GROUP,
    UNIT_SYSTEMS,
    VALID_UNITS,
    UnitTransformer,
    convert,
    format_value,
    get_label,
    get_source_unit,
)
from weewx_clearskies_realtime.units.conversion import (
    CM_PER_INCH,
    INHG_PER_MBAR,
    METER_PER_FOOT,
    METER_PER_MILE,
    MM_PER_INCH,
)

# ---------------------------------------------------------------------------
# Conversion accuracy — match weewx 5.3.1 exactly
# ---------------------------------------------------------------------------


def test_f_to_c() -> None:
    """1. Freezing, boiling, and a mid-range temperature."""
    assert convert(32.0, "degree_F", "degree_C") == pytest.approx(0.0, rel=1e-6, abs=1e-9)
    assert convert(212.0, "degree_F", "degree_C") == pytest.approx(100.0, rel=1e-6)
    assert convert(72.0, "degree_F", "degree_C") == pytest.approx(
        (72.0 - 32.0) / 1.8, rel=1e-6
    )


def test_c_to_f() -> None:
    """2. Round-trip C → F at canonical values."""
    assert convert(0.0, "degree_C", "degree_F") == pytest.approx(32.0, rel=1e-6, abs=1e-9)
    assert convert(100.0, "degree_C", "degree_F") == pytest.approx(212.0, rel=1e-6)


def test_f_to_k() -> None:
    """3. Freezing point in Kelvin."""
    assert convert(32.0, "degree_F", "degree_K") == pytest.approx(273.15, rel=1e-6)


def test_mph_to_kph() -> None:
    """4. 60 mph using weewx factor 1.609344."""
    assert convert(60.0, "mile_per_hour", "km_per_hour") == pytest.approx(
        60.0 * 1.609344, rel=1e-6
    )


def test_mph_to_knot() -> None:
    """5. 10 mph → knots using weewx factor 0.868976242."""
    assert convert(10.0, "mile_per_hour", "knot") == pytest.approx(
        10.0 * 0.868976242, rel=1e-6
    )


def test_mph_to_mps() -> None:
    """6. 10 mph → m/s using weewx factor 0.44704."""
    assert convert(10.0, "mile_per_hour", "meter_per_second") == pytest.approx(
        10.0 * 0.44704, rel=1e-6
    )


def test_inhg_to_mbar() -> None:
    """7. Standard atmosphere: 29.92 inHg → ~1013.25 mbar."""
    result = convert(29.92, "inHg", "mbar")
    assert result == pytest.approx(29.92 / INHG_PER_MBAR, rel=1e-6)
    # Sanity: near standard atmosphere
    assert result is not None
    assert 1013.0 < result < 1013.5


def test_mbar_to_hpa_identity() -> None:
    """8. mbar and hPa are numerically identical (1 mbar == 1 hPa)."""
    assert convert(1013.25, "mbar", "hPa") == pytest.approx(1013.25, rel=1e-6)


def test_inch_to_mm() -> None:
    """9. Exact MM_PER_INCH factor."""
    assert convert(1.0, "inch", "mm") == pytest.approx(MM_PER_INCH, rel=1e-6)
    assert convert(1.0, "inch", "mm") == pytest.approx(25.4, rel=1e-6)


def test_foot_to_meter() -> None:
    """10. 5280 feet → METER_PER_MILE (1609.34 m by definition)."""
    result = convert(5280.0, "foot", "meter")
    assert result == pytest.approx(METER_PER_MILE, rel=1e-6)
    # Sanity: METER_PER_FOOT * 5280 == METER_PER_MILE
    assert METER_PER_FOOT * 5280.0 == pytest.approx(METER_PER_MILE, rel=1e-9)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_convert_none() -> None:
    """11. None input → None output (no crash)."""
    assert convert(None, "degree_F", "degree_C") is None


def test_convert_same_unit() -> None:
    """12. Identity: same from/to unit → value unchanged."""
    assert convert(72.5, "degree_F", "degree_F") == pytest.approx(72.5, rel=1e-9)
    assert convert(0.0, "degree_F", "degree_F") == pytest.approx(0.0, abs=1e-12)


def test_convert_unknown_pair_raises() -> None:
    """13. Unknown (from, to) pair raises ValueError."""
    with pytest.raises(ValueError, match="No conversion"):
        convert(1.0, "degree_F", "watt_per_meter_squared")


# ---------------------------------------------------------------------------
# UnitTransformer — transform_record()
# ---------------------------------------------------------------------------

# US system (code 1) → Metric target
_METRIC_TARGETS = {
    "group_temperature":  "degree_C",
    "group_speed":        "km_per_hour",
    "group_pressure":     "mbar",
    "group_pressurerate": "mbar_per_hour",
    "group_rain":         "mm",
    "group_rainrate":     "mm_per_hour",
    "group_altitude":     "meter",
    "group_distance":     "km",
    "group_direction":    "degree_compass",
    "group_radiation":    "watt_per_meter_squared",
    "group_percent":      "percent",
    "group_moisture":     "centibar",
    "group_volt":         "volt",
}


def test_transform_record_us() -> None:
    """14. US system record converted to Metric targets."""
    transformer = UnitTransformer(target_units=_METRIC_TARGETS)
    record = {
        "dateTime": 1716000000,
        "usUnits":  1,
        "interval": 5,
        "outTemp":  72.0,   # 72 °F → ~22.222 °C
        "windSpeed": 10.0,  # 10 mph → 16.09344 km/h
    }
    result = transformer.transform_record(record, us_units=1)

    # Metadata fields stripped
    assert "dateTime" not in result
    assert "usUnits" not in result
    assert "interval" not in result

    out_temp = result["outTemp"]
    assert isinstance(out_temp, dict)
    assert out_temp["value"] == pytest.approx((72.0 - 32.0) / 1.8, rel=1e-6)
    assert out_temp["label"] == "°C"

    wind_speed = result["windSpeed"]
    assert isinstance(wind_speed, dict)
    assert wind_speed["value"] == pytest.approx(10.0 * 1.609344, rel=1e-6)
    assert wind_speed["label"] == " km/h"


def test_transform_record_none_values() -> None:
    """15. None observation values produce {value: None, formatted: 'N/A'}."""
    transformer = UnitTransformer(target_units=_METRIC_TARGETS)
    record = {"outTemp": None, "windSpeed": None}
    result = transformer.transform_record(record, us_units=1)

    assert result["outTemp"]["value"] is None
    assert result["outTemp"]["formatted"] == "N/A"
    assert result["windSpeed"]["value"] is None
    assert result["windSpeed"]["formatted"] == "N/A"


# ---------------------------------------------------------------------------
# UnitTransformer — transform_field()
# ---------------------------------------------------------------------------


def test_transform_field_string_input() -> None:
    """16. MQTT string '72.5' is parsed to float before conversion."""
    transformer = UnitTransformer(target_units={"group_temperature": "degree_C"})
    result = transformer.transform_field("outTemp", "72.5", "degree_F")
    assert isinstance(result["value"], float)
    assert result["value"] == pytest.approx((72.5 - 32.0) / 1.8, rel=1e-6)
    assert result["label"] == "°C"


def test_transform_field_wind_direction_south() -> None:
    """17a. 180° maps to 'S'."""
    transformer = UnitTransformer(target_units={"group_direction": "degree_compass"})
    result = transformer.transform_field("windDir", 180.0, "degree_compass")
    assert result["formatted"] == "S"
    assert result["label"] == "S"
    assert result["value"] == pytest.approx(180.0)


def test_transform_field_wind_direction_ne() -> None:
    """17b. 45° maps to 'NE'."""
    transformer = UnitTransformer(target_units={"group_direction": "degree_compass"})
    result = transformer.transform_field("windDir", 45.0, "degree_compass")
    assert result["formatted"] == "NE"


def test_transform_field_custom_ordinates() -> None:
    """18. Custom 16-label list is used for compass direction output."""
    custom = ["Nord", "NNO", "NO", "ONO", "O", "OSO", "SO", "SSO",
              "Sud", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    transformer = UnitTransformer(
        target_units={"group_direction": "degree_compass"},
        ordinates=custom,
    )
    result = transformer.transform_field("windDir", 180.0, "degree_compass")
    assert result["formatted"] == "Sud"


def test_transform_record_unknown_obs() -> None:
    """19. Observation not in OBS_GROUP is passed through raw."""
    transformer = UnitTransformer(target_units=_METRIC_TARGETS)
    record = {"someCustomField": 42.0}
    result = transformer.transform_record(record, us_units=1)
    assert result["someCustomField"] == 42.0


# ---------------------------------------------------------------------------
# extras sub-dict recursion (bug fix — fields inside extras were not converted)
# ---------------------------------------------------------------------------


def test_transform_record_extras_known_field_converted() -> None:
    """19a. extras sub-dict: field in OBS_GROUP is converted and formatted.

    inDewpoint is in OBS_GROUP (group_temperature).  A raw float value inside
    the extras dict must go through the same conversion/formatting path as if
    it were a top-level key — not pass through as an unrounded float.
    """
    transformer = UnitTransformer(target_units=_METRIC_TARGETS)
    # inDewpoint is group_temperature; US system source unit is degree_F.
    record = {
        "extras": {
            "inDewpoint": 58.0,  # 58 °F → 14.444 °C
        }
    }
    result = transformer.transform_record(record, us_units=1)

    assert "extras" in result
    extras = result["extras"]
    assert isinstance(extras, dict)

    in_dew = extras["inDewpoint"]
    assert isinstance(in_dew, dict), (
        f"expected ConvertedValue dict, got {in_dew!r} — "
        "inDewpoint inside extras was not transformed"
    )
    expected_c = (58.0 - 32.0) / 1.8
    assert in_dew["value"] == pytest.approx(expected_c, rel=1e-6)
    assert in_dew["label"] == "°C"
    # formatted string honours the default degree_C format ("%.1f")
    assert in_dew["formatted"] == "14.4"


def test_transform_record_extras_unknown_field_passthrough() -> None:
    """19b. extras sub-dict: field NOT in OBS_GROUP passes through unchanged."""
    transformer = UnitTransformer(target_units=_METRIC_TARGETS)
    record = {
        "extras": {
            "luminosity": 12345.678,
            "foo": "bar",
        }
    }
    result = transformer.transform_record(record, us_units=1)

    extras = result["extras"]
    assert isinstance(extras, dict)
    # Neither "luminosity" nor "foo" are in OBS_GROUP — raw passthrough.
    assert extras["luminosity"] == pytest.approx(12345.678, rel=1e-6)
    assert extras["foo"] == "bar"


def test_transform_record_extras_mixed() -> None:
    """19c. extras sub-dict with both known and unknown fields handled correctly."""
    transformer = UnitTransformer(target_units=_METRIC_TARGETS)
    record = {
        "extras": {
            "inDewpoint": 32.0,   # known — 32 °F → 0 °C
            "luminosity": 500.0,  # unknown — raw passthrough
        }
    }
    result = transformer.transform_record(record, us_units=1)

    extras = result["extras"]
    assert isinstance(extras, dict)

    # inDewpoint converted.
    in_dew = extras["inDewpoint"]
    assert isinstance(in_dew, dict)
    assert in_dew["value"] == pytest.approx(0.0, abs=1e-9)

    # luminosity unchanged.
    assert extras["luminosity"] == pytest.approx(500.0, rel=1e-6)


def test_transform_record_extras_none_value() -> None:
    """19d. extras sub-dict: None value for a known field produces N/A result."""
    transformer = UnitTransformer(target_units=_METRIC_TARGETS)
    record = {"extras": {"inDewpoint": None}}
    result = transformer.transform_record(record, us_units=1)

    in_dew = result["extras"]["inDewpoint"]  # type: ignore[index]
    assert isinstance(in_dew, dict)
    assert in_dew["value"] is None
    assert in_dew["formatted"] == "N/A"


def test_transform_record_extras_not_a_dict_passthrough() -> None:
    """19e. If extras value is not a dict (e.g. a string), it passes through raw.

    The extras recursion only activates when the value of the "extras" key IS
    a dict.  Other shapes pass through unchanged so the code is future-safe.
    """
    transformer = UnitTransformer(target_units=_METRIC_TARGETS)
    record = {"extras": "unexpected-string"}
    result = transformer.transform_record(record, us_units=1)
    # extras is not a dict → treated as unknown passthrough
    assert result["extras"] == "unexpected-string"


# ---------------------------------------------------------------------------
# Groups helpers
# ---------------------------------------------------------------------------


def test_get_source_unit_us() -> None:
    """20. outTemp in US system → degree_F."""
    assert get_source_unit("outTemp", 1) == "degree_F"


def test_get_source_unit_metric() -> None:
    """21. outTemp in Metric system → degree_C."""
    assert get_source_unit("outTemp", 16) == "degree_C"


def test_get_source_unit_unknown_obs() -> None:
    """22. Unknown observation name → None."""
    assert get_source_unit("notARealObservation", 1) is None


# ---------------------------------------------------------------------------
# Labels / formatting
# ---------------------------------------------------------------------------


def test_format_value_defaults() -> None:
    """23. degree_F uses '%.1f'; inch uses '%.2f'."""
    assert format_value(72.123, "degree_F") == "72.1"
    assert format_value(1.456, "inch") == "1.46"


def test_label_override() -> None:
    """24. Custom label replaces the default."""
    overrides = {"degree_C": "° Celsius"}
    assert get_label("degree_C", overrides) == "° Celsius"
    # Without override, default is returned
    assert get_label("degree_C") == "°C"


def test_format_override() -> None:
    """25. Custom format string replaces the default."""
    overrides = {"degree_F": "%.2f"}
    assert format_value(72.123, "degree_F", overrides) == "72.12"
    # Without override, default %.1f applies
    assert format_value(72.123, "degree_F") == "72.1"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_transformer_rejects_invalid_unit() -> None:
    """26. Passing an invalid unit for a group raises ValueError at construction."""
    with pytest.raises(ValueError, match="Invalid unit"):
        UnitTransformer(target_units={"group_temperature": "furlongs_per_fortnight"})


# ---------------------------------------------------------------------------
# Additional conversion round-trips (not in brief spec, but high value)
# ---------------------------------------------------------------------------


def test_cm_to_inch_roundtrip() -> None:
    """2.54 cm == 1 inch, round-trip via CM_PER_INCH."""
    assert convert(CM_PER_INCH, "cm", "inch") == pytest.approx(1.0, rel=1e-6)


def test_kpa_to_inhg() -> None:
    """kPa → inHg uses the same INHG_PER_MBAR constant * 10."""
    # 101.325 kPa is standard atmosphere; should be ~29.921 inHg
    result = convert(101.325, "kPa", "inHg")
    assert result is not None
    assert result == pytest.approx(INHG_PER_MBAR * 10.0 * 101.325, rel=1e-6)


def test_speed2_conversion() -> None:
    """speed2 units use the same factors as speed."""
    result_speed2 = convert(60.0, "mile_per_hour2", "km_per_hour2")
    result_speed = convert(60.0, "mile_per_hour", "km_per_hour")
    assert result_speed2 == pytest.approx(result_speed, rel=1e-9)


def test_metricwx_system_has_mm() -> None:
    """MetricWX (17) uses mm for rain, not cm."""
    assert UNIT_SYSTEMS[17]["group_rain"] == "mm"
    assert UNIT_SYSTEMS[16]["group_rain"] == "cm"


def test_transform_record_radiation_passthrough() -> None:
    """Radiation observation (W/m²) stays in same unit for US and Metric."""
    targets = {"group_radiation": "watt_per_meter_squared"}
    transformer = UnitTransformer(target_units=targets)
    record = {"radiation": 500.0}
    result = transformer.transform_record(record, us_units=1)
    rad = result["radiation"]
    assert isinstance(rad, dict)
    assert rad["value"] == pytest.approx(500.0)
    assert rad["label"] == " W/m²"
    assert rad["formatted"] == "500.0"


def test_transform_field_none_value() -> None:
    """None raw_value → N/A output, no crash."""
    transformer = UnitTransformer(target_units={"group_temperature": "degree_C"})
    result = transformer.transform_field("outTemp", None, "degree_F")
    assert result["value"] is None
    assert result["formatted"] == "N/A"


def test_transform_field_non_numeric_string() -> None:
    """Non-numeric string from MQTT returns value=None without raising."""
    transformer = UnitTransformer(target_units={"group_temperature": "degree_C"})
    result = transformer.transform_field("outTemp", "N/A", "degree_F")
    assert result["value"] is None


# ---------------------------------------------------------------------------
# transform_field() — pass-through groups (no target unit configured)
# ---------------------------------------------------------------------------


def test_transform_field_radiation_passthrough_formats() -> None:
    """Radiation field with no target unit configured gets formatted (not raw float).

    Regression: before the fix, transform_field() returned str(raw_value) for
    groups with no target unit, leaking unrounded floats (e.g. '104.043') to
    the SSE stream while the REST path returned formatted values.
    """
    # No radiation target configured — group_radiation has no entry.
    transformer = UnitTransformer(target_units={"group_temperature": "degree_C"})
    result = transformer.transform_field(
        "radiation", "104.043", "watt_per_meter_squared"
    )
    assert result["value"] == pytest.approx(104.043, rel=1e-6)
    # watt_per_meter_squared format is "%.1f" → "104.0", not "104.043"
    assert result["formatted"] == "104.0"
    assert result["label"] == " W/m²"


def test_transform_field_humidity_passthrough_formats() -> None:
    """Humidity (group_percent) with no target unit: formats as integer percent."""
    transformer = UnitTransformer(target_units={"group_temperature": "degree_C"})
    result = transformer.transform_field("outHumidity", "65.0", "percent")
    assert result["value"] == pytest.approx(65.0, rel=1e-6)
    # percent format is "%.0f" → "65"
    assert result["formatted"] == "65"
    assert result["label"] == "%"


def test_transform_field_uv_passthrough_formats() -> None:
    """UV index (group_uv) with no target unit: formats to one decimal place."""
    transformer = UnitTransformer(target_units={"group_temperature": "degree_C"})
    result = transformer.transform_field("UV", "3.7", "uv_index")
    assert result["value"] == pytest.approx(3.7, rel=1e-6)
    # uv_index format is "%.1f" → "3.7"
    assert result["formatted"] == "3.7"


def test_transform_field_passthrough_respects_format_override() -> None:
    """Pass-through path honours operator format_overrides."""
    overrides = {"watt_per_meter_squared": "%.2f"}
    transformer = UnitTransformer(
        target_units={"group_temperature": "degree_C"},
        format_overrides=overrides,
    )
    result = transformer.transform_field(
        "radiation", "104.043", "watt_per_meter_squared"
    )
    assert result["formatted"] == "104.04"

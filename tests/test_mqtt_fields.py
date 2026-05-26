"""Tests for mqtt_fields — suffix stripping, packet normalisation, conversion.

Covers strip_suffix(), normalize_packet(), and convert_mqtt_packet().
All numeric comparisons use pytest.approx() to match floating-point precision
without requiring exact bit equality.
"""

from __future__ import annotations

import pytest

from weewx_clearskies_realtime.mqtt_fields import (
    convert_mqtt_packet,
    normalize_packet,
    strip_suffix,
)


# ---------------------------------------------------------------------------
# strip_suffix — suffixed fields
# ---------------------------------------------------------------------------


def test_strip_suffix_temperature() -> None:
    assert strip_suffix("outTemp_F") == ("outTemp", "degree_F")


def test_strip_suffix_speed() -> None:
    assert strip_suffix("windSpeed_mph") == ("windSpeed", "mile_per_hour")


def test_strip_suffix_pressure() -> None:
    assert strip_suffix("barometer_inHg") == ("barometer", "inHg")


def test_strip_suffix_rain() -> None:
    assert strip_suffix("rain_in") == ("rain", "inch")


def test_strip_suffix_rain_rate() -> None:
    assert strip_suffix("rainRate_inch_per_hour") == ("rainRate", "inch_per_hour")


def test_strip_suffix_radiation() -> None:
    assert strip_suffix("radiation_Wpm2") == ("radiation", "watt_per_meter_squared")


# ---------------------------------------------------------------------------
# strip_suffix — suffix-less fields (no suffix matched)
# ---------------------------------------------------------------------------


def test_strip_suffix_no_suffix_winddir() -> None:
    assert strip_suffix("windDir") == ("windDir", None)


def test_strip_suffix_no_suffix_humidity() -> None:
    assert strip_suffix("outHumidity") == ("outHumidity", None)


def test_strip_suffix_no_suffix_uv() -> None:
    assert strip_suffix("UV") == ("UV", None)


def test_strip_suffix_no_suffix_datetime() -> None:
    assert strip_suffix("dateTime") == ("dateTime", None)


# ---------------------------------------------------------------------------
# strip_suffix — metric unit suffixes
# ---------------------------------------------------------------------------


def test_strip_suffix_metric_c() -> None:
    assert strip_suffix("outTemp_C") == ("outTemp", "degree_C")


def test_strip_suffix_metric_kph() -> None:
    assert strip_suffix("windSpeed_kph") == ("windSpeed", "km_per_hour")


def test_strip_suffix_metric_mm() -> None:
    assert strip_suffix("rain_mm") == ("rain", "mm")


def test_strip_suffix_metric_mbar() -> None:
    assert strip_suffix("barometer_mbar") == ("barometer", "mbar")


# ---------------------------------------------------------------------------
# strip_suffix — longest-suffix-first ordering (ambiguity guards)
# ---------------------------------------------------------------------------


def test_strip_suffix_meter_per_second() -> None:
    """meter_per_second must not accidentally match meter_per_second2 fields."""
    assert strip_suffix("windGust_meter_per_second") == ("windGust", "meter_per_second")


def test_strip_suffix_meter_per_second2() -> None:
    """meter_per_second2 is tried before meter_per_second."""
    assert strip_suffix("windGust_meter_per_second2") == ("windGust", "meter_per_second2")


def test_strip_suffix_km_per_hour2() -> None:
    """km_per_hour2 is tried before km_per_hour (longer first)."""
    assert strip_suffix("windGust_km_per_hour2") == ("windGust", "km_per_hour2")


def test_strip_suffix_knot2() -> None:
    """knot2 is tried before knot."""
    assert strip_suffix("windGust_knot2") == ("windGust", "knot2")


def test_strip_suffix_inch_per_hour_beats_inch() -> None:
    """inch_per_hour is tried before 'in' so rainRate strips correctly."""
    # rainRate_inch_per_hour: longest match wins
    assert strip_suffix("rainRate_inch_per_hour") == ("rainRate", "inch_per_hour")


def test_strip_suffix_empty_base_not_matched() -> None:
    """A field whose name IS the suffix must not strip to empty base."""
    # "mph" alone — stripping "_mph" would leave empty base; return as-is.
    assert strip_suffix("mph") == ("mph", None)


# ---------------------------------------------------------------------------
# normalize_packet
# ---------------------------------------------------------------------------


def test_normalize_packet() -> None:
    packet = {
        "outTemp_F":    "72.5",
        "windSpeed_mph": "5.2",
        "windDir":      "241",
        "outHumidity":  "65",
        "UV":           "3.2",
        "dateTime":     "1716739200",
        "usUnits":      "1",
    }
    result = normalize_packet(packet)

    assert result["outTemp"]      == ("72.5", "degree_F")
    assert result["windSpeed"]    == ("5.2",  "mile_per_hour")
    assert result["windDir"]      == ("241",  None)
    assert result["outHumidity"]  == ("65",   None)
    assert result["UV"]           == ("3.2",  None)
    assert result["dateTime"]     == ("1716739200", None)
    assert result["usUnits"]      == ("1",    None)


def test_normalize_packet_pressure_suffix() -> None:
    """Pressure fields with full-unit suffix are normalised correctly."""
    packet = {"barometer_inHg": "29.92", "rainRate_inch_per_hour": "0.05"}
    result = normalize_packet(packet)
    assert result["barometer"]  == ("29.92", "inHg")
    assert result["rainRate"]   == ("0.05",  "inch_per_hour")


# ---------------------------------------------------------------------------
# convert_mqtt_packet — full pipeline integration
# ---------------------------------------------------------------------------


def test_convert_mqtt_packet_us_to_metric() -> None:
    """US MQTT packet with target group_temperature=degree_C converts F→C."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    transformer = UnitTransformer(
        target_units={
            "group_temperature": "degree_C",
            "group_speed":       "mile_per_hour",
            "group_pressure":    "inHg",
            "group_percent":     "percent",
            "group_direction":   "degree_compass",
        }
    )
    packet = {
        "outTemp_F":   "72.5",
        "windSpeed_mph": "5.2",
        "windDir":     "241",
        "outHumidity": "65",
        "dateTime":    "1716739200",
        "usUnits":     "1",
    }
    result = convert_mqtt_packet(packet, transformer)

    # Temperature: 72.5 °F → (72.5 - 32) / 1.8 = 22.5 °C
    assert result["outTemp"]["value"] == pytest.approx(22.5, rel=1e-3)
    assert "C" in result["outTemp"]["label"]

    # Wind speed — same unit, value unchanged
    assert result["windSpeed"]["value"] == pytest.approx(5.2, rel=1e-3)

    # Wind direction — suffix-less, uses group_direction → compass label
    assert result["windDir"]["label"]  # non-empty compass label string (e.g. "WSW")

    # Humidity — percent→percent, value passes through
    assert result["outHumidity"]["value"] == pytest.approx(65.0, rel=1e-3)

    # dateTime — metadata, passed through as raw string
    assert result["dateTime"] == "1716739200"

    # usUnits is omitted from the output
    assert "usUnits" not in result


def test_convert_mqtt_packet_datetime_passthrough() -> None:
    """dateTime is always emitted as a raw string; usUnits and interval are dropped."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    transformer = UnitTransformer(target_units={"group_temperature": "degree_C"})
    packet = {
        "outTemp_F": "68.0",
        "dateTime":  "1716739200",
        "usUnits":   "1",
        "interval":  "5",
    }
    result = convert_mqtt_packet(packet, transformer)

    assert result["dateTime"] == "1716739200"
    assert "usUnits" not in result
    assert "interval" not in result


def test_convert_mqtt_packet_unknown_field() -> None:
    """Unknown field (not in OBS_GROUP, no suffix) is passed through raw."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    transformer = UnitTransformer(target_units={"group_temperature": "degree_F"})
    packet = {"custom_field": "42", "usUnits": "1"}
    result = convert_mqtt_packet(packet, transformer)

    assert result["custom_field"]["value"] == pytest.approx(42.0)
    assert result["custom_field"]["label"] == ""


def test_convert_mqtt_packet_unknown_field_non_numeric() -> None:
    """Unknown non-numeric field is passed through with value=None."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    transformer = UnitTransformer(target_units={"group_temperature": "degree_F"})
    packet = {"station_type": "VantagePro", "usUnits": "1"}
    result = convert_mqtt_packet(packet, transformer)

    assert result["station_type"]["value"] is None
    assert result["station_type"]["formatted"] == "VantagePro"


def test_convert_mqtt_packet_malformed_us_units() -> None:
    """Malformed usUnits defaults to US (1) without crashing."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    transformer = UnitTransformer(
        target_units={"group_temperature": "degree_C", "group_direction": "degree_compass"}
    )
    # windDir has no suffix; with malformed usUnits, should still process via default US
    packet = {"windDir": "180", "usUnits": "bad_value"}
    result = convert_mqtt_packet(packet, transformer)

    # windDir is in OBS_GROUP → should be converted via get_source_unit("windDir", 1)
    assert result["windDir"]["value"] == pytest.approx(180.0)


def test_convert_mqtt_packet_pressure_suffix() -> None:
    """barometer_inHg: suffix 'inHg' correctly identifies source unit."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    transformer = UnitTransformer(target_units={"group_pressure": "mbar"})
    packet = {"barometer_inHg": "29.92", "usUnits": "1"}
    result = convert_mqtt_packet(packet, transformer)

    # 29.92 inHg → mbar ~ 1013.25
    assert result["barometer"]["value"] == pytest.approx(29.92 / 0.0295299875, rel=1e-3)
    assert "mb" in result["barometer"]["label"] or "hPa" in result["barometer"]["label"] or "mbar" in result["barometer"]["label"]


def test_convert_mqtt_packet_metric_mode() -> None:
    """Metric MQTT packet (usUnits=16) with suffix-less fields uses Metric source."""
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

    # Target: keep metric units (Celsius) — no conversion for °C→°C
    transformer = UnitTransformer(
        target_units={
            "group_temperature": "degree_C",
            "group_percent":     "percent",
            "group_direction":   "degree_compass",
        }
    )
    # In metric mode the extension might send outTemp_C (suffixed)
    # or outTemp (suffix-less, usUnits=16). Test suffix-less path.
    packet = {
        "outTemp":     "20.0",   # no suffix — look up via usUnits=16
        "outHumidity": "70",
        "windDir":     "90",
        "usUnits":     "16",
    }
    result = convert_mqtt_packet(packet, transformer)

    # outTemp source unit (usUnits=16) → degree_C; target degree_C → identity
    assert result["outTemp"]["value"] == pytest.approx(20.0)
    assert "C" in result["outTemp"]["label"]

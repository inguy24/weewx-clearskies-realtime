"""Strip MQTT unit suffixes and identify source units for conversion.

weewx-mqtt appends unit suffixes to field names when append_units_label = True
(e.g. outTemp_F, windSpeed_mph, barometer_inHg).  The suffix comes from the
UNIT_REDUCTIONS dict in the weewx-mqtt extension; units not in that dict use
the full unit name as suffix (e.g. barometer_inHg, rainRate_inch_per_hour).

Fields whose unit maps to None in UNIT_REDUCTIONS have NO suffix
(e.g. windDir, outHumidity, UV, dateTime).

This module:
  - Strips suffixes and identifies source units.
  - Normalises a whole MQTT packet (dict of suffixed-name → value-string).
  - Converts a normalised packet to display units via a UnitTransformer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from weewx_clearskies_realtime.units.transformer import UnitTransformer

from weewx_clearskies_realtime.units.groups import get_source_unit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Suffix → unit mapping
# ---------------------------------------------------------------------------

# Built from UNIT_REDUCTIONS (reversed) + all unit names that use themselves
# as suffix (units not present in UNIT_REDUCTIONS).
#
# Where a suffix could ambiguously map to more than one unit, the more
# specific (longer) suffix wins via _SORTED_SUFFIXES below.
_SUFFIX_TO_UNIT: dict[str, str] = {
    # From UNIT_REDUCTIONS (reversed) — shortened suffix → full unit name
    "F":    "degree_F",
    "C":    "degree_C",
    "in":   "inch",
    "mph":  "mile_per_hour",
    "kph":  "km_per_hour",
    "knot": "knot",
    "knot2": "knot2",
    "mps":  "meter_per_second",
    "Wpm2": "watt_per_meter_squared",
    # Units not in UNIT_REDUCTIONS — full unit name is the suffix
    "inHg":          "inHg",
    "mbar":          "mbar",
    "hPa":           "hPa",
    "kPa":           "kPa",
    "inHg_per_hour": "inHg_per_hour",
    "mbar_per_hour": "mbar_per_hour",
    "hPa_per_hour":  "hPa_per_hour",
    "kPa_per_hour":  "kPa_per_hour",
    "inch_per_hour": "inch_per_hour",
    "cm_per_hour":   "cm_per_hour",
    "mm_per_hour":   "mm_per_hour",
    "cm":            "cm",
    "mm":            "mm",
    "foot":          "foot",
    "meter":         "meter",
    "mile":          "mile",
    "km":            "km",
    "km_per_hour":   "km_per_hour",
    "km_per_hour2":  "km_per_hour2",
    "meter_per_second":  "meter_per_second",
    "meter_per_second2": "meter_per_second2",
}

# Pre-sorted by suffix length, longest first.  This ensures that
# "inch_per_hour" is tried before "inch" so "rain_inch_per_hour" strips
# correctly rather than matching the shorter "in" suffix on a later entry.
_SORTED_SUFFIXES: list[tuple[str, str]] = sorted(
    _SUFFIX_TO_UNIT.items(), key=lambda x: len(x[0]), reverse=True
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def strip_suffix(field_name: str) -> tuple[str, str | None]:
    """Strip a unit suffix from an MQTT field name.

    Args:
        field_name: Raw MQTT field name (e.g. "outTemp_F", "windDir").

    Returns:
        (base_name, source_unit) tuple.
        source_unit is None if no known suffix matched.

    Examples:
        strip_suffix("outTemp_F")            → ("outTemp",   "degree_F")
        strip_suffix("barometer_inHg")       → ("barometer", "inHg")
        strip_suffix("rainRate_inch_per_hour") → ("rainRate", "inch_per_hour")
        strip_suffix("windDir")              → ("windDir",   None)
        strip_suffix("outHumidity")          → ("outHumidity", None)
        strip_suffix("dateTime")             → ("dateTime",  None)
    """
    for suffix, unit in _SORTED_SUFFIXES:
        tag = f"_{suffix}"
        if field_name.endswith(tag):
            base = field_name[: -len(tag)]
            if base:  # Guard against a field name that IS just the suffix
                return base, unit
    return field_name, None


def normalize_packet(packet: dict[str, Any]) -> dict[str, tuple[Any, str | None]]:
    """Normalise an entire MQTT packet dict.

    Args:
        packet: Raw MQTT payload {suffixed_field_name: value_string}.

    Returns:
        Dict mapping base_field_name → (raw_value, source_unit).
        source_unit is None for suffix-less fields (windDir, outHumidity, …).
    """
    result: dict[str, tuple[Any, str | None]] = {}
    for field_name, value in packet.items():
        base_name, source_unit = strip_suffix(field_name)
        result[base_name] = (value, source_unit)
    return result


def convert_mqtt_packet(
    packet: dict[str, Any],
    transformer: "UnitTransformer",
) -> dict[str, Any]:
    """Convert a raw MQTT packet to display-unit values.

    Strips unit suffixes, resolves source units, applies UnitTransformer,
    and returns a dict ready for SSE broadcast.

    Args:
        packet:      Raw MQTT payload {suffixed_field: value_string}.
        transformer: Configured UnitTransformer (from app.py lifespan).

    Returns:
        Dict of base_field → {value, label, formatted} for weather
        observations, plus metadata:
          - "dateTime" is passed through as a raw string.
          - "usUnits" and "interval" are omitted.
          - Unknown fields (not in OBS_GROUP and no suffix) are passed
            through as {"value": float|None, "label": "", "formatted": str}.
    """
    normalized = normalize_packet(packet)

    # usUnits is needed for suffix-less fields to look up their source unit.
    us_units_raw, _ = normalized.get("usUnits", (None, None))
    try:
        us_units = int(float(us_units_raw)) if us_units_raw is not None else 1
    except (ValueError, TypeError):
        us_units = 1  # default to US units if the value is malformed

    result: dict[str, Any] = {}

    for base_name, (raw_value, source_unit) in normalized.items():
        # --- Metadata fields ---
        if base_name == "dateTime":
            result[base_name] = raw_value
            continue
        if base_name in ("usUnits", "interval"):
            continue

        # --- Suffixed fields: source unit came from the suffix ---
        if source_unit is not None:
            result[base_name] = transformer.transform_field(base_name, raw_value, source_unit)
            continue

        # --- Suffix-less fields: look up source unit from usUnits ---
        src = get_source_unit(base_name, us_units)
        if src is not None:
            result[base_name] = transformer.transform_field(base_name, raw_value, src)
        else:
            # Unknown observation — pass through raw so the browser still
            # receives it rather than silently dropping it.
            try:
                result[base_name] = {
                    "value": float(raw_value),
                    "label": "",
                    "formatted": str(raw_value),
                }
            except (ValueError, TypeError):
                result[base_name] = {
                    "value": None,
                    "label": "",
                    "formatted": str(raw_value) if raw_value is not None else "N/A",
                }

    return result

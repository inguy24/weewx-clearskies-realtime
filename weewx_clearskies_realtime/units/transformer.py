"""High-level unit transformer.

UnitTransformer converts raw weather data dicts (from the REST archive path
or the MQTT field path) into display-ready dicts with converted values,
labels, and formatted strings.

Design notes:
- Stateless per-call; the transformer itself holds only configuration.
- Both transform_record() (REST/archive path) and transform_field() (MQTT path)
  return the same output shape: {"value": float|None, "label": str, "formatted": str}.
- transform_field() accepts str because MQTT sends all values as strings.
"""

from __future__ import annotations

from .conversion import convert
from .derived import beaufort, comfort_index
from .groups import OBS_GROUP, UNIT_SYSTEMS, VALID_UNITS, get_source_unit  # noqa: F401
from .labels import format_value, get_label

# Metadata fields in archive records that carry no physical unit.
_METADATA_FIELDS: frozenset[str] = frozenset({"dateTime", "usUnits", "interval"})


def _extract_value(entry: object) -> float | None:
    """Extract a numeric value from a converted record entry or a raw number.

    Handles both the dict shape {"value": float, ...} (converted entries) and
    plain int/float values (raw archive records). Returns None for None, missing
    keys, or non-numeric values.
    """
    if entry is None:
        return None
    if isinstance(entry, dict):
        val = entry.get("value")
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
    try:
        return float(entry)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


# Default 16-point compass ordinate labels (weewx default order).
_DEFAULT_ORDINATES: list[str] = [
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
]


class UnitTransformer:
    """Transforms raw weather values to operator display units.

    Args:
        target_units:   group_name → target unit string.
                        Keys are group names (e.g. "group_temperature"),
                        values are unit strings (e.g. "degree_C").
        label_overrides:   unit → label override (from operator [[Labels]]).
        format_overrides:  unit → format string override (from [[StringFormats]]).
        ordinates:         16 compass direction labels, N through NNW.
    """

    def __init__(
        self,
        target_units: dict[str, str],
        label_overrides: dict[str, str] | None = None,
        format_overrides: dict[str, str] | None = None,
        ordinates: list[str] | None = None,
    ) -> None:
        # Validate every target unit against the known valid-unit set for its group.
        for group, unit in target_units.items():
            if group in VALID_UNITS and unit not in VALID_UNITS[group]:
                raise ValueError(f"Invalid unit '{unit}' for {group}")
        self._targets = target_units
        self._label_overrides = label_overrides
        self._format_overrides = format_overrides
        self._ordinates = ordinates if ordinates is not None else _DEFAULT_ORDINATES

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def transform_record(self, data: dict[str, object], us_units: int) -> dict[str, object]:
        """Transform an archive record dict from the REST API.

        Args:
            data:     observation_name → raw_value (values may be None).
            us_units: unit-system code (1=US, 16=Metric, 17=MetricWX).

        Returns:
            dict where known observations become
            {"value": float|None, "label": str, "formatted": str}
            and unknown / metadata fields are passed through unchanged.
        """
        result: dict[str, object] = {}

        for obs, raw_value in data.items():
            if obs in _METADATA_FIELDS:
                continue

            group = OBS_GROUP.get(obs)
            if group is None:
                # Unknown observation — pass raw value through.
                result[obs] = raw_value
                continue

            target_unit = self._targets.get(group)
            if target_unit is None:
                # No target configured for this group — pass raw through.
                result[obs] = raw_value
                continue

            source_unit = get_source_unit(obs, us_units)

            if source_unit is None or raw_value is None:
                result[obs] = {
                    "value": None,
                    "label": get_label(target_unit, self._label_overrides),
                    "formatted": "N/A",
                }
                continue

            # Wind direction: degrees are degrees; format as compass label.
            if group == "group_direction":
                assert isinstance(raw_value, (int, float))
                deg = float(raw_value)
                compass = self._direction_label(deg)
                result[obs] = {"value": deg, "label": compass, "formatted": compass}
                continue

            assert isinstance(raw_value, (int, float))
            converted = convert(float(raw_value), source_unit, target_unit)
            assert converted is not None
            result[obs] = {
                "value": converted,
                "label": get_label(target_unit, self._label_overrides),
                "formatted": format_value(converted, target_unit, self._format_overrides),
            }

        # --- Derived fields ---
        # Computed from raw values + us_units so we work in the original unit
        # system rather than back-converting from display units.

        raw_wind = data.get("windSpeed")
        if raw_wind is not None:
            src_wind = get_source_unit("windSpeed", us_units)
            if src_wind is not None:
                try:
                    result["beaufort"] = beaufort(float(raw_wind), src_wind)
                except (ValueError, TypeError):
                    pass

        raw_temp = data.get("outTemp")
        if raw_temp is not None:
            src_temp = get_source_unit("outTemp", us_units)
            if src_temp is not None:
                try:
                    result["comfortIndex"] = comfort_index(float(raw_temp), src_temp)
                except (ValueError, TypeError):
                    pass

        # --- Sky condition and weatherText (ADR-044) ---
        # Lazy imports to avoid circular dependency (conditions_text →
        # units.conversion → units.__init__ → transformer → conditions_text).
        from .. import sky_condition as _sky
        from ..conditions_text import build_weather_text as _build_wt

        # radiation group is always watt_per_meter_squared across all unit
        # systems, so raw values == display values. Safe to feed into the
        # sky_condition buffer from either path.
        raw_radiation = data.get("radiation")
        raw_max_solar = data.get("maxSolarRad")
        if raw_radiation is not None and raw_max_solar is not None:
            try:
                _sky.update(float(raw_radiation), float(raw_max_solar))
            except (ValueError, TypeError):
                pass

        sky = _sky.classify()

        # Extract display-unit values for weatherText components.
        wind_val = _extract_value(result.get("windSpeed"))
        target_speed = self._targets.get("group_speed")
        dewpoint_val = _extract_value(result.get("dewpoint"))
        target_temp = self._targets.get("group_temperature")
        rain_rate_val = _extract_value(result.get("rainRate"))
        target_rainrate = self._targets.get("group_rainrate")

        result["weatherText"] = {
            "value": _build_wt(
                sky=sky,
                rain_rate=rain_rate_val,
                rain_rate_unit=target_rainrate if target_rainrate else "inch_per_hour",
                wind_speed=wind_val,
                wind_speed_unit=target_speed if target_speed else "mile_per_hour",
                dewpoint=dewpoint_val,
                dewpoint_unit=target_temp if target_temp else "degree_F",
            ),
            "label": "",
            "formatted": "",
        }

        return result

    def transform_field(
        self,
        obs_name: str,
        raw_value: float | str | None,
        source_unit: str,
    ) -> dict[str, object]:
        """Transform a single field with known source unit (MQTT path).

        Args:
            obs_name:    observation name (e.g. "outTemp").
            raw_value:   raw value; may be a string because MQTT sends strings.
            source_unit: source unit (e.g. "degree_F").

        Returns:
            {"value": float|None, "label": str, "formatted": str}
        """
        group = OBS_GROUP.get(obs_name)
        target_unit = self._targets.get(group, "") if group else ""

        if raw_value is None:
            return {
                "value": None,
                "label": get_label(target_unit, self._label_overrides),
                "formatted": "N/A",
            }

        # Parse string → float (MQTT sends everything as strings).
        try:
            numeric = float(raw_value)
        except (ValueError, TypeError):
            return {"value": None, "label": "", "formatted": str(raw_value)}

        if group is None:
            return {"value": numeric, "label": "", "formatted": str(raw_value)}

        if not target_unit:
            return {"value": numeric, "label": "", "formatted": str(raw_value)}

        # Wind direction special case.
        if group == "group_direction":
            compass = self._direction_label(numeric)
            return {"value": numeric, "label": compass, "formatted": compass}

        converted = convert(numeric, source_unit, target_unit)
        assert converted is not None
        return {
            "value": converted,
            "label": get_label(target_unit, self._label_overrides),
            "formatted": format_value(converted, target_unit, self._format_overrides),
        }

    def add_derived_fields(self, record: dict[str, object]) -> None:
        """Add Beaufort and comfortIndex from already-converted display values.

        Mutates *record* in place.  Called after all individual fields have
        been transformed (MQTT path), so values are already in display units.

        Args:
            record: Converted record dict (field → ConvertedValue dict or raw).
        """
        wind_entry = record.get("windSpeed")
        if isinstance(wind_entry, dict) and wind_entry.get("value") is not None:
            target_speed = self._targets.get("group_speed")
            if target_speed is not None:
                try:
                    record["beaufort"] = beaufort(
                        float(wind_entry["value"]),  # type: ignore[arg-type]
                        target_speed,
                    )
                except (ValueError, TypeError):
                    pass

        temp_entry = record.get("outTemp")
        if isinstance(temp_entry, dict) and temp_entry.get("value") is not None:
            target_temp = self._targets.get("group_temperature")
            if target_temp is not None:
                try:
                    record["comfortIndex"] = comfort_index(
                        float(temp_entry["value"]),  # type: ignore[arg-type]
                        target_temp,
                    )
                except (ValueError, TypeError):
                    pass

        # --- Sky condition and weatherText (ADR-044) ---
        from .. import sky_condition as _sky
        from ..conditions_text import build_weather_text as _build_wt

        # radiation is always watt_per_meter_squared; display value == raw
        # value. Update the rolling buffer from the already-converted record.
        rad_val = _extract_value(record.get("radiation"))
        max_val = _extract_value(record.get("maxSolarRad"))
        if rad_val is not None and max_val is not None:
            try:
                _sky.update(rad_val, max_val)
            except (ValueError, TypeError):
                pass

        sky = _sky.classify()

        wind_val = _extract_value(record.get("windSpeed"))
        target_speed_unit = self._targets.get("group_speed")
        dewpoint_val = _extract_value(record.get("dewpoint"))
        target_temp_unit = self._targets.get("group_temperature")
        rain_rate_val = _extract_value(record.get("rainRate"))
        target_rainrate_unit = self._targets.get("group_rainrate")

        record["weatherText"] = {
            "value": _build_wt(
                sky=sky,
                rain_rate=rain_rate_val,
                rain_rate_unit=target_rainrate_unit if target_rainrate_unit else "inch_per_hour",
                wind_speed=wind_val,
                wind_speed_unit=target_speed_unit if target_speed_unit else "mile_per_hour",
                dewpoint=dewpoint_val,
                dewpoint_unit=target_temp_unit if target_temp_unit else "degree_F",
            ),
            "label": "",
            "formatted": "",
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _direction_label(self, degrees: float) -> str:
        """Convert compass degrees to the appropriate ordinate label."""
        # Divides the circle into 16 equal 22.5° sectors; offset by 11.25° so
        # that N spans 348.75°–11.25° rather than 0°–22.5°.
        idx = int((degrees + 11.25) / 22.5) % 16
        return self._ordinates[idx]

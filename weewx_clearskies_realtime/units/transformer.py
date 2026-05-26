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
from .groups import OBS_GROUP, UNIT_SYSTEMS, VALID_UNITS, get_source_unit  # noqa: F401
from .labels import format_value, get_label

# Metadata fields in archive records that carry no physical unit.
_METADATA_FIELDS: frozenset[str] = frozenset({"dateTime", "usUnits", "interval"})

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

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _direction_label(self, degrees: float) -> str:
        """Convert compass degrees to the appropriate ordinate label."""
        # Divides the circle into 16 equal 22.5° sectors; offset by 11.25° so
        # that N spans 348.75°–11.25° rather than 0°–22.5°.
        idx = int((degrees + 11.25) / 22.5) % 16
        return self._ordinates[idx]

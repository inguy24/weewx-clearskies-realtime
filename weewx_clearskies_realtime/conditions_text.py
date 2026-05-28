"""Current conditions text composer (ADR-044).

Assembles weatherText from sky condition, precipitation, wind (Beaufort),
and comfort (dewpoint) components. Each component is independently nullable;
absent components are dropped from the composed string.

Module-level state is held only in sky_condition — this module is stateless.
"""

from __future__ import annotations

from . import sky_condition as _sky_condition_module
from .units.conversion import convert
from .units.derived import beaufort

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _precip_label(rain_rate: float | None, source_unit: str) -> str | None:
    """Classify precipitation from rain rate (AMS/WMO thresholds).

    Thresholds are applied in in/hr:
      < 0.10 → Light Rain
      0.10–0.30 → Moderate Rain
      > 0.30 → Heavy Rain

    Returns None when rain_rate is None or ≤ 0.
    """
    if rain_rate is None or rain_rate <= 0:
        return None

    # Convert to in/hr for canonical threshold comparison.
    if source_unit == "inch_per_hour":
        rate_inhr = rain_rate
    else:
        converted = convert(rain_rate, source_unit, "inch_per_hour")
        if converted is None:
            return None
        rate_inhr = converted

    if rate_inhr < 0.10:
        return "Light Rain"
    if rate_inhr < 0.30:
        return "Moderate Rain"
    return "Heavy Rain"


def _comfort_label(dewpoint: float | None, source_unit: str) -> str | None:
    """Dewpoint-based comfort descriptor (NWS scale).

    Thresholds applied in °F:
      60–64 → Humid
      65–69 → Very Humid
      70–74 → Oppressive
      ≥ 75  → Miserable
      < 60  → comfortable, return None (omitted from text)

    Returns None when dewpoint is None or comfortable (< 60 °F).
    """
    if dewpoint is None:
        return None

    if source_unit == "degree_F":
        dp_f = dewpoint
    else:
        converted = convert(dewpoint, source_unit, "degree_F")
        if converted is None:
            return None
        dp_f = converted

    if dp_f >= 75:
        return "Miserable"
    if dp_f >= 70:
        return "Oppressive"
    if dp_f >= 65:
        return "Very Humid"
    if dp_f >= 60:
        return "Humid"
    return None


def _compose(parts: list[str | None]) -> str:
    """Join non-empty parts into a natural-language string (Oxford comma).

    Rules per ADR-044 §6 composition table:
      1 part  → "{a}"
      2 parts → "{a} and {b}"
      3+ parts → "{a}, {b}, ... and {last}"
    """
    filtered = [p for p in parts if p]
    if not filtered:
        return ""
    if len(filtered) == 1:
        return filtered[0]
    if len(filtered) == 2:
        return f"{filtered[0]} and {filtered[1]}"
    return ", ".join(filtered[:-1]) + f", and {filtered[-1]}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_weather_text(
    *,
    sky: str | None = None,
    rain_rate: float | None = None,
    rain_rate_unit: str = "inch_per_hour",
    wind_speed: float | None = None,
    wind_speed_unit: str = "mile_per_hour",
    dewpoint: float | None = None,
    dewpoint_unit: str = "degree_F",
    provider_sky: str | None = None,
) -> str:
    """Build the full weatherText string (ADR-044).

    Components are assembled in priority order: sky, precipitation, wind,
    comfort. Null/absent components are dropped. Calm wind (Beaufort 0) is
    omitted to keep the text clean (ADR-044 §4).

    Args:
        sky:             Sky condition from sky_condition.classify() (may be
                         None if night/startup — falls back to provider_sky).
        rain_rate:       Rain rate value.
        rain_rate_unit:  Unit of rain_rate (default "inch_per_hour").
        wind_speed:      Wind speed value.
        wind_speed_unit: Unit of wind_speed (default "mile_per_hour").
        dewpoint:        Dewpoint value.
        dewpoint_unit:   Unit of dewpoint (default "degree_F").
        provider_sky:    Provider weather text, used as fallback when the
                         local solar analysis produces None.

    Returns:
        Composed conditions text, e.g. "Partly Cloudy and Moderate breeze",
        or "" when no components are available.
    """
    parts: list[str | None] = []

    # Sky condition: use local solar classification only during daytime.
    # At night, fall back to provider sky data (ADR-044 §1b).
    if sky is not None and _sky_condition_module.is_daytime():
        effective_sky = sky
    else:
        effective_sky = provider_sky
    parts.append(effective_sky)

    # Precipitation.
    parts.append(_precip_label(rain_rate, rain_rate_unit))

    # Wind (Beaufort label). Beaufort 0 = Calm is omitted per ADR-044 §4.
    if wind_speed is not None:
        try:
            b = beaufort(wind_speed, wind_speed_unit)
            if b["value"] > 0:
                parts.append(str(b["label"]))
        except (ValueError, TypeError):
            pass

    # Comfort (dewpoint-based).
    parts.append(_comfort_label(dewpoint, dewpoint_unit))

    return _compose(parts)

"""Current conditions text composer (ADR-044).

Assembles weatherText from temperature-comfort, sky condition, wind (Beaufort),
and precipitation components.  Each component is independently nullable;
absent components are dropped from the composed string.

Component order (ADR-044 §9 amendment, 2026-05-28):
  [temperature-comfort, sky, wind, precipitation]

Composition uses a "with" connector for the final part to prevent double-"and"
when the temperature-comfort label is compound (e.g. "Warm and Humid").

Module-level state is held only in sky_condition and temperature_comfort —
this module is stateless.
"""

from __future__ import annotations

from . import sky_condition as _sky_condition_module
from . import temperature_comfort as _temperature_comfort
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


def _compose(parts: list[str | None]) -> str:
    """Join non-empty parts into a natural-language string (ADR-044 §9 amendment).

    Rules:
      1 part  → "{a}"
      2 parts → "{a}, with {b}"
      3+ parts → "{a}, {b}, ..., with {last}"

    This format prevents double-"and" when the first part is a compound
    temperature-comfort label like "Warm and Humid".
    """
    filtered = [p for p in parts if p]
    if not filtered:
        return ""
    if len(filtered) == 1:
        return filtered[0]
    if len(filtered) == 2:
        return f"{filtered[0]}, with {filtered[1]}"
    return ", ".join(filtered[:-1]) + f", with {filtered[-1]}"


def _to_fahrenheit(value: float | None, source_unit: str) -> float | None:
    """Convert *value* from *source_unit* to °F.  Returns None when value is None."""
    if value is None:
        return None
    if source_unit == "degree_F":
        return value
    return convert(value, source_unit, "degree_F")


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
    app_temp: float | None = None,
    dewpoint: float | None = None,
    out_temp: float | None = None,
    heatindex: float | None = None,
    windchill: float | None = None,
    temp_unit: str = "degree_F",
    dewpoint_unit: str = "degree_F",
    provider_sky: str | None = None,
) -> str:
    """Build the full weatherText string (ADR-044 §9 amendment, 2026-05-28).

    Components are assembled in priority order:
      [temperature-comfort, sky, wind, precipitation]

    Null/absent components are dropped.  Calm wind (Beaufort 0) is omitted
    to keep the text clean (ADR-044 §4).

    Smoothed inputs from ``enrichment.input_smoother`` should be passed by
    the caller when available; this function performs no smoothing itself.

    Args:
        sky:             Sky condition from sky_condition.classify() (may be
                         None if night/startup — falls back to provider_sky).
        rain_rate:       Rain rate value.
        rain_rate_unit:  Unit of rain_rate (default "inch_per_hour").
        wind_speed:      Wind speed value.
        wind_speed_unit: Unit of wind_speed (default "mile_per_hour").
        app_temp:        Apparent temperature (feels-like) value.
        dewpoint:        Dewpoint value.
        out_temp:        Dry-bulb temperature value.
        heatindex:       Heat index value.
        windchill:       Wind chill value.
        temp_unit:       Unit for app_temp, out_temp, heatindex, windchill
                         (default "degree_F").
        dewpoint_unit:   Unit of dewpoint (default "degree_F").
        provider_sky:    Provider weather text, used as fallback when the
                         local solar analysis produces None.

    Returns:
        Composed conditions text, e.g. "Warm and Humid, Partly Cloudy, with Light Rain",
        or "" when no components are available.
    """
    parts: list[str | None] = []

    # 1. Temperature-comfort (2D matrix, ADR-044 §5-7).
    # Convert all temperature inputs to °F before classifying.
    app_temp_f = _to_fahrenheit(app_temp, temp_unit)
    dewpoint_f = _to_fahrenheit(dewpoint, dewpoint_unit)
    out_temp_f = _to_fahrenheit(out_temp, temp_unit)
    heatindex_f = _to_fahrenheit(heatindex, temp_unit)
    windchill_f = _to_fahrenheit(windchill, temp_unit)

    temp_comfort_label = _temperature_comfort.classify(
        app_temp=app_temp_f,
        dewpoint=dewpoint_f,
        out_temp=out_temp_f,
        heatindex=heatindex_f,
        windchill=windchill_f,
    )
    parts.append(temp_comfort_label)

    # 2. Sky condition: use local solar classification only during daytime.
    # At night, fall back to provider sky data (ADR-044 §1b).
    if sky is not None and _sky_condition_module.is_daytime():
        effective_sky = sky
    else:
        effective_sky = provider_sky
    parts.append(effective_sky)

    # 3. Wind (Beaufort label). Beaufort 0 = Calm is omitted per ADR-044 §4.
    if wind_speed is not None:
        try:
            b = beaufort(wind_speed, wind_speed_unit)
            if b["value"] > 0:
                parts.append(str(b["label"]))
        except (ValueError, TypeError):
            pass

    # 4. Precipitation.
    parts.append(_precip_label(rain_rate, rain_rate_unit))

    return _compose(parts)

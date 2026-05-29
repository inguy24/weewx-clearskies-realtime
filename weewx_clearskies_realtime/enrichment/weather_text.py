"""Weather text enrichment for GET /api/v1/current.

Composes the weatherText string by combining smoothed sensor readings with
the sky condition classifier, then injects it into the /current response.
"""

import logging

from weewx_clearskies_realtime.conditions_text import build_weather_text
from weewx_clearskies_realtime.enrichment.input_smoother import get_smoothed
from weewx_clearskies_realtime.sky_condition import classify as sky_classify

logger = logging.getLogger(__name__)


def compose_weather_text() -> str:
    """Build the weatherText string from current smoothed values.

    Reads all smoothed values from the input_smoother ring buffers and the
    current sky classification from the 30-minute solar analysis window, then
    delegates to build_weather_text().

    All smoothed values are in US units (°F, mph, in/hr) — the weewx default
    internal unit system.  build_weather_text() handles threshold comparisons.

    Returns:
        Composed conditions text (e.g. "Warm and Humid, Partly Cloudy"), or
        "" when no components are available.
    """
    return build_weather_text(
        sky=sky_classify(),
        rain_rate=get_smoothed("rainRate"),
        rain_rate_unit="inch_per_hour",
        wind_speed=get_smoothed("windSpeed"),
        wind_speed_unit="mile_per_hour",
        app_temp=get_smoothed("appTemp"),
        dewpoint=get_smoothed("dewpoint"),
        out_temp=get_smoothed("outTemp"),
        heatindex=get_smoothed("heatindex"),
        windchill=get_smoothed("windchill"),
        temp_unit="degree_F",
        dewpoint_unit="degree_F",
    )


def enrich_weather_text(data: dict) -> dict:  # type: ignore[type-arg]
    """Inject ``weatherText`` into a /current response envelope.

    Calls compose_weather_text() for the composed string and writes it
    into the observation sub-dict (``data["data"]["weatherText"]``) so
    weatherText is co-located with all other observation fields rather than
    floating at the envelope top level.

    Placement logic:
    - When ``data["data"]`` is a dict, writes into that sub-dict.
    - Otherwise falls back to writing at the top level of *data* (e.g. when
      the upstream API returned a non-standard shape).

    Never raises: exceptions are caught, logged, and the key is set to None.
    """
    try:
        text = compose_weather_text()
        value = text or None

        obs = data.get("data")
        if isinstance(obs, dict):
            obs["weatherText"] = value
        else:
            data["weatherText"] = value
    except Exception:  # noqa: BLE001
        logger.exception("weather_text enrichment failed")
        obs = data.get("data")
        if isinstance(obs, dict):
            obs.setdefault("weatherText", None)
        else:
            data.setdefault("weatherText", None)
    return data

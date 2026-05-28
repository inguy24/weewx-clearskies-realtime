"""Weather text enrichment for GET /api/v1/current.

Composes the weatherText string by combining smoothed sensor readings with
the sky condition classifier, then injects it into the /current response.
"""

import logging

from weewx_clearskies_realtime.conditions_text import build_weather_text
from weewx_clearskies_realtime.enrichment.input_smoother import get_smoothed
from weewx_clearskies_realtime.sky_condition import classify as sky_classify

logger = logging.getLogger(__name__)


def enrich_weather_text(data: dict) -> dict:  # type: ignore[type-arg]
    """Inject ``weatherText`` into a /current response envelope.

    Uses smoothed values from the input_smoother ring buffers and the
    current sky classification from the 30-minute solar analysis window.

    All smoothed values are assumed to be in US units (°F, mph, in/hr)
    since that is the weewx default internal unit system. The
    build_weather_text() function handles conversion to canonical units
    for threshold comparisons.
    """
    try:
        text = build_weather_text(
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
        data["weatherText"] = text or None
    except Exception:  # noqa: BLE001
        logger.exception("weather_text enrichment failed")
        data.setdefault("weatherText", None)
    return data

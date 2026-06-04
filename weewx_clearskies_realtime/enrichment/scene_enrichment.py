"""Scene enrichment for GET /api/v1/current (ADR-047).

Injects a ``scene`` descriptor into the /current response envelope's ``data``
sub-dict.  The descriptor has shape::

    scene: {
        "sky":     "clear" | "cloudy" | "storm",
        "daytime": bool,
        "overlay": "rain" | "snow" | null,
    }

This enrichment runs once per GET /current request and:

1. Fetches today's almanac sunrise/sunset from the upstream API's
   /api/v1/almanac endpoint (once per day — cached in scene module).
2. Fetches the current-hour hourly forecast from /api/v1/forecast?interval=hourly
   to read ``precipType`` (the structured snow/sleet/freezing-rain field that is
   not present on the Observation model).
3. Reads the resolved sky label from the /current observation's ``weatherText``
   and from sky_condition.classify() (local solar analysis).
4. Calls scene.detect_precip() to update the server-side 15-minute linger timer.
5. Calls scene.build_scene() to build the current descriptor and injects it into
   ``data["data"]``.

The SSE stream carries the same ``scene`` field: a packet-tap processor
(scene_packet_tap.py) reads the current scene state from the module and injects
it into every loop packet before fan-out.

Never raises: all errors are caught and logged; ``scene`` is set to a default
safe value (clear/false/null) so the dashboard is never left without a background.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from weewx_clearskies_realtime import scene as scene_mod
from weewx_clearskies_realtime import sky_condition

logger = logging.getLogger(__name__)

# Upstream API paths (relative — prefixed with the proxy's upstream_url).
_ALMANAC_PATH = "/api/v1/almanac"
_FORECAST_PATH = "/api/v1/forecast"

# Fallback scene emitted when the enrichment cannot build a real descriptor.
_FALLBACK_SCENE: dict[str, object] = {
    "sky": "clear",
    "daytime": False,
    "overlay": None,
}


async def enrich_scene(data: dict[str, Any]) -> dict[str, Any]:
    """Inject ``scene`` into a /current response envelope.

    Reads cloud/sky from the sky_condition module (local solar), falls back to
    provider weatherText for sky and storm detection.  Fetches precipType from
    the first current hourly forecast point.  Fetches today's almanac
    sunrise/sunset.  Updates the server-side linger state then serialises.

    Never raises.  ``data["data"]["scene"]`` is always set on return.
    """
    obs = data.get("data")
    if not isinstance(obs, dict):
        # Non-observation shape — inject at top level and return.
        data["scene"] = dict(_FALLBACK_SCENE)
        return data

    try:
        await _update_sun_times()
        precip_type = await _fetch_current_precip_type()

        # Sky label: prefer local solar classifier, fall back to provider text.
        sky_label: str | None = sky_condition.classify()
        if sky_label is None:
            # Night or startup — use the provider-reported text as sky label.
            weather_text = obs.get("weatherText")
            sky_label = str(weather_text) if weather_text else None

        # Provider conditions text for storm detection.
        conditions_text = str(obs.get("weatherText") or "")

        # Local rain rate (sign only — used as a presence check, not threshold).
        rain_rate = _extract_float(obs.get("rainRate"))

        # Update server-side precip linger state.
        scene_mod.detect_precip(
            precip_type=precip_type,
            conditions_text=conditions_text,
            rain_rate=rain_rate,
        )

        descriptor = scene_mod.build_scene(sky_label)

    except Exception:  # noqa: BLE001
        logger.exception("scene enrichment failed — using fallback")
        descriptor = dict(_FALLBACK_SCENE)

    obs["scene"] = descriptor
    data["scene"] = descriptor
    return data


# ---------------------------------------------------------------------------
# Upstream API helpers
# ---------------------------------------------------------------------------


async def _update_sun_times() -> None:
    """Fetch today's almanac sunrise/sunset and cache them in scene module.

    Re-fetches only when the cached sunset has passed — NOT on UTC date
    rollover.  A UTC date rollover can occur hours before the local sunset
    (e.g. midnight UTC = 5 PM PDT, but sunset is 8 PM PDT).  Using the
    sunset as the invalidation signal ensures daytime stays True until the
    actual sunset.
    """
    if not scene_mod.sun_times_need_refresh():
        return  # Cached times still valid

    try:
        from weewx_clearskies_realtime.proxy import get_upstream_client

        client, upstream_url = get_upstream_client()
        if client is None or not upstream_url:
            return

        url = f"{upstream_url}{_ALMANAC_PATH}"
        now_utc = datetime.now(tz=UTC)
        today_str = now_utc.strftime("%Y-%m-%d")

        rise, sset = await _fetch_sun_times(client, url, today_str)
        if rise is None or sset is None:
            return

        # If today's sunrise is still in the future, we're in the pre-sunrise
        # hours of the UTC day — but it may still be yesterday's evening locally.
        # Fetch yesterday's almanac and use those times instead.
        rise_dt = datetime.fromisoformat(rise.replace("Z", "+00:00"))
        if rise_dt > now_utc:
            from datetime import timedelta
            yesterday_str = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
            yrise, ysset = await _fetch_sun_times(client, url, yesterday_str)
            if yrise is not None and ysset is not None:
                rise, sset = yrise, ysset

        scene_mod.update_sun_times(rise, sset)

    except Exception:  # noqa: BLE001
        logger.debug("almanac fetch failed — daytime unavailable", exc_info=True)


async def _fetch_sun_times(
    client: Any, url: str, date_str: str
) -> tuple[str | None, str | None]:
    """Fetch sunrise/sunset for a given date from the upstream almanac API."""
    resp = await client.get(url, params={"date": date_str})
    if resp.status_code >= 400:  # noqa: PLR2004
        logger.debug("almanac fetch for %s returned %d", date_str, resp.status_code)
        return None, None
    almanac_json = resp.json()
    sun_block = almanac_json.get("data", {}).get("sun", {})
    return sun_block.get("rise"), sun_block.get("set")


async def _fetch_current_precip_type() -> str | None:
    """Fetch precipType for the current hour from the upstream hourly forecast.

    Returns:
        Canonical precipType string ("rain", "snow", "sleet", "freezing-rain")
        or None when no provider is configured, the forecast is unavailable, or
        the current hour has no precipType.
    """
    try:
        from weewx_clearskies_realtime.proxy import get_upstream_client

        client, upstream_url = get_upstream_client()
        if client is None or not upstream_url:
            return None

        url = f"{upstream_url}{_FORECAST_PATH}"
        resp = await client.get(url, params={"hours": 1, "days": 0})
        if resp.status_code >= 400:  # noqa: PLR2004
            logger.debug("forecast fetch returned %d — precipType unavailable", resp.status_code)
            return None

        forecast_json = resp.json()
        # ForecastBundle envelope: {data: {hourly: [...], daily: [...]}, source: ..., generatedAt: ...}
        bundle = forecast_json.get("data", forecast_json)
        hourly = bundle.get("hourly", [])
        if not hourly or not isinstance(hourly, list):
            return None

        first_point = hourly[0]
        if not isinstance(first_point, dict):
            return None

        raw = first_point.get("precipType")
        return str(raw).strip().lower() if raw else None

    except Exception:  # noqa: BLE001
        logger.debug("forecast precipType fetch failed", exc_info=True)
        return None


def _extract_float(raw: object) -> float | None:
    """Extract a float from a raw observation field value or None."""
    if raw is None:
        return None
    value = raw.get("value") if isinstance(raw, dict) else raw  # type: ignore[union-attr]
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

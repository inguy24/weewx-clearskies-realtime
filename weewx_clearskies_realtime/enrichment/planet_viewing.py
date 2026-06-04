"""Planet viewing quality enrichment for GET /api/v1/almanac/planets.

Injects per-planet viewing quality fields into each planet dict in the
``data.evening``, ``data.morning``, and ``data.allNight`` lists.

Data fetched (once per call):
- ``{upstream_url}/api/v1/almanac/seeing-forecast`` — 7Timer seeing/transparency/cloud
- ``{upstream_url}/api/v1/almanac`` — Moon RA/Dec/illumination and Sun rise/set

Fields injected into each planet dict:
- ``viewingQuality``  — "excellent" | "good" | "fair" | "poor" | "not_visible" | null
- ``viewingScore``    — float 0.0–1.0 or null
- ``bestViewingTime`` — ISO-8601 string or null
- ``clearWindowStart`` — ISO-8601 string or null
- ``clearWindowEnd``   — ISO-8601 string or null
- ``conjunction``     — "Close Conjunction with Moon Tonight" | null
- ``viewingNote``     — "In Sun's Glare" | "Bright moon nearby" | null

Graceful degradation: if seeing forecast is unavailable, ALL viewing fields are null.
If almanac is unavailable, moon penalty and conjunction detection are skipped.
Individual per-planet failures set that planet's fields to null without aborting others.

Never raises — the entire function body is wrapped in try/except.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Score lookup tables (per spec)
# ---------------------------------------------------------------------------

SEEING_SCORES: dict[int, float] = {
    1: 1.0, 2: 1.0, 3: 0.85, 4: 0.65, 5: 0.45, 6: 0.25, 7: 0.10, 8: 0.10,
}

TRANSPARENCY_SCORES: dict[int, float] = {
    1: 1.0, 2: 1.0, 3: 1.0, 4: 0.7, 5: 0.7, 6: 0.3, 7: 0.3, 8: 0.1,
}

# Cloud cover threshold: above this value the planet is not visible.
_CLOUD_GATE_OCTET = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _altitude_score(alt_deg: float) -> float:
    """Translate altitude in degrees to a score component."""
    if alt_deg > 45:
        return 1.0
    if alt_deg > 30:
        return 0.85
    if alt_deg > 20:
        return 0.60
    if alt_deg > 10:
        return 0.30
    return 0.10


def _angular_distance_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Angular distance between two celestial positions, in degrees.

    Uses the spherical law of cosines.  Clamps cos_d to [-1, 1] to guard
    against floating-point precision errors that produce domain errors in acos.
    """
    ra1_r, dec1_r = math.radians(ra1), math.radians(dec1)
    ra2_r, dec2_r = math.radians(ra2), math.radians(dec2)
    cos_d = (
        math.sin(dec1_r) * math.sin(dec2_r)
        + math.cos(dec1_r) * math.cos(dec2_r) * math.cos(ra1_r - ra2_r)
    )
    cos_d = max(-1.0, min(1.0, cos_d))
    return math.degrees(math.acos(cos_d))


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 string to a UTC-aware datetime, or None."""
    if not ts:
        return None
    try:
        # Replace trailing Z with +00:00 for fromisoformat compatibility.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _nearest_forecast_point(
    forecast_points: list[dict[str, Any]],
    target_dt: datetime,
) -> dict[str, Any] | None:
    """Return the forecast point whose ``validTime`` is closest to *target_dt*.

    Returns None if the list is empty or no point has a parseable validTime.
    """
    best: dict[str, Any] | None = None
    best_delta: float = float("inf")
    for pt in forecast_points:
        pt_dt = _parse_iso(pt.get("validTime"))
        if pt_dt is None:
            continue
        delta = abs((pt_dt - target_dt).total_seconds())
        if delta < best_delta:
            best_delta = delta
            best = pt
    return best


def _score_to_rating(score: float) -> str:
    """Map composite score to a rating label."""
    if score >= 0.75:
        return "excellent"
    if score >= 0.50:
        return "good"
    if score >= 0.30:
        return "fair"
    return "poor"


_RATING_TIERS = ["poor", "fair", "good", "excellent"]


def _downgrade_rating(rating: str) -> str:
    """Downgrade a rating by one tier. "poor" stays "poor"."""
    idx = _RATING_TIERS.index(rating) if rating in _RATING_TIERS else 0
    return _RATING_TIERS[max(0, idx - 1)]


def _null_viewing_fields() -> dict[str, Any]:
    """Return a dict of all viewing fields set to null."""
    return {
        "viewingQuality": None,
        "viewingScore": None,
        "bestViewingTime": None,
        "clearWindowStart": None,
        "clearWindowEnd": None,
        "conjunction": None,
        "viewingNote": None,
    }


# ---------------------------------------------------------------------------
# Per-planet computation
# ---------------------------------------------------------------------------


def _compute_planet_fields(
    planet: dict[str, Any],
    group: str,  # "evening" | "morning" | "allNight"
    forecast_points: list[dict[str, Any]],
    moon_ra: float | None,
    moon_dec: float | None,
    moon_illumination: float | None,
    sun_rise: str | None,
    sun_set: str | None,
) -> dict[str, Any]:
    """Compute viewing quality fields for a single planet.

    Returns a dict of the seven viewing fields (values may be None on failure).
    Raises nothing — the caller should catch exceptions if needed.
    """
    name: str = planet.get("name", "")
    transit_ts: str | None = planet.get("transitTime")
    planet_ra: float | None = _to_float(planet.get("rightAscension"))
    planet_dec: float | None = _to_float(planet.get("declination"))
    planet_alt: float | None = _to_float(planet.get("altitude"))
    planet_rise_ts: str | None = planet.get("rise")
    planet_set_ts: str | None = planet.get("set")
    elongation: float | None = _to_float(planet.get("elongation"))

    # Mercury: determine the viewing window time (twilight horizon, not transit).
    # mercury_viewing_dt holds the twilight reference for bestViewingTime injection.
    is_mercury = name.lower() == "mercury"
    mercury_viewing_dt: datetime | None = None
    if is_mercury:
        # Elongation gate: too close to Sun to be observable.
        if elongation is not None and elongation < 12:
            return {
                **_null_viewing_fields(),
                "viewingQuality": "not_visible",
                "viewingScore": 0.0,
                "viewingNote": "In Sun's Glare",
            }
        # Select reference time for forecast matching: sunset+40min or sunrise-40min.
        if group == "evening":
            mercury_viewing_dt = _shifted_sun_time(sun_set, +40)
        else:
            mercury_viewing_dt = _shifted_sun_time(sun_rise, -40)
        # Fall back to transit if twilight reference unavailable.
        forecast_ref_dt = mercury_viewing_dt if mercury_viewing_dt is not None else _parse_iso(transit_ts)
    else:
        forecast_ref_dt = _parse_iso(transit_ts)

    if forecast_ref_dt is None:
        # No usable reference time — cannot compute quality.
        return _null_viewing_fields()

    # Find the nearest forecast point.
    nearest = _nearest_forecast_point(forecast_points, forecast_ref_dt)
    if nearest is None:
        return _null_viewing_fields()

    cloud_octet = nearest.get("cloudCoverOctet")
    if cloud_octet is None:
        cloud_octet = 0

    # Cloud gate.
    if cloud_octet > _CLOUD_GATE_OCTET:
        result = {
            **_null_viewing_fields(),
            "viewingQuality": "not_visible",
            "viewingScore": 0.0,
        }
    else:
        seeing_s = SEEING_SCORES.get(int(nearest.get("seeingIndex") or 0), 0.1)
        transp_s = TRANSPARENCY_SCORES.get(int(nearest.get("transparencyIndex") or 0), 0.1)
        alt_s = _altitude_score(planet_alt) if planet_alt is not None else 0.10

        score = (seeing_s * 0.80) + (transp_s * 0.05) + (alt_s * 0.15)
        rating = _score_to_rating(score)

        # Mercury: cap "excellent" at "good".
        if is_mercury and rating == "excellent":
            rating = "good"

        # Uranus/Neptune: apply moon proximity penalty.
        view_note: str | None = None
        if name.lower() in ("uranus", "neptune"):
            if (
                moon_illumination is not None
                and moon_illumination > 50
                and moon_ra is not None
                and moon_dec is not None
                and planet_ra is not None
                and planet_dec is not None
            ):
                dist = _angular_distance_deg(planet_ra, planet_dec, moon_ra, moon_dec)
                if dist < 30:
                    rating = _downgrade_rating(rating)
                    view_note = "Bright moon nearby"

        result = {
            **_null_viewing_fields(),
            "viewingQuality": rating,
            "viewingScore": round(score, 4),
            "viewingNote": view_note,
        }

    # Conjunction check (all planets).
    conjunction: str | None = None
    if (
        moon_ra is not None
        and moon_dec is not None
        and planet_ra is not None
        and planet_dec is not None
    ):
        dist = _angular_distance_deg(planet_ra, planet_dec, moon_ra, moon_dec)
        if dist < 5:
            conjunction = "Close Conjunction with Moon Tonight"
    result["conjunction"] = conjunction

    # Best viewing time.
    rise_dt = _parse_iso(planet_rise_ts)
    set_dt = _parse_iso(planet_set_ts)

    if is_mercury:
        # For Mercury use the twilight window time (not transit) as best viewing time.
        result["bestViewingTime"] = (
            mercury_viewing_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            if mercury_viewing_dt is not None
            else None
        )
    else:
        if cloud_octet <= _CLOUD_GATE_OCTET and nearest:
            # Transit is clear — transit is the best viewing time.
            result["bestViewingTime"] = _format_iso(transit_ts)
        else:
            # Transit is cloudy — find nearest clear point within rise-set window.
            result["bestViewingTime"] = _nearest_clear_time(
                forecast_points, rise_dt, set_dt
            )

    # Clear viewing window.
    clear_start, clear_end = _clear_window(forecast_points, rise_dt, set_dt)
    result["clearWindowStart"] = clear_start
    result["clearWindowEnd"] = clear_end

    return result


def _shifted_sun_time(ts: str | None, delta_minutes: int) -> datetime | None:
    """Parse *ts* and offset by *delta_minutes*, returning UTC-aware datetime."""
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return dt + timedelta(minutes=delta_minutes)


def _nearest_clear_time(
    forecast_points: list[dict[str, Any]],
    rise_dt: datetime | None,
    set_dt: datetime | None,
) -> str | None:
    """Return the validTime of the nearest clear point within the rise-set window."""
    now = datetime.now(tz=UTC)
    closest: dict[str, Any] | None = None
    closest_delta = float("inf")
    for pt in forecast_points:
        pt_dt = _parse_iso(pt.get("validTime"))
        if pt_dt is None:
            continue
        if rise_dt is not None and pt_dt < rise_dt:
            continue
        if set_dt is not None and pt_dt > set_dt:
            continue
        cloud = pt.get("cloudCoverOctet", 9)
        if cloud is None or cloud > _CLOUD_GATE_OCTET:
            continue
        delta = abs((pt_dt - now).total_seconds())
        if delta < closest_delta:
            closest_delta = delta
            closest = pt
    if closest is None:
        return None
    return _format_iso(closest.get("validTime"))


def _clear_window(
    forecast_points: list[dict[str, Any]],
    rise_dt: datetime | None,
    set_dt: datetime | None,
) -> tuple[str | None, str | None]:
    """Return (clearWindowStart, clearWindowEnd) for clear points within rise-set."""
    clear_dts: list[datetime] = []
    for pt in forecast_points:
        pt_dt = _parse_iso(pt.get("validTime"))
        if pt_dt is None:
            continue
        if rise_dt is not None and pt_dt < rise_dt:
            continue
        if set_dt is not None and pt_dt > set_dt:
            continue
        cloud = pt.get("cloudCoverOctet", 9)
        if cloud is None or cloud > _CLOUD_GATE_OCTET:
            continue
        clear_dts.append(pt_dt)
    if not clear_dts:
        return None, None
    return (
        min(clear_dts).strftime("%Y-%m-%dT%H:%M:%SZ"),
        max(clear_dts).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _format_iso(ts: str | None) -> str | None:
    """Normalise an ISO timestamp to 'Z' suffix form, or None."""
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_float(val: object) -> float | None:
    """Coerce a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Main enrichment entry point
# ---------------------------------------------------------------------------


async def enrich_planet_viewing(data: dict[str, Any]) -> dict[str, Any]:
    """Inject viewing quality fields into all planets in a /almanac/planets response.

    Fetches 7Timer seeing forecast and almanac moon/sun data from upstream.
    Degrades gracefully: seeing unavailable → all fields null; almanac unavailable
    → moon penalty and conjunction skipped.

    Never raises.
    """
    try:
        from weewx_clearskies_realtime.proxy import get_upstream_client

        client, upstream_url = get_upstream_client()
        if client is None or not upstream_url:
            logger.debug("planet_viewing enrichment: proxy client not available")
            return _inject_null_for_all_planets(data)

        # --- Fetch seeing forecast ---
        forecast_points: list[dict[str, Any]] | None = None
        try:
            resp = await client.get(f"{upstream_url}/api/v1/almanac/seeing-forecast")
            if resp.status_code < 400:  # noqa: PLR2004
                seeing_json = resp.json()
                # Expect {data: {points: [...]}} or {data: [...]} shapes.
                inner = seeing_json.get("data", seeing_json)
                if isinstance(inner, list):
                    forecast_points = inner
                elif isinstance(inner, dict):
                    forecast_points = inner.get("points") or inner.get("forecast") or []
                else:
                    forecast_points = []
            else:
                logger.debug(
                    "seeing-forecast returned %d; viewing fields will be null",
                    resp.status_code,
                )
        except Exception:  # noqa: BLE001
            logger.debug("seeing-forecast fetch failed; viewing fields will be null", exc_info=True)

        if not forecast_points:
            # Cannot compute quality without seeing data.
            return _inject_null_for_all_planets(data)

        # --- Fetch almanac for moon + sun ---
        moon_ra: float | None = None
        moon_dec: float | None = None
        moon_illumination: float | None = None
        sun_rise: str | None = None
        sun_set: str | None = None

        try:
            almanac_resp = await client.get(f"{upstream_url}/api/v1/almanac")
            if almanac_resp.status_code < 400:  # noqa: PLR2004
                almanac_json = almanac_resp.json()
                almanac_data = almanac_json.get("data", {})

                moon_block = almanac_data.get("moon", {})
                if isinstance(moon_block, dict):
                    moon_ra = _to_float(moon_block.get("rightAscension"))
                    moon_dec = _to_float(moon_block.get("declination"))
                    moon_illumination = _to_float(
                        moon_block.get("illuminationPercent")
                    )

                sun_block = almanac_data.get("sun", {})
                if isinstance(sun_block, dict):
                    sun_rise = sun_block.get("rise")
                    sun_set = sun_block.get("set")
            else:
                logger.debug(
                    "almanac returned %d; moon penalty and conjunction skipped",
                    almanac_resp.status_code,
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "almanac fetch failed; moon penalty and conjunction skipped", exc_info=True
            )

        # --- Process each group ---
        payload = data.get("data")
        if not isinstance(payload, dict):
            return data

        for group in ("evening", "morning", "allNight"):
            planets = payload.get(group)
            if not isinstance(planets, list):
                continue
            for planet in planets:
                if not isinstance(planet, dict):
                    continue
                try:
                    fields = _compute_planet_fields(
                        planet=planet,
                        group=group,
                        forecast_points=forecast_points,
                        moon_ra=moon_ra,
                        moon_dec=moon_dec,
                        moon_illumination=moon_illumination,
                        sun_rise=sun_rise,
                        sun_set=sun_set,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "planet_viewing: failed computing fields for %s",
                        planet.get("name", "<unknown>"),
                        exc_info=True,
                    )
                    fields = _null_viewing_fields()
                planet.update(fields)

        return data

    except Exception:  # noqa: BLE001
        logger.exception("planet_viewing enrichment: unexpected error")
        return data


def _inject_null_for_all_planets(data: dict[str, Any]) -> dict[str, Any]:
    """Set all viewing fields to null for every planet in the response."""
    payload = data.get("data")
    if not isinstance(payload, dict):
        return data
    for group in ("evening", "morning", "allNight"):
        planets = payload.get(group)
        if not isinstance(planets, list):
            continue
        for planet in planets:
            if isinstance(planet, dict):
                planet.update(_null_viewing_fields())
    return data

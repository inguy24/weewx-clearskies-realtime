"""Barometer trend enrichment for GET /api/v1/current.

Computes a pressure trend (inHg delta) by comparing the current barometer
reading against the archive record nearest to 3 hours ago.  The result is
injected as ``barometerTrend`` into the /current response envelope.

Positive values indicate rising pressure; negative values indicate falling.
``null`` is injected when the current reading is absent, the archive query
fails, or the historical record is missing.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# How far back (in seconds) to look for the historical barometer reading.
TREND_TIME_DELTA: int = 10800  # 3 hours

# Grace period: accept a record within ±TREND_TIME_GRACE seconds of the
# target timestamp.  The archive endpoint returns the single record closest
# to the requested time; we accept it if within this window.
TREND_TIME_GRACE: int = 300  # 5 minutes


async def enrich_barometer_trend(data: dict[str, Any]) -> dict[str, Any]:
    """Inject ``barometerTrend`` (inHg delta over 3 h) into a /current response.

    The enrichment reads the current ``barometer`` and ``dateTime`` from the
    observation sub-dict (``data["data"]``), queries the upstream archive
    endpoint for the record nearest to 3 hours ago, and computes the delta.

    Any failure (missing fields, HTTP error, JSON decode error, network issue)
    results in ``barometerTrend: null`` being injected.  The function never
    raises — GET /current must not break because of this enrichment.
    """
    # The /current response envelope shape: {data: {...obs...}, units: {...}, ...}
    obs = data.get("data")
    if not isinstance(obs, dict):
        data["barometerTrend"] = None
        return data

    current_barometer = obs.get("barometer")
    date_time = obs.get("dateTime")

    if current_barometer is None or date_time is None:
        data["barometerTrend"] = None
        return data

    try:
        current_barometer = float(current_barometer)
        ts_current = int(date_time)
    except (TypeError, ValueError):
        data["barometerTrend"] = None
        return data

    ts_historical = ts_current - TREND_TIME_DELTA

    try:
        from weewx_clearskies_realtime.proxy import get_upstream_client

        client, upstream_url = get_upstream_client()
        if client is None or not upstream_url:
            data["barometerTrend"] = None
            return data

        url = f"{upstream_url}/api/v1/archive"
        resp = await client.get(
            url,
            params={"to": ts_historical, "limit": 1, "fields": "barometer,dateTime"},
        )

        if resp.status_code == 404 or resp.status_code >= 400:
            data["barometerTrend"] = None
            return data

        archive_data = resp.json()

    except Exception:  # noqa: BLE001
        logger.warning(
            "barometer_trend: archive query failed",
            extra={"ts_historical": ts_historical},
            exc_info=True,
        )
        data["barometerTrend"] = None
        return data

    # The archive response shape: {records: [...], ...} or {data: [...], ...}
    # Try both key names that _apply_conversion recognises.
    records: list[Any] | None = None
    for key in ("records", "data", "results"):
        candidate = archive_data.get(key)
        if isinstance(candidate, list):
            records = candidate
            break

    if not records:
        data["barometerTrend"] = None
        return data

    record = records[0]
    if not isinstance(record, dict):
        data["barometerTrend"] = None
        return data

    historical_barometer = record.get("barometer")
    historical_ts_raw = record.get("dateTime")

    if historical_barometer is None:
        data["barometerTrend"] = None
        return data

    try:
        historical_barometer = float(historical_barometer)
    except (TypeError, ValueError):
        data["barometerTrend"] = None
        return data

    # Grace-period check: reject the record if it is too far from the target.
    if historical_ts_raw is not None:
        try:
            historical_ts = int(historical_ts_raw)
            if abs(historical_ts - ts_historical) > TREND_TIME_GRACE:
                data["barometerTrend"] = None
                return data
        except (TypeError, ValueError):
            # Can't validate the timestamp — proceed without rejecting.
            pass

    trend = round(current_barometer - historical_barometer, 3)
    data["barometerTrend"] = trend
    return data

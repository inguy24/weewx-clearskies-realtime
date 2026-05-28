"""Barometer trend enrichment for GET /api/v1/current.

Computes a pressure trend (inHg delta) by comparing the current barometer
reading against the archive record nearest to 3 hours ago.  The result is
injected as ``barometerTrend`` into the /current response envelope.

Positive values indicate rising pressure; negative values indicate falling.
``null`` is injected when the current reading is absent, the archive query
fails, or no historical record is found within the grace window.

Contract notes (verified against the API 2026-05-28):
* The /current observation carries its time as ``timestamp`` (UTC ISO-8601,
  e.g. ``"2026-05-28T22:35:00Z"``) — there is no epoch ``dateTime`` field.
* The upstream ``/archive`` endpoint expects ISO-8601 ``from``/``to`` bounds,
  rejects unknown field names (so we request ``timestamp``, not ``dateTime``),
  and returns records under the ``data`` key, each carrying ``timestamp``.
* ``/archive`` returns records in ascending time order, so a bare
  ``to=<target>&limit=1`` yields the *oldest* record at or before the target,
  not the nearest one.  We therefore bound the query to a tight window around
  the 3-hours-ago target (``±TREND_TIME_GRACE``) so ``limit=1`` returns a
  record close to the target.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# How far back (in seconds) to look for the historical barometer reading.
TREND_TIME_DELTA: int = 10800  # 3 hours

# Grace period: accept a record within ±TREND_TIME_GRACE seconds of the target
# timestamp, and bound the archive query to that same window.
TREND_TIME_GRACE: int = 300  # 5 minutes


def _iso_to_epoch(value: str) -> int:
    """Parse a UTC ISO-8601 string (``...Z``) to integer epoch seconds."""
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _epoch_to_iso(epoch: int) -> str:
    """Format integer epoch seconds as a UTC ISO-8601 string (``...Z``)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def enrich_barometer_trend(data: dict[str, Any]) -> dict[str, Any]:
    """Inject ``barometerTrend`` (inHg delta over 3 h) into a /current response.

    Reads the current ``barometer`` and ``timestamp`` from the observation
    sub-dict (``data["data"]``), queries the upstream archive endpoint for the
    record nearest to 3 hours ago, and computes the delta.

    Any failure (missing fields, unparseable timestamp, HTTP error, JSON decode
    error, network issue) results in ``barometerTrend: null``.  The function
    never raises — GET /current must not break because of this enrichment.
    """
    # The /current response envelope shape: {data: {...obs...}, units: {...}, ...}
    obs = data.get("data")
    if not isinstance(obs, dict):
        data["barometerTrend"] = None
        return data

    current_barometer = obs.get("barometer")
    timestamp = obs.get("timestamp")

    if current_barometer is None or timestamp is None:
        data["barometerTrend"] = None
        return data

    try:
        current_barometer = float(current_barometer)
        ts_current = _iso_to_epoch(str(timestamp))
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

        # Bound the query to a tight window around the target; /archive returns
        # ascending time order, so limit=1 then yields a record within the
        # grace window rather than the oldest record at or before the target.
        url = f"{upstream_url}/api/v1/archive"
        resp = await client.get(
            url,
            params={
                "from": _epoch_to_iso(ts_historical - TREND_TIME_GRACE),
                "to": _epoch_to_iso(ts_historical + TREND_TIME_GRACE),
                "limit": 1,
                "fields": "barometer,timestamp",
            },
        )

        if resp.status_code >= 400:
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

    # The archive response envelope key is ``data`` (a list); accept the legacy
    # ``records``/``results`` names too for robustness.
    records: list[Any] | None = None
    for key in ("data", "records", "results"):
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
    historical_ts_raw = record.get("timestamp")

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
            historical_ts = _iso_to_epoch(str(historical_ts_raw))
            if abs(historical_ts - ts_historical) > TREND_TIME_GRACE:
                data["barometerTrend"] = None
                return data
        except (TypeError, ValueError):
            # Can't validate the timestamp — proceed without rejecting.
            pass

    trend = round(current_barometer - historical_barometer, 3)
    data["barometerTrend"] = trend
    return data

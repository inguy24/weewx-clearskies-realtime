"""Barometer trend enrichment for GET /api/v1/current.

Computes a pressure trend by comparing the current barometer reading against
the archive record nearest to 3 hours ago.  Two fields are injected into the
/current response envelope:

* ``barometerTrend`` — numeric delta (current minus historical, rounded 3 dp)
  in whatever unit the barometer values carry.  Kept as-is for backwards
  compatibility.
* ``barometerTrendDirection`` — direction string: one of ``"rising"``,
  ``"falling"``, ``"steady"``, or ``null`` when the trend is null or the
  source unit cannot be resolved to inHg for threshold comparison.  The
  classification threshold is ±0.01 inHg applied to the delta after converting
  it to inHg using the project's unit-conversion helper (ADR-042).

Positive ``barometerTrend`` values indicate rising pressure; negative values
indicate falling.  Both fields are ``null`` when the current reading is absent,
the archive query fails, or no historical record is found within the grace
window.

Source-unit resolution order (lead-approved, 2026-05-29):
1. The transformer's configured ``group_pressure`` target unit — accessed via
   the proxy module's ``_transformer._targets`` when a transformer is
   configured.  This is the authoritative unit because it is validated against
   ``VALID_UNITS["group_pressure"]`` at transformer construction time.
2. Fallback: ``data["units"]["barometer"]`` label stripped from the upstream
   response.  Accepted only when it is a member of
   ``_VALID_PRESSURE_UNITS`` (handles operator ``[[Labels]]`` override
   — a customised label like ``"mb"`` would not be in the valid set and is
   rejected rather than passed to convert()).
3. If neither source yields a known valid unit, ``barometerTrendDirection`` is
   ``null`` and a DEBUG log line explains why.  ``barometerTrend`` is still
   emitted.  The function never raises.

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

# Pressure units known to the conversion registry.  Used to validate a
# candidate unit string before passing it to convert().
_VALID_PRESSURE_UNITS: frozenset[str] = frozenset({"inHg", "mbar", "hPa", "kPa"})

# Threshold in inHg for rising/falling classification (matches historical
# dashboard semantics — barometer.ts ±0.01 inHg).
_DIRECTION_THRESHOLD_INHG: float = 0.01

# How far back (in seconds) to look for the historical barometer reading.
TREND_TIME_DELTA: int = 10800  # 3 hours

# Grace period: accept a record within ±TREND_TIME_GRACE seconds of the target
# timestamp, and bound the archive query to that same window.
TREND_TIME_GRACE: int = 300  # 5 minutes


def _resolve_pressure_unit(data: dict[str, Any]) -> str | None:
    """Return the authoritative pressure unit for the barometer delta.

    Resolution order (per lead approval 2026-05-29):
    1. ``proxy._transformer._targets["group_pressure"]`` — the configured
       display unit, already validated against VALID_UNITS at construction.
    2. ``data["units"]["barometer"]`` label — accepted only when it is a
       member of ``_VALID_PRESSURE_UNITS`` (rejects operator label overrides
       like ``"mb"`` that would cause convert() to raise).

    Returns a unit string (e.g. ``"inHg"``, ``"mbar"``) or ``None`` when the
    unit cannot be determined safely.  Never raises.
    """
    # Option 1: transformer's configured group_pressure target unit.
    try:
        import weewx_clearskies_realtime.proxy as _proxy_mod  # lazy, avoids circular import

        transformer = getattr(_proxy_mod, "_transformer", None)
        if transformer is not None:
            targets = getattr(transformer, "_targets", {})
            unit = targets.get("group_pressure")
            if unit and unit in _VALID_PRESSURE_UNITS:
                return unit
    except Exception:  # noqa: BLE001
        pass

    # Option 2: units-block label from the upstream response.
    try:
        units_block = data.get("units")
        if isinstance(units_block, dict):
            label = str(units_block.get("barometer", "")).strip()
            if label in _VALID_PRESSURE_UNITS:
                return label
    except Exception:  # noqa: BLE001
        pass

    return None


def _classify_direction(trend: float, source_unit: str | None) -> str | None:
    """Return ``"rising"``, ``"falling"``, or ``"steady"`` for *trend*.

    Converts *trend* from *source_unit* to inHg before comparing against
    ``_DIRECTION_THRESHOLD_INHG``.  Returns ``None`` when *source_unit* is
    ``None`` (unit unknown) or when the conversion fails.  Never raises.
    """
    if source_unit is None:
        return None
    try:
        from weewx_clearskies_realtime.units.conversion import convert

        trend_inhg = convert(trend, source_unit, "inHg")
        if trend_inhg is None:
            return None
        if trend_inhg > _DIRECTION_THRESHOLD_INHG:
            return "rising"
        if trend_inhg < -_DIRECTION_THRESHOLD_INHG:
            return "falling"
        return "steady"
    except Exception:  # noqa: BLE001
        return None


def _iso_to_epoch(value: str) -> int:
    """Parse a UTC ISO-8601 string (``...Z``) to integer epoch seconds."""
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _epoch_to_iso(epoch: int) -> str:
    """Format integer epoch seconds as a UTC ISO-8601 string (``...Z``)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def enrich_barometer_trend(data: dict[str, Any]) -> dict[str, Any]:
    """Inject ``barometerTrend`` and ``barometerTrendDirection`` into a /current response.

    Reads the current ``barometer`` and ``timestamp`` from the observation
    sub-dict (``data["data"]``), queries the upstream archive endpoint for the
    record nearest to 3 hours ago, computes the numeric delta, and classifies
    the direction by converting that delta to inHg before thresholding.

    Any failure (missing fields, unparseable timestamp, HTTP error, JSON decode
    error, network issue) results in both fields being ``null``.  The function
    never raises — GET /current must not break because of this enrichment.
    """
    # The /current response envelope shape: {data: {...obs...}, units: {...}, ...}
    obs = data.get("data")
    if not isinstance(obs, dict):
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    current_barometer = obs.get("barometer")
    timestamp = obs.get("timestamp")

    if current_barometer is None or timestamp is None:
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    try:
        current_barometer = float(current_barometer)
        ts_current = _iso_to_epoch(str(timestamp))
    except (TypeError, ValueError):
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    ts_historical = ts_current - TREND_TIME_DELTA

    try:
        from weewx_clearskies_realtime.proxy import get_upstream_client

        client, upstream_url = get_upstream_client()
        if client is None or not upstream_url:
            data["barometerTrend"] = None
            data["barometerTrendDirection"] = None
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
            data["barometerTrendDirection"] = None
            return data

        archive_data = resp.json()

    except Exception:  # noqa: BLE001
        logger.warning(
            "barometer_trend: archive query failed",
            extra={"ts_historical": ts_historical},
            exc_info=True,
        )
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
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
        data["barometerTrendDirection"] = None
        return data

    record = records[0]
    if not isinstance(record, dict):
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    historical_barometer = record.get("barometer")
    historical_ts_raw = record.get("timestamp")

    if historical_barometer is None:
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    try:
        historical_barometer = float(historical_barometer)
    except (TypeError, ValueError):
        data["barometerTrend"] = None
        data["barometerTrendDirection"] = None
        return data

    # Grace-period check: reject the record if it is too far from the target.
    if historical_ts_raw is not None:
        try:
            historical_ts = _iso_to_epoch(str(historical_ts_raw))
            if abs(historical_ts - ts_historical) > TREND_TIME_GRACE:
                data["barometerTrend"] = None
                data["barometerTrendDirection"] = None
                return data
        except (TypeError, ValueError):
            # Can't validate the timestamp — proceed without rejecting.
            pass

    trend = round(current_barometer - historical_barometer, 3)
    data["barometerTrend"] = trend

    # Classify direction: convert the raw delta to inHg before comparing to
    # the ±0.01 inHg threshold so the classification is unit-agnostic.
    source_unit = _resolve_pressure_unit(data)
    if source_unit is None:
        logger.debug(
            "barometer_trend: pressure unit unknown — barometerTrendDirection null",
            extra={"units_block": data.get("units")},
        )
    data["barometerTrendDirection"] = _classify_direction(trend, source_unit)
    return data

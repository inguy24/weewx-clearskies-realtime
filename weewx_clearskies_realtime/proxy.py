"""BFF proxy — forwards /api/v1/* requests to the upstream API.

Applies UnitTransformer to JSON responses that contain weather data.
Two response shapes are handled:

  1. Flat record with ``usUnits`` key — weewx archive records proxied directly.
     ``_apply_conversion`` passes the record to
     ``UnitTransformer.transform_record()``.

  2. Observation envelope ``{data: dict, units: label_dict, …}`` — the shape
     returned by the upstream clearskies-api for ``/api/v1/current``.  The
     ``data`` sub-dict holds raw observation values in the station's native
     unit system.  The ``units`` sub-dict maps field names to human-readable
     label strings (e.g. ``{"outTemp": "°F", "windSpeed": "mph"}``).
     ``_infer_us_units`` converts that label block to a weewx ``usUnits``
     integer (1 = US, 16 = Metric, 17 = MetricWX) so
     ``UnitTransformer.transform_record()`` can apply the right conversion.

ADR-041: The realtime service is the dashboard's single backend gateway.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from weewx_clearskies_realtime.units.transformer import UnitTransformer

logger = logging.getLogger(__name__)

router = APIRouter()

_client: httpx.AsyncClient | None = None
_transformer: UnitTransformer | None = None
_upstream_url: str = ""
_tls_verify: bool = False

# URL probed by the health readiness check.  Tried in order; first success wins.
_HEALTH_PROBE_PATHS: tuple[str, ...] = ("/health/live", "/health/ready", "/api/v1/health")

# Headers that must not be forwarded to the upstream — they are hop-by-hop or
# would confuse the upstream about the real host.
_HOP_BY_HOP: frozenset[str] = frozenset(
    {"host", "connection", "transfer-encoding", "te", "trailer", "upgrade"}
)


def configure(
    upstream_url: str,
    timeout: int,
    tls_verify: bool,
    transformer: UnitTransformer | None,
) -> None:
    """Initialize the proxy with upstream URL and unit transformer.

    Called once at app startup.  Idempotent — safe to call again (e.g. in tests).
    """
    global _client, _transformer, _upstream_url, _tls_verify  # noqa: PLW0603
    _upstream_url = upstream_url.rstrip("/")
    _transformer = transformer
    _tls_verify = tls_verify
    # Close any existing client before replacing it.
    # We can't await here (called from sync context at startup), so just replace;
    # the old client will be garbage-collected.
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        verify=tls_verify,
        follow_redirects=True,
    )
    logger.info(
        "Proxy configured",
        extra={"upstream_url": _upstream_url, "tls_verify": tls_verify},
    )


async def close() -> None:
    """Close the httpx client.  Called at app shutdown."""
    global _client  # noqa: PLW0603
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Route — catch-all /api/v1/* forward
# ---------------------------------------------------------------------------


@router.api_route(
    "/api/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    include_in_schema=False,
)
async def proxy_api(request: Request, path: str) -> Response:
    """Forward request to upstream API and apply unit conversion to JSON responses."""
    if _client is None or not _upstream_url:
        return JSONResponse(
            {"error": "API proxy not configured"},
            status_code=503,
        )

    url = f"{_upstream_url}/api/v1/{path}"
    params = dict(request.query_params)

    # Forward headers selectively — strip hop-by-hop and host.
    headers: dict[str, str] = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    body = await request.body() if request.method != "GET" else None

    try:
        upstream_resp = await _client.request(
            method=request.method,
            url=url,
            params=params,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        logger.warning("Upstream API unreachable", extra={"upstream_url": _upstream_url})
        return JSONResponse({"error": "upstream API unreachable"}, status_code=502)
    except httpx.TimeoutException:
        logger.warning("Upstream API timed out", extra={"upstream_url": _upstream_url})
        return JSONResponse({"error": "upstream API timeout"}, status_code=504)

    content_type = upstream_resp.headers.get("content-type", "")
    if "application/json" in content_type and _transformer is not None:
        try:
            data = upstream_resp.json()
            converted = _apply_conversion(data)
            return JSONResponse(converted, status_code=upstream_resp.status_code)
        except Exception:  # noqa: BLE001
            # Conversion failed; fall through to raw pass-through below.
            logger.debug("Unit conversion failed; passing through raw response")

    # Non-JSON or conversion failed — pass through raw.
    # Strip hop-by-hop headers from upstream response before forwarding.
    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# Health probe — synchronous, registered with health.py probe registry
# ---------------------------------------------------------------------------


def upstream_health_probe() -> "ProbeResult":  # type: ignore[name-defined]  # noqa: F821
    """Synchronous readiness probe: check upstream API connectivity.

    Imported and registered by __main__.py after the proxy is configured.
    Returns a ProbeResult (imported at call time to avoid circular imports).
    """
    from weewx_clearskies_realtime.health import ProbeResult

    if not _upstream_url:
        # Proxy not configured — not a failure, just not applicable.
        return ProbeResult(name="upstream_api", status="ok", messages=["proxy not configured"])

    for probe_path in _HEALTH_PROBE_PATHS:
        url = f"{_upstream_url}{probe_path}"
        try:
            # Synchronous httpx call — health probes run in a sync context.
            resp = httpx.get(
                url,
                timeout=5.0,
                follow_redirects=True,
                verify=_tls_verify,
            )
            if resp.status_code < 500:  # noqa: PLR2004
                return ProbeResult(
                    name="upstream_api",
                    status="ok",
                    messages=[f"GET {url} → {resp.status_code}"],
                )
        except httpx.ConnectError:
            # Try next probe path before failing.
            continue
        except httpx.TimeoutException:
            return ProbeResult(
                name="upstream_api",
                status="warning",
                messages=[f"upstream API probe timed out ({url})"],
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                name="upstream_api",
                status="warning",
                messages=[f"upstream API probe error: {exc}"],
            )

    return ProbeResult(
        name="upstream_api",
        status="warning",
        messages=[f"upstream API unreachable at {_upstream_url}"],
    )


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

# Map from the label strings the upstream API returns in its ``units`` block
# to weewx unit-system codes.  Temperature is used as the primary discriminator
# (unambiguous across all three systems); rain disambiguates Metric from
# MetricWX when the temperature label alone would match both.
_TEMP_LABEL_TO_US_UNITS: dict[str, int] = {
    "°F": 1,   # US
    "°C": 16,  # Metric or MetricWX — disambiguated by rain label below
}
_RAIN_LABEL_TO_US_UNITS: dict[str, int] = {
    "mm": 17,  # MetricWX
    "cm": 16,  # Metric
}


def _infer_us_units(units_block: dict[str, object]) -> int:
    """Infer the weewx usUnits code from the upstream API ``units`` label block.

    The upstream clearskies-api does not embed a ``usUnits`` integer in its
    ``/current`` response; it only sends a ``units`` dict of field → label
    strings (e.g. ``{"outTemp": "°F", "windSpeed": "mph"}``).  This function
    reverse-maps those labels back to a unit-system code so that
    ``UnitTransformer.transform_record()`` can look up source units.

    Args:
        units_block: The ``units`` dict from the upstream ``/current`` response.

    Returns:
        1 (US), 16 (Metric), or 17 (MetricWX).  Defaults to 1 (US) when the
        labels are absent or do not match any known system — this is the weewx
        default and safe for US-configured stations.
    """
    temp_label = str(units_block.get("outTemp", ""))
    us_units = _TEMP_LABEL_TO_US_UNITS.get(temp_label, 1)

    # Metric (16) and MetricWX (17) both use °C for temperature.
    # Disambiguate via the rain label: MetricWX uses mm, Metric uses cm.
    if us_units == 16:
        rain_label = str(units_block.get("rain", ""))
        us_units = _RAIN_LABEL_TO_US_UNITS.get(rain_label, 16)

    return us_units


def _apply_conversion(data: dict[str, object] | list[object]) -> dict[str, object] | list[object]:
    """Apply unit conversion to API response data.

    Handles three shapes:

    1. A list — each element is recursively converted if it is a dict.
    2. An observation envelope ``{data: dict, units: label_dict, …}`` — the
       shape returned by ``/current``.  The ``data`` sub-dict is a flat
       observation record; ``units`` is a label block used to infer the source
       unit system.  The transformer is applied to ``data`` directly and the
       result replaces it in the returned envelope.
    3. A nested-list envelope ``{records|data|results: [...], …}`` — the shape
       returned by ``/archive``.  Each record in the list is converted.
    4. A flat record with ``usUnits`` — weewx direct-read or MQTT records
       proxied with their unit-system code intact.
    """
    if isinstance(data, list):
        return [_apply_conversion(item) if isinstance(item, dict) else item for item in data]

    if not isinstance(data, dict):
        return data

    # --- Shape 2: observation envelope {data: dict, units: label_dict, …} ---
    # The upstream /current response embeds a flat observation dict under "data"
    # and a label block under "units".  Detect this by checking that "data" is a
    # dict (not a list) AND "units" is also a dict.
    obs_payload = data.get("data")
    units_block = data.get("units")
    if (
        isinstance(obs_payload, dict)
        and isinstance(units_block, dict)
        and _transformer is not None
    ):
        us_units = _infer_us_units(units_block)
        try:
            converted_obs = _transformer.transform_record(obs_payload, us_units)
            # transform_record returns {value, label, formatted} dicts for
            # numeric observations.  The dashboard expects a flat shape — numeric
            # values in "data" and unit labels already in the "units" envelope —
            # so flatten to a single scalar per observation.
            #
            # Rounding strategy: parse the "formatted" string back to float so
            # the precision matches the operator's StringFormats config (e.g.
            # "%.1f" for temperature, "%.0f" for wind speed).  Fall back to the
            # raw "value" float when "formatted" is not a valid number (compass
            # direction labels, "N/A" for None values, text fields like
            # weatherText).  String pass-throughs and unknown fields are kept
            # as-is.
            flattened: dict[str, object] = {}
            for key, val in converted_obs.items():
                if not isinstance(val, dict) or "value" not in val:
                    # Unknown / passthrough field — no conversion applied.
                    flattened[key] = val
                    continue
                raw_val = val["value"]
                formatted_str = val.get("formatted", "")
                if raw_val is None:
                    flattened[key] = None
                elif isinstance(raw_val, str):
                    # Text field (e.g. weatherText) — value is already a string.
                    flattened[key] = raw_val
                else:
                    # Numeric field — use the formatted string parsed back to
                    # float to preserve StringFormats-configured rounding.
                    try:
                        flattened[key] = float(formatted_str)
                    except (ValueError, TypeError):
                        # formatted is a non-numeric string (compass ordinate,
                        # "N/A", etc.) — fall back to the raw converted float,
                        # rounded to 1 decimal so direction degrees don't leak
                        # full-precision floats to callers.
                        if isinstance(raw_val, float):
                            flattened[key] = round(raw_val, 1)
                        else:
                            flattened[key] = raw_val
            return {**data, "data": flattened}
        except Exception:  # noqa: BLE001
            logger.debug("Observation envelope conversion failed; passing through raw")

    # --- Shape 3: nested-list envelope {records|data|results: [...], …} ---
    # Convert each record in the list but don't modify the outer envelope.
    for key in ("records", "data", "results"):
        if key in data and isinstance(data[key], list):
            converted_list = [
                _apply_conversion(r) if isinstance(r, dict) else r
                for r in data[key]  # type: ignore[union-attr]
            ]
            return {**data, key: converted_list}

    # --- Shape 4: flat record with usUnits (direct-read / MQTT / archive) ---
    us_units_raw = data.get("usUnits")
    if us_units_raw is not None and _transformer is not None:
        try:
            return _transformer.transform_record(data, int(us_units_raw))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            pass

    return data

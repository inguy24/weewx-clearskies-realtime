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

Enrichment extension:
  ``EnrichmentRegistry`` and ``create_proxy_router()`` provide an additive
  factory-based API for registering post-conversion enrichment functions that
  are applied to successful JSON GET responses.  This is supplementary to the
  module-level ``router`` / ``configure()`` pattern used by the existing app;
  both interfaces are intentionally preserved for backward compatibility.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

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
_enrichment_registry: "EnrichmentRegistry | None" = None

# URL probed by the health readiness check.  Tried in order; first success wins.
_HEALTH_PROBE_PATHS: tuple[str, ...] = ("/health/live", "/health/ready", "/api/v1/health")

# Headers that must not be forwarded to the upstream — they are hop-by-hop or
# would confuse the upstream about the real host.
_HOP_BY_HOP: frozenset[str] = frozenset(
    {"host", "connection", "transfer-encoding", "te", "trailer", "upgrade"}
)

# Full set of hop-by-hop headers per RFC 7230 §6.1, used by create_proxy_router().
_HOP_BY_HOP_FULL: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)

# Type alias for an enrichment function.
EnrichmentFn = Callable[[dict[str, Any]], dict[str, Any]]


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
    """Close the httpx client and reset enrichment state.  Called at app shutdown."""
    global _client, _enrichment_registry  # noqa: PLW0603
    if _client is not None:
        await _client.aclose()
        _client = None
    _enrichment_registry = None


# ---------------------------------------------------------------------------
# Route — catch-all /api/v1/* forward
# ---------------------------------------------------------------------------


def _run_enrichments(
    method: str, path: str, status_code: int, data: object,
) -> object:
    """Apply registered enrichments to a parsed JSON response.

    Only runs on successful GET requests with dict data.  Individual enrichment
    failures are logged and skipped; remaining enrichments still run.
    """
    if (
        _enrichment_registry is None
        or method != "GET"
        or status_code >= 400
        or not isinstance(data, dict)
    ):
        return data
    endpoint_key = path.split("/")[0] if path else ""
    for fn in _enrichment_registry.get(endpoint_key):
        try:
            data = fn(data)
        except Exception:  # noqa: BLE001
            logger.exception("Enrichment failed for endpoint %s", endpoint_key)
    return data


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
    if "application/json" in content_type and (
        _transformer is not None or _enrichment_registry is not None
    ):
        try:
            data = upstream_resp.json()
            if _transformer is not None:
                data = _apply_conversion(data)
            data = _run_enrichments(
                request.method, path, upstream_resp.status_code, data,
            )
            return JSONResponse(data, status_code=upstream_resp.status_code)
        except Exception:  # noqa: BLE001
            logger.debug("Processing failed; passing through raw response")

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
# Enrichment registry + factory router (additive BFF extension)
# ---------------------------------------------------------------------------


class EnrichmentRegistry:
    """Maps REST endpoint keys to lists of enrichment functions.

    Endpoint keys are the first path segment of the upstream URL path, e.g.
    "current" for /api/v1/current or /api/v1/current/detail.

    Enrichments are applied to successful JSON GET responses AFTER any unit
    conversion performed by the module-level proxy_api route.  When
    create_proxy_router() is used, enrichments are applied inside that router
    instead.
    """

    def __init__(self) -> None:
        self._registry: dict[str, list[EnrichmentFn]] = {}

    def register(self, endpoint: str, fn: EnrichmentFn) -> None:
        """Register fn for the given endpoint key.

        Multiple registrations for the same endpoint are accumulated and run
        in registration order.
        """
        self._registry.setdefault(endpoint, []).append(fn)

    def get(self, endpoint: str) -> list[EnrichmentFn]:
        """Return registered functions for endpoint, or an empty list if none."""
        return list(self._registry.get(endpoint, []))


def register_enrichment(endpoint: str, fn: EnrichmentFn) -> None:
    """Register an enrichment for the module-level proxy.

    Creates a registry if none exists.  Multiple registrations on the same
    endpoint accumulate and run in registration order.
    """
    global _enrichment_registry  # noqa: PLW0603
    if _enrichment_registry is None:
        _enrichment_registry = EnrichmentRegistry()
    _enrichment_registry.register(endpoint, fn)


def create_proxy_router(
    client: httpx.AsyncClient,
    upstream_url: str,
    registry: EnrichmentRegistry,
) -> APIRouter:
    """Return an APIRouter with a catch-all /api/v1/{path} proxy route.

    This factory creates an independent router with its own httpx client and
    enrichment registry.  It is supplementary to the module-level ``router``
    / ``configure()`` interface — both may coexist in the same application.

    Args:
        client:       Shared httpx async client (lifecycle managed by caller).
        upstream_url: Base URL of the upstream clearskies-api service.
        registry:     Enrichment registry consulted on successful JSON GETs.
    """
    factory_router = APIRouter()

    @factory_router.api_route(
        "/api/v1/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE"],
    )
    async def proxy_request(path: str, request: Request) -> Response:
        """Forward the request to the upstream API and apply enrichments."""
        # --- Build upstream URL ---
        query = request.url.query
        upstream_path = f"/api/v1/{path}"
        if query:
            upstream_path = f"{upstream_path}?{query}"

        # --- Strip hop-by-hop and host headers ---
        forwarded_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP_FULL
        }

        # --- Forward body for non-GET methods ---
        body: bytes | None = None
        if request.method != "GET":
            body = await request.body()

        # --- Send to upstream ---
        try:
            upstream_response = await client.request(
                method=request.method,
                url=upstream_path,
                headers=forwarded_headers,
                content=body,
            )
        except httpx.TimeoutException:
            logger.warning(
                "Upstream API timed out",
                extra={"path": path, "method": request.method},
            )
            return JSONResponse(
                status_code=504,
                media_type="application/problem+json",
                content={
                    "type": "about:blank",
                    "title": "Gateway Timeout",
                    "status": 504,
                    "detail": "Upstream API timed out",
                },
            )
        except httpx.ConnectError:
            logger.warning(
                "Upstream API unreachable",
                extra={"path": path, "method": request.method},
            )
            return JSONResponse(
                status_code=502,
                media_type="application/problem+json",
                content={
                    "type": "about:blank",
                    "title": "Bad Gateway",
                    "status": 502,
                    "detail": "Upstream API unreachable",
                },
            )

        # --- Strip response headers that become invalid after body mutation ---
        response_headers = {
            k: v
            for k, v in upstream_response.headers.items()
            if k.lower() not in ("content-length", "content-encoding")
        }

        content_type = upstream_response.headers.get("content-type", "")

        # --- Pass error responses through unchanged ---
        if upstream_response.status_code >= 400:
            return Response(
                content=upstream_response.content,
                status_code=upstream_response.status_code,
                headers=response_headers,
                media_type=content_type,
            )

        # --- Non-JSON or non-GET: pass through as-is ---
        is_json = "application/json" in content_type
        if not is_json or request.method != "GET":
            return Response(
                content=upstream_response.content,
                status_code=upstream_response.status_code,
                headers=response_headers,
                media_type=content_type,
            )

        # --- JSON GET: parse body and apply enrichments ---
        try:
            data: dict[str, Any] = upstream_response.json()
        except Exception:  # noqa: BLE001
            # Upstream claimed JSON but sent malformed content; forward as-is.
            logger.warning(
                "Upstream returned non-parseable JSON",
                extra={"path": path, "status": upstream_response.status_code},
            )
            return Response(
                content=upstream_response.content,
                status_code=upstream_response.status_code,
                headers=response_headers,
                media_type=content_type,
            )

        # Endpoint key = first segment of the path (e.g. "current", "charts").
        endpoint_key = path.split("/")[0] if path else ""
        enrichments = registry.get(endpoint_key)

        for fn in enrichments:
            try:
                data = fn(data)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Enrichment function raised an exception; continuing with current data",
                    extra={
                        "endpoint": endpoint_key,
                        "function": getattr(fn, "__name__", repr(fn)),
                    },
                )
                # Continue with whatever data state we had before this fn ran.

        return JSONResponse(
            content=data,
            status_code=upstream_response.status_code,
            headers=response_headers,
        )

    return factory_router


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


def _flatten_converted_value(val: object) -> object:
    """Flatten one ConvertedValue dict to a scalar, or pass non-converted values through.

    A ConvertedValue dict has the shape ``{"value": float|None, "label": str,
    "formatted": str}`` — the output of ``UnitTransformer.transform_record()``
    for known observations.  This function extracts the appropriately rounded
    scalar the dashboard expects:

    - ``None`` value → ``None``
    - String value (e.g. weatherText) → the string as-is
    - Numeric value → ``float(formatted)`` (preserves operator StringFormats
      rounding), falling back to ``round(value, 1)`` when ``formatted`` is
      non-numeric (compass ordinate, ``"N/A"``, etc.)

    Values that are NOT ConvertedValue dicts (plain strings, raw numbers,
    arbitrary nested dicts) are returned unchanged — they are unknown /
    passthrough fields that skipped conversion.
    """
    if not isinstance(val, dict) or "value" not in val:
        return val
    raw_val = val["value"]
    formatted_str = val.get("formatted", "")
    if raw_val is None:
        return None
    if isinstance(raw_val, str):
        return raw_val
    # Numeric: parse formatted string back to float to honour StringFormats.
    try:
        return float(formatted_str)
    except (ValueError, TypeError):
        # Non-numeric formatted string (compass ordinate, "N/A") — fall back
        # to the raw converted float rounded to 1 decimal.
        if isinstance(raw_val, float):
            return round(raw_val, 1)
        return raw_val


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
                # extras sub-dict: each entry may itself be a ConvertedValue
                # dict (for known observations transformed by transform_record)
                # or a raw passthrough value (unknown fields).
                if key == "extras" and isinstance(val, dict):
                    flattened_extras: dict[str, object] = {}
                    for sub_key, sub_val in val.items():
                        flattened_extras[sub_key] = _flatten_converted_value(sub_val)
                    flattened[key] = flattened_extras
                    continue

                if not isinstance(val, dict) or "value" not in val:
                    # Unknown / passthrough field — no conversion applied.
                    flattened[key] = val
                    continue
                flattened[key] = _flatten_converted_value(val)
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

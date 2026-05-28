"""REST proxy with enrichment pipeline — BFF layer (ADR-041).

Forwards /api/v1/{path} requests to the upstream clearskies-api service,
applies registered enrichment functions to successful JSON GET responses,
and returns the result to the caller.

Single responsibility: reverse-proxy REST requests, apply enrichments.
No caching, no business logic, no persistence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Type alias for an enrichment function: receives a dict and returns a (possibly
# modified) dict.  Enrichments must not mutate the input in place — they should
# return a new dict or a copy so callers can reason about state.
EnrichmentFn = Callable[[dict[str, Any]], dict[str, Any]]

# Headers that must not be forwarded between the proxy and upstream.
# RFC 7230 §6.1 hop-by-hop headers.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",  # We send our own Host derived from the upstream URL.
    }
)


# ---------------------------------------------------------------------------
# Enrichment registry
# ---------------------------------------------------------------------------


class EnrichmentRegistry:
    """Maps REST endpoint keys to lists of enrichment functions.

    Endpoint keys are the first path segment of the upstream URL path, e.g.
    "current" for /api/v1/current or /api/v1/current/detail.
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


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_proxy_router(
    client: httpx.AsyncClient,
    upstream_url: str,
    registry: EnrichmentRegistry,
) -> APIRouter:
    """Return an APIRouter with a catch-all /api/v1/{path} proxy route.

    Args:
        client:       Shared httpx async client (lifecycle managed by caller).
        upstream_url: Base URL of the upstream clearskies-api service.
        registry:     Enrichment registry consulted on successful JSON GETs.
    """
    router = APIRouter()

    @router.api_route(
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
            if k.lower() not in _HOP_BY_HOP
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

    return router

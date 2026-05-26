"""Main FastAPI application — GET /sse and BFF proxy /api/v1/* endpoints.

Single responsibility: weewx loop packets → SSE, and proxy/unit-convert
REST requests to the upstream API (ADR-041 BFF role).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse  # type: ignore[import-untyped]

from weewx_clearskies_realtime.config.settings import Settings
from weewx_clearskies_realtime.logging.redaction_filter import request_id_var
from weewx_clearskies_realtime.proxy import close as close_proxy
from weewx_clearskies_realtime.proxy import configure as configure_proxy
from weewx_clearskies_realtime.proxy import router as proxy_router
from weewx_clearskies_realtime.sse.emitter import SSEEmitter
from weewx_clearskies_realtime.units.groups import US_UNITS
from weewx_clearskies_realtime.units.transformer import UnitTransformer

logger = logging.getLogger(__name__)


def _build_transformer(settings: Settings) -> UnitTransformer | None:
    """Build a UnitTransformer from operator unit config, or None if not configured."""
    target_units = settings.units.groups
    if not target_units:
        # No operator unit config — use US_UNITS as passthrough default so the
        # transformer exists and can annotate values even without explicit config.
        # Returning None disables conversion entirely.
        return None
    return UnitTransformer(
        target_units=target_units,
        label_overrides=settings.units.labels or None,
        format_overrides=settings.units.string_formats or None,
        ordinates=settings.units.ordinates,
    )


def create_app(settings: Settings, emitter: SSEEmitter) -> FastAPI:
    """Return the main FastAPI application wired to the given SSEEmitter.

    Mounts the BFF proxy router for /api/v1/* forwarding (ADR-041).
    No OpenAPI docs are exposed in production.
    """
    transformer = _build_transformer(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Startup: configure proxy if upstream URL is set.
        if settings.api.upstream_url:
            configure_proxy(
                upstream_url=settings.api.upstream_url,
                timeout=settings.api.timeout,
                tls_verify=settings.api.tls_verify,
                transformer=transformer,
            )
        else:
            logger.info(
                "No upstream_url configured — BFF proxy disabled. "
                "Set [api] upstream_url in realtime.conf to enable."
            )
        yield
        # Shutdown: close the httpx client.
        await close_proxy()

    app = FastAPI(
        title="weewx-clearskies-realtime",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    # CORS — configurable via settings.sse.allowed_origins (default "*").
    # Operators should restrict to dashboard origin(s) in production.
    # Allow all methods so the proxy can forward POST/PUT/DELETE to the API.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.sse.allowed_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["*"],
    )

    # BFF proxy routes — must be included before the generic exception handler
    # so that /api/v1/* paths are matched first.
    app.include_router(proxy_router)

    @app.get("/sse")
    async def sse_endpoint(request: Request) -> EventSourceResponse:
        """Stream weewx loop packets as Server-Sent Events.

        Each event has:
          type: "loop"
          data: JSON-serialised loop-packet dict
        """
        # Inject a request_id so downstream log records carry it.
        rid = str(uuid.uuid4())
        request_id_var.set(rid)

        sub_q = emitter.subscribe()
        logger.info("SSE client connected", extra={"request_id": rid})

        async def generator() -> AsyncIterator[dict[str, str]]:
            try:
                async for event in emitter.event_generator(sub_q):
                    if await request.is_disconnected():
                        logger.info("SSE client disconnected", extra={"request_id": rid})
                        break
                    yield event
            finally:
                emitter.unsubscribe(sub_q)
                # Do NOT call request_id_var.reset(token) here.
                # The token was created in the sse_endpoint coroutine's context;
                # reset() across context boundaries raises ValueError.  The
                # ContextVar is scoped to this async task and is cleaned up
                # automatically when the task ends.

        return EventSourceResponse(generator())

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """RFC 9457 problem+json for unhandled exceptions."""
        logger.error("Unhandled exception", exc_info=exc)
        return JSONResponse(
            status_code=500,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "Internal Server Error",
                "status": 500,
                "detail": "An unexpected error occurred.",
            },
        )

    return app

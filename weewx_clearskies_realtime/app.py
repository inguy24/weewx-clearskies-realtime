"""Main FastAPI application — GET /sse endpoint.

Single responsibility: accept SSE clients and stream loop packets to them.
All business logic, caching, and data transformation is out of scope for
this service.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse  # type: ignore[import-untyped]

from weewx_clearskies_realtime.config.settings import Settings
from weewx_clearskies_realtime.logging.redaction_filter import request_id_var
from weewx_clearskies_realtime.sse.emitter import SSEEmitter

logger = logging.getLogger(__name__)


def create_app(settings: Settings, emitter: SSEEmitter) -> FastAPI:
    """Return the main FastAPI application wired to the given SSEEmitter.

    No OpenAPI docs are exposed in production — the SSE stream is the only
    public surface.
    """
    app = FastAPI(
        title="weewx-clearskies-realtime",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # CORS — configurable via settings.sse.allowed_origins (default "*").
    # Operators should restrict to dashboard origin(s) in production.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.sse.allowed_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

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

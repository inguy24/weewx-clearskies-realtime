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

    # CORS — default open for now; operators should restrict in production via
    # a reverse proxy or by setting allow_origins in a future config field.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
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
        token = request_id_var.set(rid)

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
                request_id_var.reset(token)

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

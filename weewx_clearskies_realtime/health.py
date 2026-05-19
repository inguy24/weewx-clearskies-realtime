"""Health check endpoints — ADR-030.

Separate FastAPI app on a loopback port.  Two routes:
  GET /health/live   — always 200; just proves the process is alive.
  GET /health/ready  — runs registered readiness probes, aggregates results.

No OpenAPI exposure.  No middleware.  Unauthenticated (loopback-only by default).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Probe result type
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Result returned by a readiness probe function."""

    name: str
    status: str  # "ok" | "warning" | "unhealthy"
    messages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Global probe registry (module-level so adapters can register from anywhere)
# ---------------------------------------------------------------------------

_probes: list[Callable[[], ProbeResult]] = []


def register_readiness_probe(probe_fn: Callable[[], ProbeResult]) -> None:
    """Register a callable that returns a ProbeResult for the /health/ready endpoint."""
    _probes.append(probe_fn)


def clear_readiness_probes() -> None:
    """Clear all registered probes — used in tests."""
    _probes.clear()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_health_app() -> FastAPI:
    """Return a minimal FastAPI application for health endpoints.

    No OpenAPI docs, no redoc, no middleware — deliberately small surface area.
    """
    app = FastAPI(
        title="weewx-clearskies-realtime-health",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        """Liveness probe — always OK if the process is responsive."""
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness() -> JSONResponse:
        """Readiness probe — runs all registered probes and aggregates."""
        results = _run_probes()
        status_code, body = _aggregate(results)
        return JSONResponse(content=body, status_code=status_code)

    return app


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_probes() -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for probe in _probes:
        try:
            results.append(probe())
        except Exception as exc:  # noqa: BLE001
            logger.error("Readiness probe raised an exception", exc_info=exc)
            results.append(
                ProbeResult(name="unknown", status="unhealthy", messages=[str(exc)])
            )
    return results


def _aggregate(results: list[ProbeResult]) -> tuple[int, dict[str, Any]]:
    """Aggregate probe results into an HTTP status code and response body.

    Rules (ADR-030):
    - any "unhealthy" → 503
    - any "warning"   → 200 with top-level status "degraded"
    - all "ok"        → 200 with top-level status "ok"
    """
    checks: dict[str, Any] = {}
    for r in results:
        checks[r.name] = {"status": r.status, "messages": r.messages}

    if any(r.status == "unhealthy" for r in results):
        top_status = "unhealthy"
        status_code = 503
    elif any(r.status == "warning" for r in results):
        top_status = "degraded"
        status_code = 200
    else:
        top_status = "ok"
        status_code = 200

    return status_code, {"status": top_status, "checks": checks}

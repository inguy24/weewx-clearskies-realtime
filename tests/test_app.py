"""Tests for app.py — main FastAPI app.

The /sse endpoint uses EventSourceResponse which keeps a streaming connection
open indefinitely.  Sync TestClient will block on any attempt to consume the
body.  Tests here focus on verifiable aspects without consuming the stream:
  - OpenAPI endpoints are suppressed (404)
  - CORS middleware is wired (OPTIONS pre-flight returns CORS headers)
  - /sse route is registered (405 on non-GET, correct path)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from weewx_clearskies_realtime.app import create_app
from weewx_clearskies_realtime.config.settings import Settings
from weewx_clearskies_realtime.sse.emitter import SSEEmitter


@pytest.fixture()
def settings() -> Settings:
    return Settings()


@pytest.fixture()
def source_queue() -> asyncio.Queue[dict[str, Any]]:
    return asyncio.Queue()


@pytest.fixture()
def emitter(source_queue: asyncio.Queue[dict[str, Any]]) -> SSEEmitter:
    return SSEEmitter(source_queue)


@pytest.fixture()
def client(settings: Settings, emitter: SSEEmitter) -> TestClient:
    app = create_app(settings, emitter)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# OpenAPI not exposed
# ---------------------------------------------------------------------------


def test_main_app_no_openapi(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_main_app_no_redoc(client: TestClient) -> None:
    assert client.get("/redoc").status_code == 404


# ---------------------------------------------------------------------------
# /sse route is registered
# ---------------------------------------------------------------------------


def test_sse_route_exists_method_not_allowed_on_post(client: TestClient) -> None:
    """POST to /sse should 405 (route exists, wrong method)."""
    resp = client.post("/sse")
    assert resp.status_code == 405


def test_unknown_route_is_404(client: TestClient) -> None:
    assert client.get("/nonexistent").status_code == 404


# ---------------------------------------------------------------------------
# CORS middleware — OPTIONS pre-flight
# ---------------------------------------------------------------------------


def test_cors_preflight_returns_allow_origin(settings: Settings, emitter: SSEEmitter) -> None:
    """OPTIONS pre-flight for CORS returns the Access-Control-Allow-Origin header."""
    app = create_app(settings, emitter)
    tc = TestClient(app, raise_server_exceptions=False)
    resp = tc.options(
        "/sse",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    # CORS middleware responds to OPTIONS with allow headers.
    assert resp.headers.get("access-control-allow-origin") == "*"

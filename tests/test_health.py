"""Tests for health.py — probe registry, aggregation, HTTP routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from weewx_clearskies_realtime.health import (
    ProbeResult,
    _aggregate,
    clear_readiness_probes,
    create_health_app,
    register_readiness_probe,
)


@pytest.fixture(autouse=True)
def reset_probes() -> None:
    """Ensure probe registry is clean for each test."""
    clear_readiness_probes()
    yield
    clear_readiness_probes()


# ---------------------------------------------------------------------------
# _aggregate — aggregation logic
# ---------------------------------------------------------------------------


def test_aggregate_all_ok() -> None:
    results = [
        ProbeResult("a", "ok", []),
        ProbeResult("b", "ok", []),
    ]
    code, body = _aggregate(results)
    assert code == 200
    assert body["status"] == "ok"


def test_aggregate_any_warning_is_degraded() -> None:
    results = [
        ProbeResult("a", "ok", []),
        ProbeResult("b", "warning", ["low disk"]),
    ]
    code, body = _aggregate(results)
    assert code == 200
    assert body["status"] == "degraded"


def test_aggregate_any_unhealthy_is_503() -> None:
    results = [
        ProbeResult("a", "ok", []),
        ProbeResult("b", "unhealthy", ["broker down"]),
    ]
    code, body = _aggregate(results)
    assert code == 503
    assert body["status"] == "unhealthy"


def test_aggregate_unhealthy_wins_over_warning() -> None:
    results = [
        ProbeResult("a", "warning", []),
        ProbeResult("b", "unhealthy", []),
    ]
    code, body = _aggregate(results)
    assert code == 503
    assert body["status"] == "unhealthy"


def test_aggregate_empty_probes_is_ok() -> None:
    code, body = _aggregate([])
    assert code == 200
    assert body["status"] == "ok"


def test_aggregate_checks_dict_structure() -> None:
    results = [ProbeResult("mqtt", "ok", []), ProbeResult("disk", "warning", ["low"])]
    _, body = _aggregate(results)
    assert "mqtt" in body["checks"]
    assert body["checks"]["mqtt"]["status"] == "ok"
    assert body["checks"]["disk"]["messages"] == ["low"]


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    app = create_health_app()
    return TestClient(app)


def test_liveness_always_200(client: TestClient) -> None:
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {}}


def test_readiness_no_probes_is_200_ok(client: TestClient) -> None:
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readiness_ok_probe(client: TestClient) -> None:
    register_readiness_probe(lambda: ProbeResult("mqtt", "ok", []))
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["mqtt"]["status"] == "ok"


def test_readiness_unhealthy_probe_returns_503(client: TestClient) -> None:
    register_readiness_probe(
        lambda: ProbeResult("mqtt", "unhealthy", ["broker unreachable"])
    )
    resp = client.get("/health/ready")
    assert resp.status_code == 503


def test_readiness_probe_exception_returns_503(client: TestClient) -> None:
    def bad_probe() -> ProbeResult:
        raise RuntimeError("probe crashed")

    register_readiness_probe(bad_probe)
    resp = client.get("/health/ready")
    assert resp.status_code == 503


def test_health_app_no_openapi(client: TestClient) -> None:
    """Health app must not expose OpenAPI endpoints."""
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404

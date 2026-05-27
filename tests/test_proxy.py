"""Tests for proxy.py — BFF REST proxy with unit conversion.

Tests use respx to mock upstream httpx calls and Starlette TestClient
to exercise the FastAPI route.

Test numbering follows the brief spec (1–8) for auditor cross-reference.
Tests 9–14 cover the observation-envelope shape returned by /current.
"""

from __future__ import annotations

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse

import weewx_clearskies_realtime.proxy as proxy_mod
from weewx_clearskies_realtime.proxy import _infer_us_units, configure, router
from weewx_clearskies_realtime.units.transformer import UnitTransformer


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_UPSTREAM = "http://upstream-api:8765"

# US-system targets used across multiple tests.
_US_TARGETS: dict[str, str] = {
    "group_temperature": "degree_C",
    "group_speed": "km_per_hour",
    "group_pressure": "mbar",
    "group_rain": "mm",
    "group_rainrate": "mm_per_hour",
    "group_altitude": "meter",
    "group_distance": "km",
    "group_direction": "degree_compass",
    "group_radiation": "watt_per_meter_squared",
    "group_percent": "percent",
    "group_moisture": "centibar",
    "group_volt": "volt",
}


@pytest.fixture(autouse=True)
def reset_proxy_state() -> None:
    """Reset module-level proxy state before/after each test."""
    proxy_mod._client = None
    proxy_mod._transformer = None
    proxy_mod._upstream_url = ""
    proxy_mod._tls_verify = False
    yield
    proxy_mod._client = None
    proxy_mod._transformer = None
    proxy_mod._upstream_url = ""
    proxy_mod._tls_verify = False


@pytest.fixture()
def transformer() -> UnitTransformer:
    """A UnitTransformer converting US → Metric."""
    return UnitTransformer(target_units=_US_TARGETS)


@pytest.fixture()
def app_no_transform() -> FastAPI:
    """FastAPI app with proxy configured but no unit transformer."""
    configure(upstream_url=_UPSTREAM, timeout=10, tls_verify=False, transformer=None)
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def app_with_transform(transformer: UnitTransformer) -> FastAPI:
    """FastAPI app with proxy configured with a Metric transformer."""
    configure(upstream_url=_UPSTREAM, timeout=10, tls_verify=False, transformer=transformer)
    app = FastAPI()
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# Test 1: proxy forwards GET request correctly
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_forwards_get(app_no_transform: FastAPI) -> None:
    """1. GET request is forwarded to upstream with correct path and params."""
    respx.get(f"{_UPSTREAM}/api/v1/station").mock(
        return_value=HttpxResponse(200, json={"station": "test"})
    )
    with TestClient(app_no_transform) as client:
        resp = client.get("/api/v1/station")
    assert resp.status_code == 200
    assert resp.json() == {"station": "test"}


# ---------------------------------------------------------------------------
# Test 2: proxy applies unit conversion to JSON with usUnits
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_applies_unit_conversion(app_with_transform: FastAPI) -> None:
    """2. Upstream JSON with usUnits triggers unit conversion."""
    # US system (usUnits=1): 32°F should become 0°C
    upstream_data = {"usUnits": 1, "outTemp": 32.0, "dateTime": 1716000000}
    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(200, json=upstream_data)
    )
    with TestClient(app_with_transform) as client:
        resp = client.get("/api/v1/current")
    assert resp.status_code == 200
    body = resp.json()
    # usUnits and dateTime are metadata — stripped by transformer
    assert "usUnits" not in body
    assert "dateTime" not in body
    # outTemp should be converted to Celsius
    assert "outTemp" in body
    out_temp = body["outTemp"]
    assert isinstance(out_temp, dict)
    assert out_temp["value"] == pytest.approx(0.0, abs=1e-9)
    assert out_temp["label"] == "°C"


# ---------------------------------------------------------------------------
# Test 3: non-JSON response passed through unchanged
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_passthrough_non_json(app_no_transform: FastAPI) -> None:
    """3. Non-JSON upstream response is passed through raw without modification."""
    html_body = b"<html><body>not json</body></html>"
    respx.get(f"{_UPSTREAM}/api/v1/report").mock(
        return_value=HttpxResponse(
            200,
            content=html_body,
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )
    with TestClient(app_no_transform) as client:
        resp = client.get("/api/v1/report")
    assert resp.status_code == 200
    assert resp.content == html_body


# ---------------------------------------------------------------------------
# Test 4: upstream unreachable → 502
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_upstream_unreachable(app_no_transform: FastAPI) -> None:
    """4. ConnectError from upstream yields 502 Bad Gateway."""
    import httpx as _httpx

    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        side_effect=_httpx.ConnectError("connection refused")
    )
    with TestClient(app_no_transform) as client:
        resp = client.get("/api/v1/current")
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Test 5: upstream timeout → 504
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_upstream_timeout(app_no_transform: FastAPI) -> None:
    """5. TimeoutException from upstream yields 504 Gateway Timeout."""
    import httpx as _httpx

    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        side_effect=_httpx.TimeoutException("timed out")
    )
    with TestClient(app_no_transform) as client:
        resp = client.get("/api/v1/current")
    assert resp.status_code == 504
    assert "timeout" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Test 6: proxy not configured → 503
# ---------------------------------------------------------------------------


def test_proxy_no_config() -> None:
    """6. Proxy endpoint with no configuration returns 503 Service Unavailable."""
    # No configure() called — _client and _upstream_url are both empty.
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        resp = client.get("/api/v1/current")
    assert resp.status_code == 503
    assert "not configured" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Test 7: nested records converted
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_nested_records(app_with_transform: FastAPI) -> None:
    """7. Upstream returns {"records": [...]}, each record is converted."""
    upstream_data = {
        "records": [
            {"usUnits": 1, "outTemp": 32.0, "dateTime": 1716000000},
            {"usUnits": 1, "outTemp": 212.0, "dateTime": 1716000060},
        ],
        "count": 2,
    }
    respx.get(f"{_UPSTREAM}/api/v1/archive").mock(
        return_value=HttpxResponse(200, json=upstream_data)
    )
    with TestClient(app_with_transform) as client:
        resp = client.get("/api/v1/archive")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    records = body["records"]
    assert len(records) == 2
    # 32°F → 0°C, 212°F → 100°C
    assert records[0]["outTemp"]["value"] == pytest.approx(0.0, abs=1e-9)
    assert records[1]["outTemp"]["value"] == pytest.approx(100.0, rel=1e-6)


# ---------------------------------------------------------------------------
# Test 8: JSON without usUnits is not converted
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_no_us_units(app_with_transform: FastAPI) -> None:
    """8. JSON response without usUnits is passed through without conversion."""
    upstream_data = {"name": "station-1", "lat": 45.0, "lon": -93.0}
    respx.get(f"{_UPSTREAM}/api/v1/station").mock(
        return_value=HttpxResponse(200, json=upstream_data)
    )
    with TestClient(app_with_transform) as client:
        resp = client.get("/api/v1/station")
    assert resp.status_code == 200
    body = resp.json()
    # No conversion — keys and values unchanged
    assert body == upstream_data


# ---------------------------------------------------------------------------
# Additional: query params forwarded
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_forwards_query_params(app_no_transform: FastAPI) -> None:
    """Query parameters are forwarded to the upstream."""
    route = respx.get(f"{_UPSTREAM}/api/v1/archive")
    route.mock(return_value=HttpxResponse(200, json={"ok": True}))
    with TestClient(app_no_transform) as client:
        resp = client.get("/api/v1/archive?from=2024-01-01&limit=100")
    assert resp.status_code == 200
    # Verify the upstream received the query params
    assert route.called
    called_url = str(route.calls[0].request.url)
    assert "from=2024-01-01" in called_url
    assert "limit=100" in called_url


# ---------------------------------------------------------------------------
# Tests 9–12: _infer_us_units unit tests
# ---------------------------------------------------------------------------


def test_infer_us_units_us_system() -> None:
    """9. °F temperature label → US unit system (code 1)."""
    units_block = {"outTemp": "°F", "windSpeed": "mph", "rain": "in"}
    assert _infer_us_units(units_block) == 1


def test_infer_us_units_metric_system() -> None:
    """10. °C + cm rain → Metric unit system (code 16)."""
    units_block = {"outTemp": "°C", "windSpeed": "km/h", "rain": "cm"}
    assert _infer_us_units(units_block) == 16


def test_infer_us_units_metricwx_system() -> None:
    """11. °C + mm rain → MetricWX unit system (code 17)."""
    units_block = {"outTemp": "°C", "windSpeed": "m/s", "rain": "mm"}
    assert _infer_us_units(units_block) == 17


def test_infer_us_units_unknown_defaults_to_us() -> None:
    """12. Empty or unrecognised label block defaults to US (code 1)."""
    assert _infer_us_units({}) == 1
    assert _infer_us_units({"outTemp": "K"}) == 1


# ---------------------------------------------------------------------------
# Tests 13–14: observation-envelope shape ({data: dict, units: label_dict})
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_converts_observation_envelope_us(app_with_transform: FastAPI) -> None:
    """13. /current US envelope: flat rounded values returned, envelope preserved.

    The proxy flattens transform_record's {value, label, formatted} dicts into
    plain scalar values so the React dashboard can render them directly.  Unit
    labels remain in the top-level "units" dict; the "data" dict holds only values.
    """
    # Upstream returns the clearskies-api /current shape: flat observation dict
    # under "data", display labels under "units".  Values are in US units
    # (°F, mph).  The transformer is configured to convert to Metric.
    upstream_data = {
        "data": {
            "timestamp": "2026-05-27T00:40:00Z",
            "outTemp": 32.0,   # °F → 0°C after conversion
            "windSpeed": 10.0, # mph → ~16.09 km/h
            "outHumidity": 65.0,
            "extras": {"foo": "bar"},
        },
        "units": {
            "outTemp": "°F",
            "windSpeed": "mph",
            "outHumidity": "%",
            "rain": "in",
        },
        "source": "weewx",
        "generatedAt": "2026-05-27T00:44:44Z",
    }
    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(200, json=upstream_data)
    )
    with TestClient(app_with_transform) as client:
        resp = client.get("/api/v1/current")

    assert resp.status_code == 200
    body = resp.json()

    # Envelope-level metadata preserved unchanged.
    assert body["source"] == "weewx"
    assert body["generatedAt"] == "2026-05-27T00:44:44Z"
    # "units" label block must remain in the envelope (provides the labels that
    # were previously embedded in each nested dict).
    assert body["units"] == upstream_data["units"]

    obs = body["data"]

    # outTemp: 32 °F → 0 °C; flat scalar rounded to 1 decimal per StringFormats
    # default for degree_C ("%.1f").  Should be 0.0, not a nested dict.
    assert "outTemp" in obs
    out_temp = obs["outTemp"]
    assert not isinstance(out_temp, dict), (
        f"expected flat scalar, got nested dict {out_temp!r} — "
        "proxy must flatten transform_record output for the /current envelope"
    )
    assert out_temp == pytest.approx(0.0, abs=1e-9)

    # windSpeed: 10 mph → ~16.09 km/h; rounded to 0 decimal per StringFormats
    # default for km_per_hour ("%.0f") → 16.0.
    wind = obs["windSpeed"]
    assert not isinstance(wind, dict), f"expected flat scalar, got {wind!r}"
    assert wind == pytest.approx(16.0, abs=0.5)   # rounded from 16.09 → 16

    # outHumidity: group_percent → percent (no conversion); rounded to 0 decimal
    # per StringFormats default for percent ("%.0f") → 65.0.
    humidity = obs["outHumidity"]
    assert not isinstance(humidity, dict), f"expected flat scalar, got {humidity!r}"
    assert humidity == pytest.approx(65.0, abs=0.5)

    # weatherText: transform_record always adds this; its value is a plain string.
    weather_text = obs.get("weatherText")
    assert isinstance(weather_text, str), (
        f"expected weatherText to be a plain string, got {weather_text!r}"
    )

    # timestamp is a string, not a known observation → passed through raw.
    assert obs["timestamp"] == "2026-05-27T00:40:00Z"

    # extras sub-dict is not a known observation → passed through raw.
    assert obs["extras"] == {"foo": "bar"}


@respx.mock
def test_proxy_observation_envelope_no_transformer(app_no_transform: FastAPI) -> None:
    """14. /current envelope with no transformer → passes through unchanged."""
    upstream_data = {
        "data": {
            "timestamp": "2026-05-27T00:40:00Z",
            "outTemp": 72.5,
        },
        "units": {"outTemp": "°F", "rain": "in"},
        "source": "weewx",
        "generatedAt": "2026-05-27T00:44:44Z",
    }
    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(200, json=upstream_data)
    )
    with TestClient(app_no_transform) as client:
        resp = client.get("/api/v1/current")

    assert resp.status_code == 200
    body = resp.json()
    # No transformer — entire response passed through raw.
    assert body == upstream_data

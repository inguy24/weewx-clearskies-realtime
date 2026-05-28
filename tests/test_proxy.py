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
    proxy_mod._enrichment_registry = None
    yield
    proxy_mod._client = None
    proxy_mod._transformer = None
    proxy_mod._upstream_url = ""
    proxy_mod._tls_verify = False
    proxy_mod._enrichment_registry = None


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

    # extras sub-dict: string values are not known observations → passed through
    # raw (no OBS_GROUP entry for "foo", and the value "bar" is a string so it
    # would not be convertible even if "foo" were registered).
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


# ---------------------------------------------------------------------------
# Tests 15–16: extras sub-dict rounding (bug fix)
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_extras_known_field_rounded(app_with_transform: FastAPI) -> None:
    """15. extras sub-dict: field in OBS_GROUP is converted and rounded.

    inDewpoint is in OBS_GROUP (group_temperature).  A raw float from the
    upstream (58.321456789 °F) must come back as a rounded scalar in the
    display unit (°C, 1-decimal precision per StringFormats default for
    degree_C) rather than passing through as a raw float.
    """
    upstream_data = {
        "data": {
            "outTemp": 72.0,   # 72 °F → 0 °C after conversion (for context)
            "extras": {
                "inDewpoint": 58.321456789,  # °F → °C, should be rounded
                "foo": "bar",               # not in OBS_GROUP → raw passthrough
            },
        },
        "units": {
            "outTemp": "°F",
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
    obs = resp.json()["data"]

    extras = obs["extras"]
    assert isinstance(extras, dict), f"expected extras dict, got {extras!r}"

    # inDewpoint: 58.321456789 °F → ~14.623 °C; rounded to 1 decimal → 14.6
    in_dew = extras["inDewpoint"]
    assert not isinstance(in_dew, dict), (
        f"expected flat scalar, got nested dict {in_dew!r} — "
        "extras field was not flattened by the proxy"
    )
    # Must be rounded — not the raw 58.321456789 leaked through.
    assert in_dew != pytest.approx(58.321456789, rel=1e-6), (
        "inDewpoint was passed through unconverted (still Fahrenheit raw float)"
    )
    # Should be ~14.6°C (1-decimal rounding per degree_C StringFormats default).
    expected_c = (58.321456789 - 32.0) / 1.8
    assert in_dew == pytest.approx(round(expected_c, 1), abs=0.05)

    # foo: unknown field → passed through raw string.
    assert extras["foo"] == "bar"


@respx.mock
def test_proxy_extras_unknown_field_passthrough(app_with_transform: FastAPI) -> None:
    """16. extras sub-dict: field NOT in OBS_GROUP passes through unchanged.

    luminosity is not a standard weewx observation and has no entry in
    OBS_GROUP, so it should pass through as-is (raw float, no conversion).
    """
    upstream_data = {
        "data": {
            "outTemp": 72.0,
            "extras": {
                "luminosity": 12345.678,  # unknown obs → raw passthrough
            },
        },
        "units": {
            "outTemp": "°F",
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
    obs = resp.json()["data"]

    extras = obs["extras"]
    # luminosity has no OBS_GROUP entry → unchanged raw float.
    assert extras["luminosity"] == pytest.approx(12345.678, rel=1e-6)


# ---------------------------------------------------------------------------
# Test 1.3: POST, PUT, DELETE are forwarded with correct method and body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,status_code", [
    ("POST", 201),
    ("PUT", 200),
    ("DELETE", 204),
])
@respx.mock
def test_proxy_forwards_non_get_methods(
    method: str, status_code: int, app_no_transform: FastAPI
) -> None:
    """1.3: POST, PUT, and DELETE requests are forwarded to upstream with correct
    method, status code, and request body.
    """
    import json as _json

    resource_path = "/api/v1/some/resource"
    upstream_url = f"{_UPSTREAM}{resource_path}"
    body_payload = {"name": "test-resource", "value": 42}
    body_bytes = _json.dumps(body_payload).encode()

    response_body = {"id": "abc123"} if status_code != 204 else None

    if method == "POST":
        route = respx.post(upstream_url).mock(
            return_value=HttpxResponse(status_code, json=response_body or {})
        )
    elif method == "PUT":
        route = respx.put(upstream_url).mock(
            return_value=HttpxResponse(status_code, json=response_body or {})
        )
    else:  # DELETE
        route = respx.delete(upstream_url).mock(
            return_value=HttpxResponse(status_code, content=b"")
        )

    with TestClient(app_no_transform) as client:
        resp = client.request(
            method,
            resource_path,
            content=body_bytes,
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == status_code
    assert route.called

    # For POST and PUT, verify body was forwarded to upstream.
    if method in ("POST", "PUT"):
        forwarded_body = route.calls[0].request.content
        assert _json.loads(forwarded_body) == body_payload


# ---------------------------------------------------------------------------
# Test 1.5: error responses (4xx/5xx) forwarded with correct status and body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 404, 500])
@respx.mock
def test_proxy_forwards_error_status_codes(
    status_code: int, app_no_transform: FastAPI
) -> None:
    """1.5: 400, 404, and 500 error responses from upstream are forwarded with
    same status code and body.
    """
    error_body = {"error": "test error"}
    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(status_code, json=error_body)
    )
    with TestClient(app_no_transform) as client:
        resp = client.get("/api/v1/current")

    assert resp.status_code == status_code
    assert resp.json() == error_body


# ---------------------------------------------------------------------------
# Test 1.9: enrichment runs on GET /current and merges result
# ---------------------------------------------------------------------------


@respx.mock
def test_enrichment_runs_on_get_current() -> None:
    """1.9: A registered enrichment is applied to a successful GET /current
    response and its output is merged into the returned JSON.
    """
    from weewx_clearskies_realtime.proxy import register_enrichment

    configure(upstream_url=_UPSTREAM, timeout=10, tls_verify=False, transformer=None)
    app = FastAPI()
    app.include_router(router)

    register_enrichment("current", lambda data: {**data, "enriched": True})

    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(200, json={"temp": 72})
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/current")

    assert resp.status_code == 200
    body = resp.json()
    assert body["temp"] == 72
    assert body["enriched"] is True


# ---------------------------------------------------------------------------
# Test 1.10: enrichment failure does not suppress the original response
# ---------------------------------------------------------------------------


@respx.mock
def test_enrichment_failure_still_returns_response() -> None:
    """1.10: When a registered enrichment raises an exception, the original
    upstream data is returned unchanged rather than a 500 error.
    """
    from weewx_clearskies_realtime.proxy import register_enrichment

    configure(upstream_url=_UPSTREAM, timeout=10, tls_verify=False, transformer=None)
    app = FastAPI()
    app.include_router(router)

    def _crashing_enrichment(data: dict) -> dict:
        _ = 1 / 0  # ZeroDivisionError
        return data

    register_enrichment("current", _crashing_enrichment)

    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(200, json={"temp": 72})
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/current")

    assert resp.status_code == 200
    assert resp.json() == {"temp": 72}


# ---------------------------------------------------------------------------
# Test 1.11: multiple enrichments run sequentially and each sees prior output
# ---------------------------------------------------------------------------


@respx.mock
def test_multiple_enrichments_run_sequentially() -> None:
    """1.11: Multiple enrichments registered for the same endpoint run in
    registration order; each enrichment receives the output of the previous one.
    """
    from weewx_clearskies_realtime.proxy import register_enrichment

    configure(upstream_url=_UPSTREAM, timeout=10, tls_verify=False, transformer=None)
    app = FastAPI()
    app.include_router(router)

    register_enrichment("current", lambda d: {**d, "step1": True})
    register_enrichment(
        "current",
        lambda d: {**d, "step2": True, "saw_step1": d.get("step1", False)},
    )

    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(200, json={"temp": 72})
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/current")

    assert resp.status_code == 200
    body = resp.json()
    assert body["step1"] is True
    assert body["step2"] is True
    # saw_step1 proves enrichment 2 saw enrichment 1's output
    assert body["saw_step1"] is True

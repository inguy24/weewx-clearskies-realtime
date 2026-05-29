"""Tests for windDirCardinal / windGustDirCardinal emission.

Covers:
- _degrees_to_index: the shared formula (all 16 sectors, +11.25 offset
  boundaries, the canonical 258°→WSW case).
- _direction_label: operator ordinates override changes the label but does
  NOT change the cardinal (Correction A from lead review).
- proxy.py Shape 2 (_apply_conversion): windDirCardinal and windGustDirCardinal
  are emitted in the /current envelope; null windDir → null windDirCardinal.
- mqtt_fields.py convert_mqtt_packet (SSE live-update path): both cardinal
  fields present in converted MQTT packet (Correction B from lead review).

Test numbering: WC-1 through WC-N for auditor cross-reference.
"""

from __future__ import annotations

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse

import weewx_clearskies_realtime.proxy as proxy_mod
from weewx_clearskies_realtime.mqtt_fields import convert_mqtt_packet
from weewx_clearskies_realtime.proxy import configure, router
from weewx_clearskies_realtime.units.transformer import (
    _DEFAULT_ORDINATES,
    _degrees_to_index,
    UnitTransformer,
)

_UPSTREAM = "http://upstream-api:8765"

# Minimal target-units dict sufficient for direction + temperature (needed to
# infer us_units from the units block in proxy Shape-2 tests).
_TARGETS: dict[str, str] = {
    "group_direction": "degree_compass",
    "group_temperature": "degree_C",
    "group_speed": "km_per_hour",
    "group_rain": "mm",
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
    return UnitTransformer(target_units=_TARGETS)


@pytest.fixture()
def app_with_transform(transformer: UnitTransformer) -> FastAPI:
    configure(upstream_url=_UPSTREAM, timeout=10, tls_verify=False, transformer=transformer)
    app = FastAPI()
    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# WC-1: _degrees_to_index — the canonical 258° → WSW case
# ---------------------------------------------------------------------------


def test_degrees_to_index_258_wsw() -> None:
    """WC-1. 258° falls in the WSW sector (index 11)."""
    idx = _degrees_to_index(258.0)
    assert idx == 11
    assert _DEFAULT_ORDINATES[idx] == "WSW"


# ---------------------------------------------------------------------------
# WC-2: all 16 sectors — mid-point of each 22.5° band maps to correct code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code,mid_deg", [
    ("N",   0.0),
    ("NNE", 22.5),
    ("NE",  45.0),
    ("ENE", 67.5),
    ("E",   90.0),
    ("ESE", 112.5),
    ("SE",  135.0),
    ("SSE", 157.5),
    ("S",   180.0),
    ("SSW", 202.5),
    ("SW",  225.0),
    ("WSW", 247.5),
    ("W",   270.0),
    ("WNW", 292.5),
    ("NW",  315.0),
    ("NNW", 337.5),
])
def test_all_16_sectors_mid_point(code: str, mid_deg: float) -> None:
    """WC-2. Mid-point of each 22.5° band maps to the expected cardinal code."""
    assert _DEFAULT_ORDINATES[_degrees_to_index(mid_deg)] == code


# ---------------------------------------------------------------------------
# WC-3: +11.25° offset — boundary cases
# ---------------------------------------------------------------------------


def test_offset_boundary_just_below_n_wraps_to_n() -> None:
    """WC-3a. 349.0° is just inside the N sector (N starts at 348.75°)."""
    assert _DEFAULT_ORDINATES[_degrees_to_index(349.0)] == "N"


def test_offset_boundary_just_above_nne() -> None:
    """WC-3b. 11.3° is just inside the NNE sector (NNE starts at 11.25°)."""
    assert _DEFAULT_ORDINATES[_degrees_to_index(11.3)] == "NNE"


def test_offset_boundary_exact_sector_edge_n() -> None:
    """WC-3c. 11.25° is exactly the N/NNE boundary — maps to NNE (int truncation)."""
    # int((11.25 + 11.25) / 22.5) % 16 = int(1.0) % 16 = 1 → NNE
    assert _DEFAULT_ORDINATES[_degrees_to_index(11.25)] == "NNE"


def test_offset_boundary_360_wraps_to_n() -> None:
    """WC-3d. 360° wraps to N (same as 0°)."""
    # int((360 + 11.25) / 22.5) % 16 = int(16.5) % 16 = 0 → N
    assert _DEFAULT_ORDINATES[_degrees_to_index(360.0)] == "N"


def test_offset_boundary_0_is_n() -> None:
    """WC-3e. 0° maps to N."""
    assert _DEFAULT_ORDINATES[_degrees_to_index(0.0)] == "N"


# ---------------------------------------------------------------------------
# WC-4: operator ordinates override — Correction A
# Changing self._ordinates changes _direction_label but NOT windDirCardinal.
# ---------------------------------------------------------------------------


def test_operator_ordinates_override_changes_direction_label_not_cardinal() -> None:
    """WC-4. Operator [[ordinates]] override alters _direction_label output but
    does NOT alter the canonical cardinal codes emitted for i18n.

    This verifies Correction A: _degrees_to_index is shared, but the two
    output paths use different label arrays.
    """
    custom_ordinates = [
        "Nord", "NNO", "NO", "ONO",
        "Ost", "OSO", "SO", "SSO",
        "Süd", "SSW", "SW", "WSW",
        "West", "WNW", "NW", "NNW",
    ]
    transformer_custom = UnitTransformer(
        target_units={"group_direction": "degree_compass"},
        ordinates=custom_ordinates,
    )
    transformer_default = UnitTransformer(
        target_units={"group_direction": "degree_compass"},
    )

    # 180° → index 8
    deg = 180.0
    idx = _degrees_to_index(deg)
    assert idx == 8

    # _direction_label uses self._ordinates — custom should return "Süd", default "S"
    assert transformer_custom._direction_label(deg) == "Süd"
    assert transformer_default._direction_label(deg) == "S"

    # Canonical cardinal is always from _DEFAULT_ORDINATES regardless of override
    assert _DEFAULT_ORDINATES[idx] == "S"

    # transform_field label/formatted uses operator override
    result_custom = transformer_custom.transform_field("windDir", deg, "degree_compass")
    assert result_custom["label"] == "Süd"
    assert result_custom["formatted"] == "Süd"

    result_default = transformer_default.transform_field("windDir", deg, "degree_compass")
    assert result_default["label"] == "S"


# ---------------------------------------------------------------------------
# WC-5: null windDir → null windDirCardinal (proxy Shape 2)
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_null_wind_dir_produces_null_cardinal(app_with_transform: FastAPI) -> None:
    """WC-5. null windDir in /current envelope → windDirCardinal is null."""
    upstream_data = {
        "data": {
            "windDir": None,
            "windGustDir": None,
            "outTemp": 32.0,
        },
        "units": {"outTemp": "°F", "rain": "in"},
        "source": "weewx",
        "generatedAt": "2026-05-29T00:00:00Z",
    }
    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(200, json=upstream_data)
    )
    with TestClient(app_with_transform) as client:
        resp = client.get("/api/v1/current")

    assert resp.status_code == 200
    obs = resp.json()["data"]
    assert obs["windDirCardinal"] is None
    assert obs["windGustDirCardinal"] is None


# ---------------------------------------------------------------------------
# WC-6: proxy Shape 2 — windDirCardinal present and correct for 258°
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_wind_dir_258_produces_wsw_cardinal(app_with_transform: FastAPI) -> None:
    """WC-6. windDir=258° in /current envelope → windDirCardinal='WSW'."""
    upstream_data = {
        "data": {
            "windDir": 258.0,
            "windGustDir": 315.0,  # NW
            "outTemp": 72.0,
        },
        "units": {"outTemp": "°F", "rain": "in"},
        "source": "weewx",
        "generatedAt": "2026-05-29T00:00:00Z",
    }
    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(200, json=upstream_data)
    )
    with TestClient(app_with_transform) as client:
        resp = client.get("/api/v1/current")

    assert resp.status_code == 200
    obs = resp.json()["data"]
    assert obs["windDirCardinal"] == "WSW"
    assert obs["windGustDirCardinal"] == "NW"


# ---------------------------------------------------------------------------
# WC-7: proxy Shape 2 — windDirCardinal absent when windDir absent from payload
# ---------------------------------------------------------------------------


@respx.mock
def test_proxy_missing_wind_dir_cardinal_is_null(app_with_transform: FastAPI) -> None:
    """WC-7. windDir absent from /current payload → windDirCardinal is null.

    The field must always be emitted (not omitted) so clients don't need to
    handle both missing-key and null.
    """
    upstream_data = {
        "data": {
            "outTemp": 72.0,
            # windDir and windGustDir deliberately absent
        },
        "units": {"outTemp": "°F", "rain": "in"},
        "source": "weewx",
        "generatedAt": "2026-05-29T00:00:00Z",
    }
    respx.get(f"{_UPSTREAM}/api/v1/current").mock(
        return_value=HttpxResponse(200, json=upstream_data)
    )
    with TestClient(app_with_transform) as client:
        resp = client.get("/api/v1/current")

    assert resp.status_code == 200
    obs = resp.json()["data"]
    # Field must be present even when windDir is absent — always null-safe
    assert "windDirCardinal" in obs
    assert obs["windDirCardinal"] is None
    assert "windGustDirCardinal" in obs
    assert obs["windGustDirCardinal"] is None


# ---------------------------------------------------------------------------
# WC-8: SSE path (mqtt_fields.convert_mqtt_packet) — Correction B
# Both cardinal fields present in converted MQTT packet.
# ---------------------------------------------------------------------------


def test_sse_path_wind_dir_258_produces_wsw_cardinal() -> None:
    """WC-8. MQTT packet with windDir=258 → windDirCardinal='WSW' in SSE output."""
    transformer = UnitTransformer(
        target_units={
            "group_direction": "degree_compass",
            "group_temperature": "degree_C",
            "group_speed": "km_per_hour",
        }
    )
    packet = {
        "windDir": "258",       # suffix-less; source unit looked up via usUnits
        "windGustDir": "315",   # NW
        "outTemp_F": "72.0",
        "usUnits": "1",
    }
    result = convert_mqtt_packet(packet, transformer)

    assert "windDirCardinal" in result, "windDirCardinal must be present in SSE packet"
    assert result["windDirCardinal"] == "WSW"
    assert "windGustDirCardinal" in result, "windGustDirCardinal must be present in SSE packet"
    assert result["windGustDirCardinal"] == "NW"


def test_sse_path_null_wind_dir_produces_null_cardinal() -> None:
    """WC-9. MQTT packet with no windDir → windDirCardinal is null in SSE output."""
    transformer = UnitTransformer(
        target_units={"group_direction": "degree_compass"},
    )
    # Packet with no windDir/windGustDir fields at all
    packet = {"outHumidity": "65", "usUnits": "1"}
    result = convert_mqtt_packet(packet, transformer)

    assert "windDirCardinal" in result
    assert result["windDirCardinal"] is None
    assert "windGustDirCardinal" in result
    assert result["windGustDirCardinal"] is None


def test_sse_path_operator_ordinates_override_does_not_affect_cardinal() -> None:
    """WC-10. Operator [[ordinates]] override in transformer does not change the
    SSE windDirCardinal — cardinal codes are always from _DEFAULT_ORDINATES.
    """
    custom_ordinates = [
        "N", "NNE", "NE", "ENE",
        "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW-custom",   # <-- custom label for index 11
        "W", "WNW", "NW", "NNW",
    ]
    transformer = UnitTransformer(
        target_units={"group_direction": "degree_compass"},
        ordinates=custom_ordinates,
    )
    packet = {"windDir": "258", "usUnits": "1"}
    result = convert_mqtt_packet(packet, transformer)

    # The operator override changes the display label in the windDir entry
    assert result["windDir"]["label"] == "WSW-custom"
    assert result["windDir"]["formatted"] == "WSW-custom"

    # The canonical cardinal code is unaffected by the operator override
    assert result["windDirCardinal"] == "WSW"

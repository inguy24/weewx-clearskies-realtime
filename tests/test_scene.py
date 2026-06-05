"""Tests for the ADR-047 scene builder (scene.py) and related enrichments.

Acceptance criteria verified here:
  AC-1: Every sky label + Foggy + none/unknown maps to a defined bucket.
  AC-2: Storm bucket fires from provider conditions text "Thunderstorm".
  AC-3: Snow overlay fires from precipType in {snow, sleet, freezing-rain}.
  AC-4: Snow wins over rain when both qualify.
  AC-5: Rain overlay fires from rainRate > 0 or precipType = "rain".
  AC-6: Overlay set on detection; present at 14:59; absent at 15:01 (linger).
  AC-7: Linger persists across a simulated reload (server-side state).
  AC-8: Day/night follows almanac sunrise/sunset correctly.
  AC-9: daytime=False when sun times not set (startup / no upstream API).
  AC-10: scene field present in build_scene() output with all three keys.
  AC-11: enrich_scene() injects scene into data["data"] dict.
  AC-12: inject_scene_into_packet() injects scene into SSE loop packets.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from weewx_clearskies_realtime import scene as scene_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    """Return the current UTC time as ISO-8601 with Z suffix."""
    from datetime import UTC, datetime
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_to_utc_iso(epoch: float) -> str:
    from datetime import UTC, datetime
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_scene() -> None:
    """Reset scene module state before each test."""
    scene_mod.reset()
    yield
    scene_mod.reset()


# ---------------------------------------------------------------------------
# AC-1: Sky-label → bucket mapping (ADR-047 §2 table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label,expected_sky", [
    # The three "clear" bucket labels
    ("Clear", "clear"),
    ("Mostly Clear", "clear"),
    ("Partly Cloudy", "clear"),
    # The two "cloudy" bucket labels
    ("Mostly Cloudy", "cloudy"),
    ("Overcast", "cloudy"),
    # Foggy → cloudy (no fog photo per ADR-047)
    ("Foggy", "cloudy"),
    # None / empty / unknown → clear (safe fallback per ADR-047)
    (None, "clear"),
    ("", "clear"),
    ("Unknown", "clear"),
])
def test_sky_label_to_bucket_mapping(label: str | None, expected_sky: str) -> None:
    """AC-1: Every sky label + Foggy + none/unknown maps to a defined bucket."""
    result = scene_mod.build_scene(sky_label=label)
    assert result["sky"] == expected_sky, (
        f"Expected sky={expected_sky!r} for label={label!r}, got {result['sky']!r}"
    )


# ---------------------------------------------------------------------------
# AC-2: Storm bucket from provider thunderstorm text
# ---------------------------------------------------------------------------


def test_storm_from_conditions_text_thunderstorm() -> None:
    """AC-2: 'Thunderstorm' in conditions text triggers storm sky bucket."""
    # The sky_label itself can be the provider text.
    result = scene_mod.build_scene(sky_label="Thunderstorm")
    assert result["sky"] == "storm"


def test_storm_from_conditions_text_thunderstorm_mixed_case() -> None:
    """AC-2: Storm detection is case-insensitive."""
    result = scene_mod.build_scene(sky_label="THUNDERSTORM Heavy Rain")
    assert result["sky"] == "storm"


def test_storm_from_conditions_text_with_qualifier() -> None:
    """AC-2: 'Thunderstorm, Heavy Rain' still fires storm bucket."""
    result = scene_mod.build_scene(sky_label="Thunderstorm, Heavy Rain")
    assert result["sky"] == "storm"


def test_no_storm_without_thunderstorm_keyword() -> None:
    """AC-2: 'Heavy Rain' alone does not trigger storm."""
    result = scene_mod.build_scene(sky_label="Heavy Rain")
    assert result["sky"] == "clear"  # unknown label → clear fallback


# ---------------------------------------------------------------------------
# AC-3: Snow overlay from precipType
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("precip_type", ["snow", "sleet", "freezing-rain"])
def test_snow_overlay_from_precip_type(precip_type: str) -> None:
    """AC-3: precipType in {snow, sleet, freezing-rain} → snow overlay."""
    scene_mod.detect_precip(
        precip_type=precip_type,
        conditions_text=None,
        rain_rate=None,
    )
    result = scene_mod.build_scene(sky_label=None)
    assert result["overlay"] == "snow", (
        f"Expected overlay='snow' for precipType={precip_type!r}, got {result['overlay']!r}"
    )


# ---------------------------------------------------------------------------
# AC-4: Snow wins over rain
# ---------------------------------------------------------------------------


def test_snow_wins_over_rain_when_both_qualify() -> None:
    """AC-4: Snow overlay wins when precipType=snow AND rainRate > 0."""
    scene_mod.detect_precip(
        precip_type="snow",
        conditions_text=None,
        rain_rate=0.15,  # positive rain rate
    )
    result = scene_mod.build_scene(sky_label=None)
    assert result["overlay"] == "snow", (
        "Snow must win over rain when both signals are present"
    )


def test_snow_wins_over_rain_via_precip_type() -> None:
    """AC-4: Snow overlay wins when precipType=sleet AND precipType=rain (impossible, but verify order)."""
    scene_mod.detect_precip(
        precip_type="sleet",
        conditions_text="Thunderstorm",
        rain_rate=0.05,
    )
    result = scene_mod.build_scene(sky_label=None)
    assert result["overlay"] == "snow"


# ---------------------------------------------------------------------------
# AC-5: Rain overlay from various sources
# ---------------------------------------------------------------------------


def test_rain_overlay_from_precip_type_rain() -> None:
    """AC-5: precipType='rain' → rain overlay."""
    scene_mod.detect_precip(precip_type="rain", conditions_text=None, rain_rate=None)
    result = scene_mod.build_scene(sky_label=None)
    assert result["overlay"] == "rain"


def test_rain_overlay_from_positive_rain_rate() -> None:
    """AC-5: rainRate > 0 → rain overlay."""
    scene_mod.detect_precip(precip_type=None, conditions_text=None, rain_rate=0.01)
    result = scene_mod.build_scene(sky_label=None)
    assert result["overlay"] == "rain"


def test_rain_overlay_from_thunderstorm_text() -> None:
    """AC-5: Thunderstorm in conditions_text → rain overlay (storms bring rain)."""
    scene_mod.detect_precip(
        precip_type=None,
        conditions_text="Thunderstorm, Heavy Rain",
        rain_rate=None,
    )
    result = scene_mod.build_scene(sky_label=None)
    assert result["overlay"] == "rain"


def test_no_overlay_without_precip() -> None:
    """AC-5: No precip → overlay=None."""
    result = scene_mod.build_scene(sky_label="Clear")
    assert result["overlay"] is None


def test_no_overlay_zero_rain_rate() -> None:
    """AC-5: rainRate=0 does not trigger rain overlay."""
    scene_mod.detect_precip(precip_type=None, conditions_text=None, rain_rate=0.0)
    result = scene_mod.build_scene(sky_label=None)
    assert result["overlay"] is None


def test_no_overlay_negative_rain_rate() -> None:
    """AC-5: Negative rainRate (sensor fault) does not trigger overlay."""
    scene_mod.detect_precip(precip_type=None, conditions_text=None, rain_rate=-0.5)
    result = scene_mod.build_scene(sky_label=None)
    assert result["overlay"] is None


# ---------------------------------------------------------------------------
# AC-6: Linger boundary — 14:59 present, 15:01 absent
# ---------------------------------------------------------------------------


def test_linger_overlay_present_before_expiry() -> None:
    """AC-6: Overlay is still present at 14:59 (899 seconds) after last detection."""
    scene_mod.detect_precip(precip_type="rain", conditions_text=None, rain_rate=None)

    # Simulate the monotonic clock having advanced 899 seconds since detection.
    # We patch time.monotonic to return a value that is 899 seconds after the
    # stored _last_precip_mono.
    stored_mono = scene_mod._last_precip_mono
    assert stored_mono is not None

    with patch("weewx_clearskies_realtime.scene.time") as mock_time:
        mock_time.monotonic.return_value = stored_mono + 899.0
        mock_time.time.return_value = time.time()  # keep daytime logic working
        result = scene_mod.build_scene(sky_label=None)

    assert result["overlay"] == "rain", (
        "Overlay must still be present 899 s (14:59) after last detection"
    )


def test_linger_overlay_absent_after_expiry() -> None:
    """AC-6: Overlay is absent at 15:01 (901 seconds) after last detection."""
    scene_mod.detect_precip(precip_type="rain", conditions_text=None, rain_rate=None)

    stored_mono = scene_mod._last_precip_mono
    assert stored_mono is not None

    with patch("weewx_clearskies_realtime.scene.time") as mock_time:
        mock_time.monotonic.return_value = stored_mono + 901.0
        mock_time.time.return_value = time.time()
        result = scene_mod.build_scene(sky_label=None)

    assert result["overlay"] is None, (
        "Overlay must be absent 901 s (15:01) after last detection"
    )


def test_linger_resets_on_new_detection() -> None:
    """AC-6: A new detection at T+800 extends the linger to T+800+900."""
    scene_mod.detect_precip(precip_type="rain", conditions_text=None, rain_rate=None)
    first_mono = scene_mod._last_precip_mono
    assert first_mono is not None

    # Second detection at T+800 (within the linger window)
    with patch("weewx_clearskies_realtime.scene.time") as mock_time:
        mock_time.monotonic.return_value = first_mono + 800.0
        mock_time.time.return_value = time.time()
        scene_mod.detect_precip(precip_type="rain", conditions_text=None, rain_rate=None)

    second_mono = scene_mod._last_precip_mono
    assert second_mono is not None
    assert second_mono == pytest.approx(first_mono + 800.0, abs=1.0), (
        "_last_precip_mono should be updated to the time of the new detection"
    )


# ---------------------------------------------------------------------------
# AC-7: Linger persists across simulated reload (server-side state)
# ---------------------------------------------------------------------------


def test_linger_persists_without_reset() -> None:
    """AC-7: Linger state is in module-level variables; it survives a simulated reload.

    A 'reload' in this test is simulated by re-importing the module reference.
    Module-level state is not cleared unless reset() is called explicitly —
    in production, neither the web server nor the SSE emitter calls reset().
    """
    scene_mod.detect_precip(precip_type="snow", conditions_text=None, rain_rate=None)
    stored_mono = scene_mod._last_precip_mono
    assert stored_mono is not None

    # Re-import the module (as a second reference) and verify the state is shared.
    import importlib
    import weewx_clearskies_realtime.scene as reimported

    # Both references point to the same module object.
    assert reimported._last_precip_mono == stored_mono, (
        "Module-level linger state must persist across re-import (simulates reload)"
    )
    reimported_result = reimported.build_scene(sky_label=None)
    assert reimported_result["overlay"] == "snow"


# ---------------------------------------------------------------------------
# AC-8: Day/night from almanac sunrise/sunset
# ---------------------------------------------------------------------------


def test_daytime_true_when_current_time_between_rise_and_set() -> None:
    """AC-8: daytime=True when now is between sunrise and sunset."""
    now = time.time()
    rise = _epoch_to_utc_iso(now - 3600)   # 1 hour ago
    sset = _epoch_to_utc_iso(now + 3600)   # 1 hour from now

    scene_mod.update_sun_times(rise, sset)
    result = scene_mod.build_scene(sky_label=None)
    assert result["daytime"] is True


def test_daytime_false_when_current_time_before_sunrise() -> None:
    """AC-8: daytime=False when now is before sunrise (pre-dawn)."""
    now = time.time()
    rise = _epoch_to_utc_iso(now + 3600)   # 1 hour from now
    sset = _epoch_to_utc_iso(now + 10800)  # 3 hours from now

    scene_mod.update_sun_times(rise, sset)
    result = scene_mod.build_scene(sky_label=None)
    assert result["daytime"] is False


def test_daytime_false_when_current_time_after_sunset() -> None:
    """AC-8: daytime=False when now is after sunset."""
    now = time.time()
    rise = _epoch_to_utc_iso(now - 10800)  # 3 hours ago
    sset = _epoch_to_utc_iso(now - 3600)   # 1 hour ago

    scene_mod.update_sun_times(rise, sset)
    result = scene_mod.build_scene(sky_label=None)
    assert result["daytime"] is False


def test_daytime_follows_sun_not_theme_toggle() -> None:
    """AC-8: daytime is computed from almanac — independent of any theme preference.

    This test verifies there is no 'theme' parameter influencing the result;
    the scene module has no knowledge of the UI theme.
    """
    now = time.time()
    rise = _epoch_to_utc_iso(now - 1800)
    sset = _epoch_to_utc_iso(now + 1800)
    scene_mod.update_sun_times(rise, sset)

    # build_scene() has no theme parameter — by design.
    import inspect
    sig = inspect.signature(scene_mod.build_scene)
    assert "theme" not in sig.parameters, (
        "build_scene() must not accept a 'theme' parameter"
    )
    result = scene_mod.build_scene(sky_label=None)
    assert result["daytime"] is True


# ---------------------------------------------------------------------------
# AC-9: daytime=False on startup / no sun times
# ---------------------------------------------------------------------------


def test_daytime_false_on_startup_no_sun_times() -> None:
    """AC-9: daytime=False when sun times have not been set yet."""
    # reset_scene fixture already reset; no update_sun_times called.
    result = scene_mod.build_scene(sky_label=None)
    assert result["daytime"] is False


def test_update_sun_times_ignores_none() -> None:
    """AC-9: update_sun_times(None, None) does not crash and leaves times unset."""
    scene_mod.update_sun_times(None, None)
    assert scene_mod._sunrise_epoch is None
    assert scene_mod._sunset_epoch is None
    result = scene_mod.build_scene(sky_label=None)
    assert result["daytime"] is False


def test_update_sun_times_ignores_invalid_iso() -> None:
    """AC-9: update_sun_times() silently ignores unparseable strings."""
    scene_mod.update_sun_times("not-a-date", "also-bad")
    result = scene_mod.build_scene(sky_label=None)
    assert result["daytime"] is False


# ---------------------------------------------------------------------------
# AC-10: build_scene() output shape
# ---------------------------------------------------------------------------


def test_build_scene_output_has_all_three_keys() -> None:
    """AC-10: build_scene() always returns a dict with sky, daytime, overlay."""
    result = scene_mod.build_scene(sky_label=None)
    assert set(result.keys()) == {"sky", "daytime", "overlay"}, (
        f"build_scene() must return exactly {{sky, daytime, overlay}}, got {set(result.keys())}"
    )


def test_build_scene_sky_is_valid_bucket() -> None:
    """AC-10: sky field is always one of the three valid buckets."""
    for label in [None, "Clear", "Foggy", "Overcast", "Thunderstorm", "Unknown"]:
        result = scene_mod.build_scene(sky_label=label)
        assert result["sky"] in {"clear", "cloudy", "storm"}, (
            f"Invalid sky bucket {result['sky']!r} for label={label!r}"
        )


def test_build_scene_daytime_is_bool() -> None:
    """AC-10: daytime field is always a bool."""
    result = scene_mod.build_scene(sky_label=None)
    assert isinstance(result["daytime"], bool)


def test_build_scene_overlay_is_valid() -> None:
    """AC-10: overlay field is 'rain', 'snow', or None."""
    for precip, expected in [("rain", "rain"), ("snow", "snow"), (None, None)]:
        scene_mod.reset()
        if precip is not None:
            scene_mod.detect_precip(precip_type=precip, conditions_text=None, rain_rate=None)
        result = scene_mod.build_scene(sky_label=None)
        assert result["overlay"] in {"rain", "snow", None}


# ---------------------------------------------------------------------------
# AC-11: enrich_scene() injects scene into data["data"]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_scene_injects_scene_into_data_dict() -> None:
    """AC-11: enrich_scene() injects scene into data['data'] dict."""
    from weewx_clearskies_realtime.enrichment.scene_enrichment import enrich_scene

    data: dict = {"data": {"outTemp": 72.5, "weatherText": "Partly Cloudy"}}

    # Patch upstream calls so no real HTTP is made in tests.
    with (
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._update_sun_times",
            new_callable=AsyncMock,
        ),
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._fetch_current_precip_type",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await enrich_scene(data)

    assert "scene" in result["data"], "enrich_scene() must inject scene into data['data']"
    scene = result["data"]["scene"]
    assert set(scene.keys()) == {"sky", "daytime", "overlay"}


@pytest.mark.asyncio
async def test_enrich_scene_scene_on_top_level_when_no_data_dict() -> None:
    """AC-11: When data['data'] is not a dict, scene goes to top level."""
    from weewx_clearskies_realtime.enrichment.scene_enrichment import enrich_scene

    data: dict = {"notData": 42}

    with (
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._update_sun_times",
            new_callable=AsyncMock,
        ),
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._fetch_current_precip_type",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await enrich_scene(data)

    assert "scene" in result


@pytest.mark.asyncio
async def test_enrich_scene_uses_precip_type_from_forecast() -> None:
    """AC-11: enrich_scene() uses the precipType returned by _fetch_current_precip_type."""
    from weewx_clearskies_realtime.enrichment.scene_enrichment import enrich_scene

    data: dict = {"data": {"outTemp": 34.0, "weatherText": "Light Snow", "rainRate": 0.0}}

    with (
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._update_sun_times",
            new_callable=AsyncMock,
        ),
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._fetch_current_precip_type",
            new_callable=AsyncMock,
            return_value="snow",
        ),
    ):
        result = await enrich_scene(data)

    scene = result["data"]["scene"]
    assert scene["overlay"] == "snow", (
        "enrich_scene() must set overlay='snow' when precipType='snow'"
    )


@pytest.mark.asyncio
async def test_enrich_scene_storm_from_weather_text() -> None:
    """AC-11: enrich_scene() sets sky='storm' when weatherText contains Thunderstorm."""
    from weewx_clearskies_realtime.enrichment.scene_enrichment import enrich_scene
    from weewx_clearskies_realtime import sky_condition

    sky_condition.reset()  # ensure local classifier returns None

    data: dict = {"data": {"outTemp": 72.0, "weatherText": "Thunderstorm, Heavy Rain"}}

    with (
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._update_sun_times",
            new_callable=AsyncMock,
        ),
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._fetch_current_precip_type",
            new_callable=AsyncMock,
            return_value="rain",
        ),
    ):
        result = await enrich_scene(data)

    scene = result["data"]["scene"]
    assert scene["sky"] == "storm", (
        "enrich_scene() must set sky='storm' when weatherText contains 'Thunderstorm'"
    )


@pytest.mark.asyncio
async def test_enrich_scene_never_raises_on_bad_input() -> None:
    """AC-11: enrich_scene() does not raise; returns fallback scene on any error."""
    from weewx_clearskies_realtime.enrichment.scene_enrichment import enrich_scene

    data: dict = {"data": {"outTemp": None, "weatherText": None}}

    # Simulate _fetch_current_precip_type blowing up.
    with (
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._update_sun_times",
            new_callable=AsyncMock,
            side_effect=RuntimeError("upstream down"),
        ),
        patch(
            "weewx_clearskies_realtime.enrichment.scene_enrichment._fetch_current_precip_type",
            new_callable=AsyncMock,
            side_effect=RuntimeError("upstream down"),
        ),
    ):
        result = await enrich_scene(data)

    # Must not raise; must produce a scene with valid shape.
    assert "scene" in result["data"]
    scene = result["data"]["scene"]
    assert set(scene.keys()) == {"sky", "daytime", "overlay"}


# ---------------------------------------------------------------------------
# AC-12: inject_scene_into_packet() injects scene into SSE loop packets
# ---------------------------------------------------------------------------


def test_inject_scene_into_packet_adds_scene_key() -> None:
    """AC-12: inject_scene_into_packet() adds 'scene' to a loop packet dict."""
    from weewx_clearskies_realtime.enrichment.scene_packet_tap import inject_scene_into_packet

    packet: dict = {"outTemp_degree_F": 72.5, "windSpeed_mile_per_hour": 8.0}
    inject_scene_into_packet(packet)

    assert "scene" in packet, "inject_scene_into_packet() must add 'scene' key to the packet"
    scene = packet["scene"]
    assert set(scene.keys()) == {"sky", "daytime", "overlay"}


def test_inject_scene_into_packet_uses_sky_condition_classify() -> None:
    """AC-12: inject_scene_into_packet() reads sky label from sky_condition.classify()."""
    from weewx_clearskies_realtime import sky_condition
    from weewx_clearskies_realtime.enrichment.scene_packet_tap import inject_scene_into_packet

    sky_condition.reset()  # classifier returns None

    packet: dict = {}
    inject_scene_into_packet(packet)

    # With no sky data, sky must be "clear" (none/unknown → clear fallback).
    assert packet["scene"]["sky"] == "clear"


def test_inject_scene_into_packet_never_raises() -> None:
    """AC-12: inject_scene_into_packet() never raises even if scene module fails."""
    from weewx_clearskies_realtime.enrichment.scene_packet_tap import inject_scene_into_packet

    packet: dict = {}
    with patch(
        "weewx_clearskies_realtime.enrichment.scene_packet_tap.scene_mod.build_scene",
        side_effect=RuntimeError("unexpected"),
    ):
        inject_scene_into_packet(packet)  # must not raise

    # Fallback scene is injected.
    assert "scene" in packet
    assert packet["scene"]["sky"] == "clear"
    assert packet["scene"]["daytime"] is False
    assert packet["scene"]["overlay"] is None


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_detect_precip_with_all_none_does_not_set_state() -> None:
    """detect_precip() with all-None inputs does not set the linger timer."""
    scene_mod.detect_precip(precip_type=None, conditions_text=None, rain_rate=None)
    assert scene_mod._last_precip_mono is None
    result = scene_mod.build_scene(sky_label=None)
    assert result["overlay"] is None


def test_reset_clears_all_state() -> None:
    """reset() clears precip linger, sun times, overlay, and provider sky state."""
    scene_mod.detect_precip(precip_type="rain", conditions_text=None, rain_rate=None)
    scene_mod.update_sun_times(_epoch_to_utc_iso(time.time() - 3600),
                               _epoch_to_utc_iso(time.time() + 3600))
    scene_mod.update_provider_sky("Overcast")

    scene_mod.reset()

    assert scene_mod._last_precip_mono is None
    assert scene_mod._last_precip_overlay is None
    assert scene_mod._sunrise_epoch is None
    assert scene_mod._sunset_epoch is None
    assert scene_mod.get_sun_times_date() is None
    assert scene_mod._provider_sky_label is None


def test_provider_sky_fallback_at_night() -> None:
    """build_scene(None) uses cached provider sky when sky_label is None."""
    scene_mod.update_provider_sky("Overcast")
    result = scene_mod.build_scene(sky_label=None)
    assert result["sky"] == "cloudy", (
        "Provider sky 'Overcast' must map to 'cloudy' bucket via fallback"
    )


def test_provider_sky_fallback_not_used_when_sky_label_present() -> None:
    """build_scene() ignores provider sky when a direct sky_label is provided."""
    scene_mod.update_provider_sky("Overcast")
    result = scene_mod.build_scene(sky_label="Clear")
    assert result["sky"] == "clear", (
        "Direct sky_label must take precedence over cached provider sky"
    )


def test_get_sun_times_date_returns_today_after_update() -> None:
    """get_sun_times_date() returns the date extracted from the sunrise string."""
    from datetime import UTC, datetime
    today_utc = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    now = time.time()
    scene_mod.update_sun_times(_epoch_to_utc_iso(now - 3600), _epoch_to_utc_iso(now + 3600))
    assert scene_mod.get_sun_times_date() == today_utc

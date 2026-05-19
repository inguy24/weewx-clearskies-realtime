"""Tests for adapters/mqtt.py — MQTTAdapter (no real broker; paho mocked)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from weewx_clearskies_realtime.adapters.mqtt import MQTTAdapter
from weewx_clearskies_realtime.config.settings import MQTTSettings


@pytest.fixture()
def settings() -> MQTTSettings:
    return MQTTSettings(
        broker_host="localhost",
        broker_port=1883,
        topic="weewx/loop",
        client_id="test-client",
        username="",
        password_env="WEEWX_CLEARSKIES_MQTT_PASSWORD",  # noqa: S106
        tls=False,
        qos=0,
        keepalive=60,
    )


@pytest.fixture()
def queue() -> asyncio.Queue[dict[str, Any]]:
    return asyncio.Queue()


@pytest.fixture()
def adapter(settings: MQTTSettings, queue: asyncio.Queue[dict[str, Any]]) -> MQTTAdapter:
    return MQTTAdapter(settings, queue)


# ---------------------------------------------------------------------------
# Health probe — before any connection
# ---------------------------------------------------------------------------


def test_health_probe_warning_before_connect(adapter: MQTTAdapter) -> None:
    # Disconnected state is "warning" not "unhealthy": /sse still serves, just
    # no events.  ADR-030: degraded → 200; unhealthy → 503.  Finding 3.
    result = adapter.health_probe()
    assert result.status == "warning"
    assert result.name == "mqtt"
    assert len(result.messages) > 0


# ---------------------------------------------------------------------------
# _on_connect callback
# ---------------------------------------------------------------------------


def test_on_connect_rc0_subscribes_and_marks_connected(
    adapter: MQTTAdapter,
) -> None:
    mock_client = MagicMock()
    adapter._on_connect(mock_client, None, {}, rc=0)

    assert adapter.connected is True
    mock_client.subscribe.assert_called_once_with("weewx/loop", qos=0)


def test_on_connect_rc_nonzero_stays_disconnected(adapter: MQTTAdapter) -> None:
    mock_client = MagicMock()
    adapter._on_connect(mock_client, None, {}, rc=5)

    assert adapter.connected is False


# ---------------------------------------------------------------------------
# _on_disconnect callback
# ---------------------------------------------------------------------------


def test_on_disconnect_marks_disconnected(adapter: MQTTAdapter) -> None:
    # Simulate prior connection
    adapter._state.connected = True
    adapter._state.subscribed = True

    adapter._on_disconnect(MagicMock(), None, rc=1)

    assert adapter.connected is False
    assert adapter._state.subscribed is False


# ---------------------------------------------------------------------------
# _on_message callback — queue push
# ---------------------------------------------------------------------------


async def test_on_message_valid_json_pushes_to_queue(
    adapter: MQTTAdapter, queue: asyncio.Queue[dict[str, Any]]
) -> None:
    loop = asyncio.get_running_loop()
    adapter._loop = loop

    packet = {"outTemp": 22.5, "dateTime": 1700000001}
    mock_msg = MagicMock()
    mock_msg.payload = json.dumps(packet).encode()
    mock_msg.topic = "weewx/loop"

    adapter._on_message(MagicMock(), None, mock_msg)

    # call_soon_threadsafe schedules put_nowait; run pending callbacks.
    await asyncio.sleep(0)
    received = queue.get_nowait()
    assert received == packet


async def test_on_message_invalid_json_is_skipped(
    adapter: MQTTAdapter, queue: asyncio.Queue[dict[str, Any]]
) -> None:
    loop = asyncio.get_running_loop()
    adapter._loop = loop

    mock_msg = MagicMock()
    mock_msg.payload = b"not-json{"
    mock_msg.topic = "weewx/loop"

    adapter._on_message(MagicMock(), None, mock_msg)
    await asyncio.sleep(0)

    assert queue.empty()


async def test_on_message_non_dict_json_is_skipped(
    adapter: MQTTAdapter, queue: asyncio.Queue[dict[str, Any]]
) -> None:
    loop = asyncio.get_running_loop()
    adapter._loop = loop

    mock_msg = MagicMock()
    mock_msg.payload = json.dumps([1, 2, 3]).encode()  # list, not dict
    mock_msg.topic = "weewx/loop"

    adapter._on_message(MagicMock(), None, mock_msg)
    await asyncio.sleep(0)

    assert queue.empty()


# ---------------------------------------------------------------------------
# Health probe after connecting
# ---------------------------------------------------------------------------


def test_health_probe_ok_after_connect_and_subscribe(adapter: MQTTAdapter) -> None:
    mock_client = MagicMock()
    adapter._on_connect(mock_client, None, {}, rc=0)

    result = adapter.health_probe()
    assert result.status == "ok"
    assert result.messages == []


# ---------------------------------------------------------------------------
# start() raises if paho-mqtt not installed
# ---------------------------------------------------------------------------


def test_start_raises_on_missing_paho(
    adapter: MQTTAdapter,
) -> None:
    """If paho-mqtt is absent (ImportError), start() raises RuntimeError with install hint."""
    loop = asyncio.new_event_loop()
    try:
        with (
            patch.dict("sys.modules", {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}),
            pytest.raises((RuntimeError, ImportError)),
        ):
            adapter.start(loop)
    finally:
        loop.close()

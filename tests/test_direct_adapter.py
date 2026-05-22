"""Tests for adapters/direct.py -- DirectAdapter (no real socket; asyncio mocked)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from weewx_clearskies_realtime.adapters.direct import DirectAdapter
from weewx_clearskies_realtime.config.settings import DirectSettings


@pytest.fixture()
def settings() -> DirectSettings:
    return DirectSettings(socket_path="/tmp/test-loop.sock")  # noqa: S108


@pytest.fixture()
def queue() -> asyncio.Queue[dict[str, Any]]:
    return asyncio.Queue()


@pytest.fixture()
def adapter(settings: DirectSettings, queue: asyncio.Queue[dict[str, Any]]) -> DirectAdapter:
    return DirectAdapter(settings, queue)


# ---------------------------------------------------------------------------
# connected property -- initial state
# ---------------------------------------------------------------------------


def test_connected_defaults_to_false(adapter: DirectAdapter) -> None:
    assert adapter.connected is False


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


def test_health_probe_warning_before_connect(adapter: DirectAdapter) -> None:
    result = adapter.health_probe()
    assert result.status == "warning"
    assert result.name == "direct"
    assert len(result.messages) > 0


def test_health_probe_ok_when_connected(adapter: DirectAdapter) -> None:
    adapter._connected = True
    result = adapter.health_probe()
    assert result.status == "ok"
    assert result.name == "direct"
    assert result.messages == []


# ---------------------------------------------------------------------------
# start() / stop()
# ---------------------------------------------------------------------------


async def test_start_creates_task(adapter: DirectAdapter) -> None:
    loop = asyncio.get_running_loop()
    with patch.object(adapter, "_run", new_callable=AsyncMock):
        adapter.start(loop)
        assert adapter._task is not None
        # Clean up
        adapter.stop()
        await asyncio.sleep(0)


async def test_stop_cancels_task_and_sets_stop_event(adapter: DirectAdapter) -> None:
    loop = asyncio.get_running_loop()

    # _run will block on stop_event; that is fine for this test.
    async def _blocking() -> None:
        await adapter._stop_event.wait()

    with patch.object(adapter, "_run", side_effect=_blocking):
        adapter.start(loop)
        assert adapter._task is not None
        assert not adapter._stop_event.is_set()

        adapter.stop()
        assert adapter._stop_event.is_set()
        # Give the event loop a tick to process cancellation.
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Packet flow -- happy path
# ---------------------------------------------------------------------------


def _make_mock_connection(lines: list[bytes]) -> tuple[AsyncMock, MagicMock]:
    """Return (reader, writer) mocks that yield *lines* then EOF."""
    reader = AsyncMock()
    # readline() returns each line in sequence, then b"" (EOF).
    reader.readline = AsyncMock(side_effect=lines + [b""])
    writer = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


async def test_packet_flow_valid_json(
    adapter: DirectAdapter, queue: asyncio.Queue[dict[str, Any]]
) -> None:
    """Valid JSON lines are parsed and pushed onto the queue."""
    packet = {"outTemp": 22.5, "dateTime": 1700000001}
    line = (json.dumps(packet) + "\n").encode()
    reader, writer = _make_mock_connection([line])

    with patch(
        "weewx_clearskies_realtime.adapters.direct.asyncio.open_unix_connection",
        return_value=(reader, writer),
    ):
        loop = asyncio.get_running_loop()
        adapter.start(loop)
        # Let the task run until it hits EOF and finishes one connect cycle.
        await asyncio.sleep(0.05)
        adapter.stop()
        await asyncio.sleep(0)

    assert not queue.empty()
    received = queue.get_nowait()
    assert received == packet


async def test_invalid_json_lines_are_skipped(
    adapter: DirectAdapter, queue: asyncio.Queue[dict[str, Any]]
) -> None:
    """Malformed JSON lines do not crash the adapter and are dropped."""
    reader, writer = _make_mock_connection([b"not-json{\n"])

    with patch(
        "weewx_clearskies_realtime.adapters.direct.asyncio.open_unix_connection",
        return_value=(reader, writer),
    ):
        loop = asyncio.get_running_loop()
        adapter.start(loop)
        await asyncio.sleep(0.05)
        adapter.stop()
        await asyncio.sleep(0)

    assert queue.empty()


async def test_non_dict_json_is_skipped(
    adapter: DirectAdapter, queue: asyncio.Queue[dict[str, Any]]
) -> None:
    """JSON arrays and other non-object payloads are dropped."""
    line = (json.dumps([1, 2, 3]) + "\n").encode()
    reader, writer = _make_mock_connection([line])

    with patch(
        "weewx_clearskies_realtime.adapters.direct.asyncio.open_unix_connection",
        return_value=(reader, writer),
    ):
        loop = asyncio.get_running_loop()
        adapter.start(loop)
        await asyncio.sleep(0.05)
        adapter.stop()
        await asyncio.sleep(0)

    assert queue.empty()


# ---------------------------------------------------------------------------
# Reconnection behavior
# ---------------------------------------------------------------------------


async def test_reconnect_on_connection_reset_error(
    adapter: DirectAdapter,
) -> None:
    """ConnectionResetError triggers a reconnect attempt (not a crash)."""
    call_count = 0

    async def _failing_connect(path: str) -> tuple[AsyncMock, MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionResetError("reset")
        # Second call: stop the adapter so the loop exits.
        adapter._stop_event.set()
        raise asyncio.CancelledError

    with patch(
        "weewx_clearskies_realtime.adapters.direct.asyncio.open_unix_connection",
        side_effect=_failing_connect,
    ):
        loop = asyncio.get_running_loop()
        adapter.start(loop)
        # First attempt raises, backoff sleep, second attempt exits via stop.
        # We patch the backoff sleep to avoid real delays.
        _wf_target = "weewx_clearskies_realtime.adapters.direct.asyncio.wait_for"
        with patch(_wf_target, new_callable=AsyncMock):
            await asyncio.sleep(0.1)

    assert call_count >= 1
    assert adapter.connected is False


async def test_reconnect_on_file_not_found(
    adapter: DirectAdapter,
) -> None:
    """FileNotFoundError (socket not yet created) triggers a reconnect attempt."""
    call_count = 0

    async def _missing_socket(path: str) -> tuple[AsyncMock, MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise FileNotFoundError("no such file")
        adapter._stop_event.set()
        raise asyncio.CancelledError

    with patch(
        "weewx_clearskies_realtime.adapters.direct.asyncio.open_unix_connection",
        side_effect=_missing_socket,
    ):
        loop = asyncio.get_running_loop()
        adapter.start(loop)
        _wf_target = "weewx_clearskies_realtime.adapters.direct.asyncio.wait_for"
        with patch(_wf_target, new_callable=AsyncMock):
            await asyncio.sleep(0.1)

    assert call_count >= 1
    assert adapter.connected is False

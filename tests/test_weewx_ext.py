"""Tests for weewx_ext.py -- ClearSkiesLoopRelay.

weewx is not available in the test environment, so it is fully mocked before
the module is imported.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Mock weewx before importing the module under test.
#
# IMPORTANT: sys.modules["weewx"] and sys.modules["weewx.engine"] must be
# real module objects (types.ModuleType), NOT MagicMock instances.  Python's
# class-definition machinery accesses weewx.engine.StdService during
# `class ClearSkiesLoopRelay(weewx.engine.StdService):` at import time.
# If the module is a MagicMock, attribute access returns a new MagicMock,
# making ClearSkiesLoopRelay itself a MagicMock instead of a real class.
# ---------------------------------------------------------------------------


class _FakeStdService:
    """Minimal stand-in for weewx.engine.StdService."""

    def __init__(self, engine: object, config_dict: object) -> None:
        self._bindings: dict[str, object] = {}

    def bind(self, event: str, handler: object) -> None:
        self._bindings[event] = handler


_mock_engine_module = types.ModuleType("weewx.engine")
_mock_engine_module.StdService = _FakeStdService  # type: ignore[attr-defined]

_mock_weewx_module = types.ModuleType("weewx")
_mock_weewx_module.NEW_LOOP_PACKET = "NEW_LOOP_PACKET"  # type: ignore[attr-defined]
_mock_weewx_module.engine = _mock_engine_module  # type: ignore[attr-defined]

sys.modules["weewx"] = _mock_weewx_module
sys.modules["weewx.engine"] = _mock_engine_module

# Now safe to import
from weewx_clearskies_realtime.weewx_ext import ClearSkiesLoopRelay  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(packet: dict[str, object]) -> MagicMock:
    event = MagicMock()
    event.packet = packet
    return event


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def relay(tmp_path: pytest.TempPathFactory) -> ClearSkiesLoopRelay:  # type: ignore[type-arg]
    sock_path = str(tmp_path / "loop.sock")  # type: ignore[operator]
    config_dict = {"ClearSkiesRealtimeRelay": {"socket_path": sock_path}}
    instance = object.__new__(ClearSkiesLoopRelay)
    _FakeStdService.__init__(instance, None, config_dict)
    instance._socket_path = sock_path
    instance._clients = []
    instance._lock = threading.Lock()
    instance._server_socket = None
    instance._accept_thread = None
    instance._running = False
    instance._start_server()
    return instance  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Skip marker for platforms without AF_UNIX
# ---------------------------------------------------------------------------

requires_unix_socket = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="Unix domain sockets not available on this platform",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@requires_unix_socket
def test_socket_created_on_init(relay: ClearSkiesLoopRelay) -> None:
    """The relay socket file exists after init."""
    import os

    assert os.path.exists(relay._socket_path)
    relay.shutDown()


@requires_unix_socket
def test_on_new_loop_packet_writes_json_to_client(relay: ClearSkiesLoopRelay) -> None:
    """A connected client receives a JSON line for each loop packet."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(relay._socket_path)
    client.settimeout(2.0)

    # Give the accept thread a moment to register the client.
    time.sleep(0.05)

    packet = {"outTemp": 21.0, "dateTime": 1700000000}
    relay.on_new_loop_packet(_make_event(packet))

    received = b""
    while b"\n" not in received:
        chunk = client.recv(4096)
        assert chunk, "Connection closed before receiving data"
        received += chunk

    client.close()
    relay.shutDown()

    line = received.split(b"\n")[0]
    parsed = json.loads(line.decode())
    assert parsed == packet


@requires_unix_socket
def test_dead_client_is_removed(relay: ClearSkiesLoopRelay) -> None:
    """A client that raises BrokenPipeError is removed from the client list."""
    dead_client = MagicMock(spec=socket.socket)
    dead_client.sendall.side_effect = BrokenPipeError("broken pipe")

    with relay._lock:
        relay._clients.append(dead_client)

    assert len(relay._clients) == 1

    relay.on_new_loop_packet(_make_event({"outTemp": 20.0}))

    with relay._lock:
        assert dead_client not in relay._clients

    relay.shutDown()


@requires_unix_socket
def test_shutdown_closes_clients_and_removes_socket(relay: ClearSkiesLoopRelay) -> None:
    """shutDown() closes all clients and removes the socket file."""
    import os

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(relay._socket_path)
    time.sleep(0.05)

    relay.shutDown()

    assert not os.path.exists(relay._socket_path)
    with relay._lock:
        assert relay._clients == []

    client.close()


@requires_unix_socket
def test_stale_socket_removed_on_start(tmp_path: pytest.TempPathFactory) -> None:  # type: ignore[type-arg]
    """A stale socket file from a previous crash is removed on startup."""
    import os

    sock_path = str(tmp_path / "stale.sock")  # type: ignore[operator]

    # Create a dummy stale file.
    open(sock_path, "w").close()
    assert os.path.exists(sock_path)

    instance = object.__new__(ClearSkiesLoopRelay)
    _FakeStdService.__init__(instance, None, {})
    instance._socket_path = sock_path
    instance._clients = []
    instance._lock = threading.Lock()
    instance._server_socket = None
    instance._accept_thread = None
    instance._running = False
    instance._start_server()

    # Socket should be a real socket now (not the stale file).
    assert os.path.exists(sock_path)
    instance.shutDown()

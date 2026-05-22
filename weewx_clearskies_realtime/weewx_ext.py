"""weewx service extension -- ClearSkiesLoopRelay.

Hooks NEW_LOOP_PACKET in the weewx engine process and broadcasts each packet
as a JSON line to all connected Unix-socket clients.  The DirectAdapter
(adapters/direct.py) connects to this socket from the realtime service process.

This file is only ever imported inside the weewx process where weewx is already
on sys.path.  Do not import it from the realtime service.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
from typing import Any

import weewx
import weewx.engine

logger = logging.getLogger(__name__)

_DEFAULT_SOCKET_PATH = "/var/run/weewx-clearskies/loop.sock"


class ClearSkiesLoopRelay(weewx.engine.StdService):
    """Weewx service that relays loop packets to a Unix domain socket.

    Each connected client receives newline-terminated JSON lines, one per
    loop packet.  The accept loop runs in a daemon thread so it does not
    block the weewx engine loop.
    """

    def __init__(self, engine: Any, config_dict: Any) -> None:  # noqa: ANN401
        super().__init__(engine, config_dict)

        relay_conf = config_dict.get("ClearSkiesRealtimeRelay", {})
        self._socket_path: str = str(relay_conf.get("socket_path", _DEFAULT_SOCKET_PATH)).strip()

        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._server_socket: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = False

        self._start_server()
        self.bind(weewx.NEW_LOOP_PACKET, self.on_new_loop_packet)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def _start_server(self) -> None:
        sock_dir = os.path.dirname(self._socket_path)
        if sock_dir:
            os.makedirs(sock_dir, exist_ok=True)

        # Remove a stale socket file left by a previous crash.
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._socket_path)
        server.listen(8)
        self._server_socket = server
        self._running = True

        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="clearskies-relay-accept"
        )
        self._accept_thread.start()
        logger.info("ClearSkiesLoopRelay listening on %s", self._socket_path)

    def _accept_loop(self) -> None:
        """Accept incoming connections until _running is cleared."""
        assert self._server_socket is not None
        while self._running:
            try:
                conn, _ = self._server_socket.accept()
            except OSError:
                # Server socket closed by shutDown() -- exit cleanly.
                break
            with self._lock:
                self._clients.append(conn)
            logger.debug("DirectAdapter client connected (%d total)", len(self._clients))

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    def on_new_loop_packet(self, event: Any) -> None:  # noqa: ANN401
        """Serialize the loop packet and broadcast to all connected clients."""
        try:
            line = (json.dumps(event.packet) + "\n").encode()
        except (TypeError, ValueError) as exc:
            logger.warning("Could not serialize loop packet: %s", exc)
            return

        dead: list[socket.socket] = []
        with self._lock:
            for client in self._clients:
                try:
                    client.sendall(line)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    dead.append(client)
            for client in dead:
                self._clients.remove(client)
                with contextlib.suppress(OSError):
                    client.close()

        if dead:
            logger.debug("Removed %d dead client(s)", len(dead))

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutDown(self) -> None:  # noqa: N802 -- weewx naming convention
        """Stop accepting connections and close all client sockets."""
        self._running = False

        if self._server_socket is not None:
            with contextlib.suppress(OSError):
                self._server_socket.close()

        with self._lock:
            for client in self._clients:
                with contextlib.suppress(OSError):
                    client.close()
            self._clients.clear()

        if os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except OSError as exc:
                logger.warning("Could not remove socket file %s: %s", self._socket_path, exc)

        logger.info("ClearSkiesLoopRelay stopped")

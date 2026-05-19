"""MQTT input adapter — ADR-005.

Subscribes to a configured MQTT topic (default: weewx/loop), parses JSON
payloads, and pushes loop-packet dicts into an asyncio.Queue for the SSE
emitter to broadcast.

paho-mqtt is imported lazily.  If the [mqtt] extra is not installed and the
operator sets mode=mqtt, a clear RuntimeError is raised at startup before this
module is imported (see __main__.py).  The lazy import here is a belt-and-
suspenders guard.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from weewx_clearskies_realtime.config.settings import MQTTSettings
from weewx_clearskies_realtime.health import ProbeResult

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class _State:
    connected: bool = False
    subscribed: bool = False


class MQTTAdapter:
    """Paho-MQTT subscriber that fans received loop packets into an asyncio.Queue.

    The paho network loop runs in its own thread (client.loop_start()).  Callbacks
    push packets onto the asyncio queue using loop.call_soon_threadsafe so the
    asyncio event loop is not touched from the paho thread.
    """

    def __init__(self, settings: MQTTSettings, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._settings = settings
        self._queue = queue
        self._state = _State()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None  # paho.mqtt.client.Client — typed loosely to stay importable

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Initialise the paho client and start the background network thread.

        Must be called from the async startup context so we can capture the
        running event loop for thread-safe queue pushes.
        """
        try:
            import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "MQTT mode requires paho-mqtt. Install with: "
                "pip install weewx-clearskies-realtime[mqtt]"
            ) from exc

        self._loop = loop
        client = mqtt.Client(client_id=self._settings.client_id)

        if self._settings.username:
            password = self._settings.password  # resolves env var; logs warning if absent
            client.username_pw_set(self._settings.username, password)

        if self._settings.tls:
            # Use default TLS context (system CA bundle).  Operators needing custom
            # CAs should configure their system trust store.
            client.tls_set()

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        logger.info(
            "Connecting to MQTT broker",
            extra={
                "broker_host": self._settings.broker_host,
                "broker_port": self._settings.broker_port,
                "topic": self._settings.topic,
                "client_id": self._settings.client_id,
            },
        )
        client.connect_async(
            host=self._settings.broker_host,
            port=self._settings.broker_port,
            keepalive=self._settings.keepalive,
        )
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        """Stop the background network thread and disconnect gracefully."""
        if self._client is not None:
            logger.info("Stopping MQTT adapter")
            self._client.loop_stop()
            self._client.disconnect()
            self._state.connected = False
            self._state.subscribed = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._state.connected

    # ------------------------------------------------------------------
    # Health probe
    # ------------------------------------------------------------------

    def health_probe(self) -> ProbeResult:
        """Readiness probe registered with the health sub-system."""
        if self._state.connected and self._state.subscribed:
            return ProbeResult(name="mqtt", status="ok", messages=[])
        messages: list[str] = []
        if not self._state.connected:
            messages.append(
                f"Not connected to broker {self._settings.broker_host}:{self._settings.broker_port}"
            )
        elif not self._state.subscribed:
            messages.append(f"Connected but not subscribed to topic {self._settings.topic}")
        return ProbeResult(name="mqtt", status="unhealthy", messages=messages)

    # ------------------------------------------------------------------
    # Paho callbacks (run in the paho network thread)
    # ------------------------------------------------------------------

    def _on_connect(
        self,
        client: Any,  # noqa: ANN401
        userdata: Any,  # noqa: ANN401
        flags: dict[str, Any],
        rc: int,
    ) -> None:
        if rc == 0:
            self._state.connected = True
            logger.info(
                "MQTT connected; subscribing",
                extra={"topic": self._settings.topic, "qos": self._settings.qos},
            )
            client.subscribe(self._settings.topic, qos=self._settings.qos)
            self._state.subscribed = True
        else:
            logger.error("MQTT connect failed", extra={"return_code": rc})

    def _on_disconnect(
        self,
        client: Any,  # noqa: ANN401
        userdata: Any,  # noqa: ANN401
        rc: int,
    ) -> None:
        self._state.connected = False
        self._state.subscribed = False
        if rc == 0:
            logger.info("MQTT disconnected cleanly")
        else:
            # paho-mqtt's auto-reconnect will retry; we just log and wait.
            logger.warning("MQTT disconnected unexpectedly", extra={"return_code": rc})

    def _on_message(
        self,
        client: Any,  # noqa: ANN401
        userdata: Any,  # noqa: ANN401
        message: Any,  # noqa: ANN401
    ) -> None:
        try:
            payload = json.loads(message.payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                "Malformed MQTT payload — skipping",
                extra={"error": str(exc), "topic": message.topic},
            )
            return

        if not isinstance(payload, dict):
            logger.warning(
                "MQTT payload is not a JSON object — skipping",
                extra={"type": type(payload).__name__, "topic": message.topic},
            )
            return

        if self._loop is not None:
            # Thread-safe push into the asyncio queue from the paho thread.
            self._loop.call_soon_threadsafe(self._queue.put_nowait, payload)
        else:
            logger.warning("Event loop not set; dropping loop packet")

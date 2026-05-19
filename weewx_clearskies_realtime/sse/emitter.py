"""SSE emitter — broadcast pattern for multiple concurrent SSE clients.

One asyncio.Queue receives loop packets from the active input adapter.
A fan-out background task reads from that queue and copies each packet
into every connected client's private subscriber queue.

Each SSE client (GET /sse) gets its own subscriber queue created on connect
and removed on disconnect.  Clients that fall behind and overflow their queue
are silently dropped (nowait put; overflow = drop).

Event format:
  event: loop
  data: <JSON-serialised loop-packet dict>
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

# Subscriber queues larger than this are considered stalled; new packets
# are dropped for that subscriber rather than blocking the fan-out task.
_SUBSCRIBER_QUEUE_MAX = 64


class SSEEmitter:
    """Fan-out from a single source queue to N subscriber queues."""

    def __init__(self, source: asyncio.Queue[dict[str, Any]]) -> None:
        self._source = source
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._fanout_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Schedule the fan-out background task on the running event loop."""
        self._fanout_task = asyncio.get_running_loop().create_task(
            self._fanout(), name="sse-fanout"
        )
        logger.info("SSE fan-out task started")

    def stop(self) -> None:
        """Cancel the fan-out task and notify all subscribers to disconnect."""
        if self._fanout_task and not self._fanout_task.done():
            self._fanout_task.cancel()
            logger.info("SSE fan-out task cancelled")
        # Signal every subscriber to close its generator cleanly.
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)  # sentinel

    # ------------------------------------------------------------------
    # Subscriber management
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        """Create and register a new subscriber queue for one SSE client."""
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self._subscribers.add(q)
        logger.debug("SSE subscriber added", extra={"total": len(self._subscribers)})
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        """Remove a subscriber queue when its client disconnects."""
        self._subscribers.discard(q)
        logger.debug("SSE subscriber removed", extra={"total": len(self._subscribers)})

    # ------------------------------------------------------------------
    # SSE event generator (one per connected client)
    # ------------------------------------------------------------------

    async def event_generator(
        self, q: asyncio.Queue[dict[str, Any] | None]
    ) -> AsyncIterator[dict[str, str]]:
        """Async generator that yields SSE events from a subscriber queue.

        Yields dicts expected by sse-starlette's EventSourceResponse:
          {"event": "loop", "data": "<json>"}

        Stops when a None sentinel is received (client disconnect or shutdown).
        """
        try:
            while True:
                packet = await q.get()
                if packet is None:
                    # Sentinel — emitter is shutting down or client disconnected.
                    break
                yield {"event": "loop", "data": json.dumps(packet, default=str)}
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Fan-out task
    # ------------------------------------------------------------------

    async def _fanout(self) -> None:
        """Read packets from the source queue and copy to all subscriber queues."""
        while True:
            try:
                packet = await self._source.get()
            except asyncio.CancelledError:
                break

            if not self._subscribers:
                continue

            stalled: list[asyncio.Queue[dict[str, Any] | None]] = []
            for sub_q in list(self._subscribers):
                try:
                    sub_q.put_nowait(packet)
                except asyncio.QueueFull:
                    # Subscriber's buffer is full — it's too slow.  Drop and log.
                    stalled.append(sub_q)
                    logger.warning(
                        "SSE subscriber queue full; dropping packet for stalled client"
                    )

            # Remove stalled subscribers so they don't keep piling up dropped packets.
            for sub_q in stalled:
                self.unsubscribe(sub_q)

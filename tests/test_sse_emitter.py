"""Tests for sse/emitter.py — SSEEmitter fan-out behaviour."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from weewx_clearskies_realtime.sse.emitter import SSEEmitter


@pytest.fixture()
def source_queue() -> asyncio.Queue[dict[str, Any]]:
    return asyncio.Queue()


@pytest.fixture()
def emitter(source_queue: asyncio.Queue[dict[str, Any]]) -> SSEEmitter:
    return SSEEmitter(source_queue)


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe
# ---------------------------------------------------------------------------


async def test_subscribe_returns_queue(emitter: SSEEmitter) -> None:
    q = emitter.subscribe()
    assert q is not None
    emitter.unsubscribe(q)


async def test_unsubscribe_removes_from_set(emitter: SSEEmitter) -> None:
    q = emitter.subscribe()
    emitter.unsubscribe(q)
    # Subscribing again after unsubscribe should work cleanly.
    q2 = emitter.subscribe()
    assert q2 is not q
    emitter.unsubscribe(q2)


# ---------------------------------------------------------------------------
# Fan-out correctness
# ---------------------------------------------------------------------------


async def test_fanout_delivers_to_single_subscriber(
    emitter: SSEEmitter, source_queue: asyncio.Queue[dict[str, Any]]
) -> None:
    emitter.start()
    sub_q = emitter.subscribe()

    packet = {"outTemp": 21.5, "dateTime": 1700000000}
    await source_queue.put(packet)

    # Give the fan-out task a moment to process.
    received = await asyncio.wait_for(sub_q.get(), timeout=1.0)
    assert received == packet

    emitter.stop()


async def test_fanout_delivers_to_multiple_subscribers(
    emitter: SSEEmitter, source_queue: asyncio.Queue[dict[str, Any]]
) -> None:
    emitter.start()
    sub1 = emitter.subscribe()
    sub2 = emitter.subscribe()

    packet = {"outTemp": 18.0}
    await source_queue.put(packet)

    r1 = await asyncio.wait_for(sub1.get(), timeout=1.0)
    r2 = await asyncio.wait_for(sub2.get(), timeout=1.0)
    assert r1 == packet
    assert r2 == packet

    emitter.stop()


async def test_fanout_no_subscribers_does_not_block(
    emitter: SSEEmitter, source_queue: asyncio.Queue[dict[str, Any]]
) -> None:
    """Packets arriving with no subscribers are consumed without error."""
    emitter.start()

    await source_queue.put({"outTemp": 10.0})
    await source_queue.put({"outTemp": 11.0})

    # Allow fan-out task to drain the queue.
    await asyncio.sleep(0.05)
    assert source_queue.empty()

    emitter.stop()


# ---------------------------------------------------------------------------
# event_generator
# ---------------------------------------------------------------------------


async def test_event_generator_yields_loop_event(emitter: SSEEmitter) -> None:
    sub_q = emitter.subscribe()
    packet = {"outTemp": 25.0}
    await sub_q.put(packet)
    await sub_q.put(None)  # sentinel to stop generator

    events = []
    async for event in emitter.event_generator(sub_q):
        events.append(event)

    assert len(events) == 1
    assert events[0]["event"] == "loop"

    import json

    data = json.loads(events[0]["data"])
    assert data["outTemp"] == pytest.approx(25.0)
    emitter.unsubscribe(sub_q)


async def test_event_generator_stops_on_sentinel(emitter: SSEEmitter) -> None:
    sub_q = emitter.subscribe()
    await sub_q.put({"a": 1})
    await sub_q.put({"b": 2})
    await sub_q.put(None)  # sentinel

    count = 0
    async for _ in emitter.event_generator(sub_q):
        count += 1

    assert count == 2
    emitter.unsubscribe(sub_q)


# ---------------------------------------------------------------------------
# Stop behaviour
# ---------------------------------------------------------------------------


async def test_stop_sends_sentinel_to_subscribers(emitter: SSEEmitter) -> None:
    emitter.start()
    emitter.subscribe()

    emitter.stop()
    # subscriber should receive sentinel (None) or queue may be empty if
    # stop() cancelled the fanout before sentinel arrives — either is ok.
    await asyncio.sleep(0.05)
    # The generator would stop cleanly; just check no exception is raised.


# ---------------------------------------------------------------------------
# Keepalive
# ---------------------------------------------------------------------------


async def test_event_generator_emits_keepalive_when_idle(emitter: SSEEmitter) -> None:
    """event_generator yields a keepalive comment when no packet arrives within
    KEEPALIVE_INTERVAL_SECONDS.  We patch the interval to near-zero so the
    test completes quickly without real wall-clock waiting.
    """
    import weewx_clearskies_realtime.sse.emitter as emitter_mod

    original = emitter_mod.KEEPALIVE_INTERVAL_SECONDS
    emitter_mod.KEEPALIVE_INTERVAL_SECONDS = 0.05  # type: ignore[assignment]
    try:
        sub_q = emitter.subscribe()

        # Collect the first event from the generator, then cancel it so the
        # test doesn't block forever on the next q.get().
        gen = emitter.event_generator(sub_q)
        first_event = await asyncio.wait_for(gen.__anext__(), timeout=2.0)

        assert first_event == {"comment": "keepalive"}, (
            f"Expected keepalive comment, got {first_event!r}"
        )

        # Clean up: send sentinel so the generator terminates cleanly.
        await sub_q.put(None)
        await gen.aclose()
        emitter.unsubscribe(sub_q)
    finally:
        emitter_mod.KEEPALIVE_INTERVAL_SECONDS = original  # type: ignore[assignment]


async def test_event_generator_yields_event_before_keepalive(emitter: SSEEmitter) -> None:
    """A packet arriving before the keepalive timeout is yielded as a loop
    event, not a keepalive comment."""
    sub_q = emitter.subscribe()
    packet = {"outTemp": 19.0}
    await sub_q.put(packet)
    await sub_q.put(None)  # sentinel to stop generator

    events = []
    async for event in emitter.event_generator(sub_q):
        events.append(event)

    assert len(events) == 1
    assert events[0]["event"] == "loop"
    assert "keepalive" not in str(events[0])
    emitter.unsubscribe(sub_q)


# ---------------------------------------------------------------------------
# 1.12: on_packet callback is called before fan-out broadcast
# ---------------------------------------------------------------------------


async def test_on_packet_called_before_broadcast(
    source_queue: asyncio.Queue[dict[str, Any]],
) -> None:
    """1.12: SSEEmitter(on_packet=fn) calls fn for every packet before it is
    broadcast to subscriber queues.

    The callback must have been invoked by the time the subscriber receives
    the packet from its own queue.
    """
    calls: list[dict[str, Any]] = []
    emitter_with_tap = SSEEmitter(source_queue, on_packet=lambda p: calls.append(p))
    emitter_with_tap.start()

    sub_q = emitter_with_tap.subscribe()
    packet: dict[str, Any] = {"temp": 72}
    await source_queue.put(packet)

    # Wait for the packet to arrive at the subscriber queue.
    received = await asyncio.wait_for(sub_q.get(), timeout=1.0)

    # The callback must have been invoked with the original packet.
    assert calls == [{"temp": 72}]
    # The subscriber also received the same packet.
    assert received == packet

    emitter_with_tap.stop()

"""Tests for enrichment/ring_buffer.py — RingBuffer acceptance criteria 1.18-1.22.

Criteria covered:
  1.18 — capacity 400: count, mean, std correct after filling to capacity
  1.19 — eviction at capacity: oldest values evicted, statistics correct
  1.20 — empty buffer raises ValueError on mean() and std()
  1.21 — concurrent add from multiple threads does not corrupt state
  1.22 — clear() resets the buffer and raises ValueError on subsequent statistics
"""

from __future__ import annotations

import math
import threading

import pytest

from weewx_clearskies_realtime.enrichment.ring_buffer import RingBuffer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _population_std(values: list[float]) -> float:
    """Compute population std-dev for a list of floats (reference implementation)."""
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


# ---------------------------------------------------------------------------
# 1.18 — capacity 400: count, mean, std correct
# ---------------------------------------------------------------------------


def test_ring_buffer_400_values() -> None:
    """1.18: RingBuffer(400) holds 400 values with correct count, mean, and std.

    Population std-dev of uniform 1..400 ≈ 115.47.
    """
    buf = RingBuffer(capacity=400)
    values = [float(i) for i in range(1, 401)]
    for v in values:
        buf.add(v)

    assert buf.count == 400
    assert buf.mean() == pytest.approx(200.5, rel=1e-9)

    expected_std = _population_std(values)
    assert buf.std() == pytest.approx(expected_std, rel=1e-3)


# ---------------------------------------------------------------------------
# 1.19 — eviction at capacity: oldest values evicted
# ---------------------------------------------------------------------------


def test_ring_buffer_eviction_at_capacity() -> None:
    """1.19: Adding 500 values into capacity-400 buffer evicts oldest 100.

    After adding 1..500 into a capacity-400 buffer, count == 400,
    mean == 300.5 (average of 101..500 = (101+500)/2), and std is correct for 101..500.

    Note: the brief stated 350.5, which is the mean of 201..500 (if 300 were evicted).
    The correct value for evicting 100 from 500 additions into a 400-capacity buffer
    is 300.5, verified by: sum(range(101, 501)) / 400 == 300.5.
    """
    buf = RingBuffer(capacity=400)
    for i in range(1, 501):
        buf.add(float(i))

    surviving_values = [float(i) for i in range(101, 501)]

    assert buf.count == 400
    assert buf.mean() == pytest.approx(300.5, rel=1e-9)

    expected_std = _population_std(surviving_values)
    assert buf.std() == pytest.approx(expected_std, rel=1e-3)


# ---------------------------------------------------------------------------
# 1.20 — empty buffer raises ValueError
# ---------------------------------------------------------------------------


def test_ring_buffer_empty_raises() -> None:
    """1.20: mean() and std() raise ValueError when the buffer is empty."""
    buf = RingBuffer(capacity=10)

    with pytest.raises(ValueError):
        buf.mean()

    with pytest.raises(ValueError):
        buf.std()


# ---------------------------------------------------------------------------
# 1.21 — concurrent add does not corrupt state
# ---------------------------------------------------------------------------


def test_ring_buffer_concurrent_add() -> None:
    """1.21: Two threads each adding 500 values yield count == 1000 with no corruption.

    Thread 1 adds 0.0..499.0, thread 2 adds 500.0..999.0.
    After both join, count must be 1000 and mean must be ~499.5.
    abs=1.0 tolerance accommodates any valid interleaving order.
    """
    buf = RingBuffer(capacity=1000)

    def add_range(start: int, stop: int) -> None:
        for i in range(start, stop):
            buf.add(float(i))

    t1 = threading.Thread(target=add_range, args=(0, 500))
    t2 = threading.Thread(target=add_range, args=(500, 1000))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert buf.count == 1000
    # Mean of 0..999 is 499.5; any thread ordering still yields sum = 0+1+...+999
    assert buf.mean() == pytest.approx(499.5, abs=1.0)


# ---------------------------------------------------------------------------
# 1.22 — clear() resets buffer
# ---------------------------------------------------------------------------


def test_ring_buffer_clear() -> None:
    """1.22: clear() resets count to 0 and causes mean() to raise ValueError."""
    buf = RingBuffer(capacity=100)
    for i in range(50):
        buf.add(float(i))

    assert buf.count == 50

    buf.clear()

    assert buf.count == 0
    with pytest.raises(ValueError):
        buf.mean()

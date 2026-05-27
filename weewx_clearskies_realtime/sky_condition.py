"""Sky condition classification from solar radiation (ADR-044).

Uses the clear sky index kc = GHI_measured / GHI_clearsky with temporal
variability analysis over a 30-minute sliding window to classify sky
conditions. maxSolarRad from weewx serves as the clear-sky reference.

Module-level state (the deque buffer) is intentional. The BFF is a
single-process service; the buffer must persist across requests and
packet calls. Use reset() in tests to isolate test cases.
"""

from __future__ import annotations

import math
import time
from collections import deque

# ---------------------------------------------------------------------------
# Rolling buffer configuration
# ---------------------------------------------------------------------------

# 30-minute sliding window at ~5-second MQTT intervals = ~360 entries.
_WINDOW_SECONDS: float = 1800.0

# Minimum samples before a classification is returned. Fewer than this
# means we don't have enough statistical power; caller should fall back to
# provider sky data or omit the sky descriptor.
_MIN_SAMPLES: int = 30

# Night/twilight guard: maxSolarRad below this (W/m²) means solar analysis
# is unreliable. Skip adding to the buffer.
_MIN_SOLAR_RAD: float = 50.0

# Pyranometer noise floor (W/m²). Below this, treat as zero.
_NOISE_FLOOR: float = 0.0

# kc classification thresholds (ADR-044 table).
_KC_CLEAR_THRESHOLD: float = 0.85
_KC_OVERCAST_THRESHOLD: float = 0.40
_SIGMA_HIGH_THRESHOLD: float = 0.10

# Maximum kc (cloud-edge enhancement above clear-sky; Tapakis & Charalambides 2014).
_KC_MAX: float = 1.2

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Stores (timestamp_epoch_seconds, kc) tuples. Deque has no fixed maxlen
# because window eviction is time-based, not count-based.
_buffer: deque[tuple[float, float]] = deque()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def update(
    radiation: float | None,
    max_solar_rad: float | None,
    timestamp: float | None = None,
) -> None:
    """Add a new reading to the rolling buffer.

    Silently skips the reading when:
    - maxSolarRad is None or < 50 W/m² (night/twilight — solar analysis unreliable).
    - radiation is None or < 0 (below pyranometer noise floor).

    Args:
        radiation:     Measured GHI in W/m² from station pyranometer.
        max_solar_rad: Theoretical clear-sky GHI in W/m² from weewx.
        timestamp:     Epoch seconds (defaults to time.time()).
    """
    if timestamp is None:
        timestamp = time.time()

    # Night/twilight guard.
    if max_solar_rad is None or max_solar_rad < _MIN_SOLAR_RAD:
        return

    # Noise floor guard.
    if radiation is None or radiation < _NOISE_FLOOR:
        return

    # Clear sky index, clamped to [0.0, _KC_MAX].
    kc = radiation / max_solar_rad
    kc = min(kc, _KC_MAX)
    kc = max(kc, 0.0)

    _buffer.append((timestamp, kc))

    # Evict entries older than the window.
    cutoff = timestamp - _WINDOW_SECONDS
    while _buffer and _buffer[0][0] < cutoff:
        _buffer.popleft()


def classify() -> str | None:
    """Classify sky condition from the rolling buffer.

    Uses 2D classification on mean(kc) and sigma(kc) per ADR-044 table.

    Returns:
        One of: "Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy",
        "Overcast", or None when the buffer has fewer than _MIN_SAMPLES entries
        (startup or extended night period).
    """
    if len(_buffer) < _MIN_SAMPLES:
        return None

    values = [kc for _, kc in _buffer]
    n = len(values)
    mean_kc = sum(values) / n
    variance = sum((v - mean_kc) ** 2 for v in values) / n
    sigma_kc = math.sqrt(variance)

    # ADR-044 two-dimensional classification table.
    if mean_kc >= _KC_CLEAR_THRESHOLD:
        # High mean — clear or mostly-clear.
        if sigma_kc >= _SIGMA_HIGH_THRESHOLD:
            # High variability: cloud-edge events indicate nearby clouds.
            return "Mostly Clear"
        return "Clear"

    if mean_kc >= _KC_OVERCAST_THRESHOLD:
        # Mid-range mean — partly or mostly cloudy.
        if sigma_kc >= _SIGMA_HIGH_THRESHOLD:
            # High variability: broken cumulus passing overhead.
            return "Partly Cloudy"
        # Low variability: uniform stratus.
        return "Mostly Cloudy"

    # Low mean: overcast regardless of variability.
    return "Overcast"


def is_daytime() -> bool:
    """Return True when the buffer has a recent daytime reading.

    A reading is "recent" if it was added within the last 5 minutes.
    This is used to decide whether to attempt solar-radiation-based
    classification or fall back to provider sky data.
    """
    if not _buffer:
        return False
    now = time.time()
    # _buffer[-1] is the most recently added entry.
    return (now - _buffer[-1][0]) < 300.0


def reset() -> None:
    """Clear the rolling buffer.

    Intended for test isolation only. Not called during normal operation.
    """
    _buffer.clear()

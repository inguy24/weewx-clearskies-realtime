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
# provider sky data or omit the sky descriptor. 36 samples at ~5 s intervals
# = ~3 minutes of minimum observation before classification begins.
_MIN_SAMPLES: int = 36

# Night/twilight guard: maxSolarRad below this (W/m²) means solar analysis
# is unreliable. Skip adding to the buffer.
_MIN_SOLAR_RAD: float = 50.0

# Pyranometer noise floor (W/m²). Below this, treat as zero.
_NOISE_FLOOR: float = 0.0

# kc classification thresholds (ADR-044 amended sigma-first table).
# Both branches use the same 6-tier scale; sigma determines which set of
# kc breakpoints applies.
#
# Low sigma branch (uniform sky):
_KC_CLEAR_THRESHOLD: float = 0.85           # → Clear
_KC_UNIFORM_MOSTLY_CLEAR: float = 0.70      # → Mostly Clear
_KC_UNIFORM_PARTLY_CLOUDY: float = 0.50     # → Partly Cloudy
_KC_UNIFORM_MOSTLY_CLOUDY: float = 0.30     # → Mostly Cloudy
# (below 0.30 → Overcast)
#
# High sigma branch (broken/variable sky):
_KC_MOSTLY_CLEAR_THRESHOLD: float = 0.70    # → Mostly Clear
_KC_MOSTLY_CLOUDY_THRESHOLD: float = 0.40   # → Mostly Cloudy
# (below 0.40 → Mostly Cloudy)
_SIGMA_HIGH_THRESHOLD: float = 0.10

# Maximum kc (cloud-edge enhancement above clear-sky; Tapakis & Charalambides 2014).
_KC_MAX: float = 1.2

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Stores (timestamp_epoch_seconds, kc) tuples. Deque has no fixed maxlen
# because window eviction is time-based, not count-based.
_buffer: deque[tuple[float, float]] = deque()

# Tracks whether the previous update() call was during daytime. Used to detect
# the daytime→night transition so stale daytime readings can be cleared from
# the buffer at sunset (prevents night conditions from inheriting daytime kc).
_was_daytime: bool = False


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

    # Detect daytime→night transition and clear stale daytime readings.
    # This prevents yesterday's clear-sky kc values from persisting into the
    # night period and producing spurious classifications at next sunrise.
    global _was_daytime
    currently_daytime = max_solar_rad is not None and max_solar_rad >= _MIN_SOLAR_RAD
    if _was_daytime and not currently_daytime:
        _buffer.clear()
    _was_daytime = currently_daytime

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

    Uses sigma-first classification per ADR-044 amended table. Sigma (temporal
    variability) is the primary axis because a flat kc curve below clear-sky
    means uniform cloud cover regardless of how much light penetrates.

    Returns:
        One of: "Clear", "Mostly Clear", "Partly Cloudy", "Mostly Cloudy",
        "Overcast", "Heavily Overcast", or None when the buffer has fewer
        than _MIN_SAMPLES entries (startup or extended night period).
    """
    if len(_buffer) < _MIN_SAMPLES:
        return None

    values = [kc for _, kc in _buffer]
    n = len(values)
    mean_kc = sum(values) / n
    variance = sum((v - mean_kc) ** 2 for v in values) / n
    sigma_kc = math.sqrt(variance)

    # Sigma-first classification (ADR-044 amended table).
    # Low sigma = uniform sky. High sigma = broken clouds.
    if sigma_kc < _SIGMA_HIGH_THRESHOLD:
        # Uniform sky — flat kc curve, consistent cloud cover.
        if mean_kc >= _KC_CLEAR_THRESHOLD:
            return "Clear"
        if mean_kc >= _KC_UNIFORM_MOSTLY_CLEAR:
            return "Mostly Clear"
        if mean_kc >= _KC_UNIFORM_PARTLY_CLOUDY:
            return "Partly Cloudy"
        if mean_kc >= _KC_UNIFORM_MOSTLY_CLOUDY:
            return "Mostly Cloudy"
        return "Overcast"
    else:
        # Variable sky — broken clouds, mean determines proportion.
        if mean_kc >= _KC_MOSTLY_CLEAR_THRESHOLD:
            return "Mostly Clear"
        if mean_kc >= _KC_MOSTLY_CLOUDY_THRESHOLD:
            return "Partly Cloudy"
        return "Mostly Cloudy"


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
    """Clear the rolling buffer and reset transition state.

    Intended for test isolation only. Not called during normal operation.
    """
    global _was_daytime
    _buffer.clear()
    _was_daytime = False

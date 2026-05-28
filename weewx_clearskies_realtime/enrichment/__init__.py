"""weewx_clearskies_realtime.enrichment — enrichment primitives package.

Exports the public API for enrichment consumers without coupling them to
the proxy module.  The proxy imports EnrichmentRegistry directly from
proxy.py; this package provides the stateful building blocks.
"""

from weewx_clearskies_realtime.enrichment.packet_tap import (
    clear_processors,
    process_packet,
    register_processor,
)
from weewx_clearskies_realtime.enrichment.ring_buffer import RingBuffer

__all__ = [
    "RingBuffer",
    "clear_processors",
    "process_packet",
    "register_processor",
]

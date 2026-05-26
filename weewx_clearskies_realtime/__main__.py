"""Entry point -- startup sequence and serve_all().

Startup order (per spec):
  1. setup_logging("INFO")          -- bootstrap level
  2. load_settings()
  3. setup_logging(settings.level)  -- operator level
  4. Validate mode (direct | mqtt)
  5. For mqtt: lazy-import paho-mqtt; fail clearly if absent
  6. Build queue, adapter, emitter, apps
  7. asyncio.run(serve_all(...))

Dual-stack binding:
  Each bind_host is resolved via socket.getaddrinfo (never gethostbyname) to
  obtain the full set of (family, address) tuples.  A separate uvicorn server
  is started for each resolved address so both IPv4 and IPv6 are served.
  Per rules/coding.md §1, loopback default binds both 127.0.0.1 and ::1.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import sys
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Address resolution -- IPv4/IPv6 dual-stack (rules/coding.md §1)
# ---------------------------------------------------------------------------


def _resolve_bind_addresses(host: str, port: int) -> list[tuple[str, int]]:
    """Resolve host to all (address, port) pairs using getaddrinfo.

    When host is a loopback alias ("localhost", "127.0.0.1", "::1") this
    returns entries for both IPv4 and IPv6 loopback so the service is
    reachable on both stacks.

    Returns a deduplicated list of (address_string, port) tuples.
    """
    seen: set[str] = set()
    result: list[tuple[str, int]] = []

    # For a bare loopback spec, expand to both families explicitly.
    loopback_aliases = {"localhost", "127.0.0.1", "::1"}
    # getaddrinfo("localhost", ...) may return only one family depending on
    # OS /etc/hosts; enumerate both explicitly for loopback aliases.
    hosts_to_resolve: list[str] = ["127.0.0.1", "::1"] if host in loopback_aliases else [host]

    for h in hosts_to_resolve:
        try:
            infos = socket.getaddrinfo(h, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            logger.warning("getaddrinfo failed for %s:%d -- %s", h, port, exc)
            continue
        for _family, _socktype, _proto, _canonname, sockaddr in infos:
            addr = sockaddr[0]
            if addr not in seen:
                seen.add(addr)
                result.append((addr, port))

    if not result:
        # Fall back to whatever the OS gives us for the raw host string.
        logger.warning("No addresses resolved for %s; using as-is", host)
        result = [(host, port)]

    return result


# ---------------------------------------------------------------------------
# serve_all -- runs both apps until shutdown signal
# ---------------------------------------------------------------------------


async def serve_all(
    main_app: Any,  # FastAPI
    health_app: Any,  # FastAPI
    settings: Any,  # Settings
    adapter: Any,  # MQTTAdapter | DirectAdapter
    sse_emitter: Any,  # SSEEmitter
) -> None:
    """Start the adapter, fan-out, and both uvicorn servers; run until SIGTERM/SIGINT."""
    import uvicorn  # type: ignore[import-untyped]

    loop = asyncio.get_running_loop()

    # --- Start input adapter ---
    adapter.start(loop)

    # --- Start SSE fan-out task ---
    sse_emitter.start()

    # --- Build uvicorn configs for each resolved address ---
    main_addrs = _resolve_bind_addresses(settings.sse.bind_host, settings.sse.bind_port)
    health_addrs = _resolve_bind_addresses(settings.health.bind_host, settings.health.bind_port)

    log_level = settings.logging.level.lower()

    configs: list[uvicorn.Config] = []
    for addr, port in main_addrs:
        configs.append(
            uvicorn.Config(
                app=main_app,
                host=addr,
                port=port,
                log_level=log_level,
                # Uvicorn's own logging is reconfigured by setup_logging() to propagate
                # through the root JSON handler -- disable uvicorn's default setup.
                access_log=True,
            )
        )
        logger.info("SSE endpoint will listen on %s:%d", addr, port)

    for addr, port in health_addrs:
        configs.append(
            uvicorn.Config(
                app=health_app,
                host=addr,
                port=port,
                log_level=log_level,
                access_log=False,  # Health probe traffic excluded from access logs
            )
        )
        logger.info("Health endpoint will listen on %s:%d", addr, port)

    servers = [uvicorn.Server(cfg) for cfg in configs]

    # Register graceful shutdown on SIGTERM / SIGINT.
    shutdown_event = asyncio.Event()

    def _request_shutdown() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows does not support add_signal_handler for all signals.
        with contextlib.suppress(NotImplementedError, OSError):
            loop.add_signal_handler(sig, _request_shutdown)

    # Start all servers concurrently.
    server_tasks = [asyncio.create_task(srv.serve()) for srv in servers]

    # Wait for shutdown signal.
    await shutdown_event.wait()

    logger.info("Shutting down")
    sse_emitter.stop()
    adapter.stop()

    for srv in servers:
        srv.should_exit = True

    # Give servers a moment to drain.
    await asyncio.gather(*server_tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point -- full startup sequence."""
    from weewx_clearskies_realtime.logging.setup import setup_logging

    # Step 1: bootstrap logging at INFO so early errors are visible.
    setup_logging("INFO")

    # Step 2: load configuration.
    from weewx_clearskies_realtime.config.settings import load_settings

    try:
        settings = load_settings()
    except FileNotFoundError as exc:
        logger.error("Configuration not found: %s", exc)
        sys.exit(1)
    except (RuntimeError, ValueError) as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    # Step 3: re-configure logging at operator's chosen level.
    setup_logging(settings.logging.level)

    # Step 4: build queue, adapter, emitter, apps.
    from weewx_clearskies_realtime.app import create_app
    from weewx_clearskies_realtime.health import create_health_app, register_readiness_probe
    from weewx_clearskies_realtime.sse.emitter import SSEEmitter

    packet_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    mode = settings.input.mode

    if mode == "direct":
        from weewx_clearskies_realtime.adapters.direct import DirectAdapter

        adapter: Any = DirectAdapter(settings.input.direct, packet_queue)
        log_extra: dict[str, object] = {
            "mode": mode,
            "socket_path": settings.input.direct.socket_path,
            "sse_port": settings.sse.bind_port,
            "health_port": settings.health.bind_port,
        }
    else:
        # Step 5 (mqtt only): lazy-import paho-mqtt; fail clearly.
        try:
            import paho.mqtt.client  # noqa: F401  # type: ignore[import-untyped]
        except ImportError:
            logger.error(
                "MQTT mode requires paho-mqtt. "
                "Install with: pip install weewx-clearskies-realtime[mqtt]"
            )
            sys.exit(1)

        from weewx_clearskies_realtime.adapters.mqtt import MQTTAdapter

        adapter = MQTTAdapter(settings.mqtt, packet_queue)
        log_extra = {
            "mode": mode,
            "broker": f"{settings.mqtt.broker_host}:{settings.mqtt.broker_port}",
            "topic": settings.mqtt.topic,
            "sse_port": settings.sse.bind_port,
            "health_port": settings.health.bind_port,
        }

    sse_emitter = SSEEmitter(packet_queue)

    main_app = create_app(settings, sse_emitter)
    health_app = create_health_app()

    # Register adapter health probe.
    register_readiness_probe(adapter.health_probe)

    # Register upstream API health probe if proxy is configured (ADR-041).
    if settings.api.upstream_url:
        from weewx_clearskies_realtime.proxy import upstream_health_probe
        register_readiness_probe(upstream_health_probe)

    logger.info("Starting weewx-clearskies-realtime", extra=log_extra)

    # Step 6: run.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_all(main_app, health_app, settings, adapter, sse_emitter))


if __name__ == "__main__":
    main()

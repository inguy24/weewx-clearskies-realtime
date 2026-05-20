# Changelog

All notable changes to weewx-clearskies-realtime are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-1.0: minor version bumps may include breaking changes. Read this file before upgrading.

The cross-repo compatibility matrix (which api/dashboard/realtime versions work together) is in [`clearskies-stack/README.md`](https://github.com/inguy24/weewx-clearskies-stack/blob/master/README.md).

---

## [0.1.0] — 2026-05-19

First public release.

### Added

**Core service (FastAPI / Python 3.12 / sse-starlette / paho-mqtt)**

- `GET /sse` — Server-Sent Events endpoint; streams weewx loop packets as JSON events with type `loop`
- paho-mqtt subscriber with automatic exponential-backoff reconnection (1 s → 120 s)
- Asyncio queue fan-out: packets received on the paho thread are broadcast to all connected SSE clients concurrently
- IPv4/IPv6 dual-stack listener via `socket.getaddrinfo`

**Health**

- `GET /health/live` — liveness check (loopback-only port 8082)
- `GET /health/ready` — readiness check; degrades when not connected to broker or not subscribed

**Configuration**

- ConfigObj/INI config at `/etc/weewx-clearskies/realtime.conf`
- Secret-leak guard: refuses to start if any INI key looks like a credential
- MQTT password loaded from environment variable (never from config file)
- MQTT TLS support with optional CA bundle
- Configurable SSE bind host/port and CORS allowed origins
- `CLEARSKIES_LOG_LEVEL` environment variable overrides log level from config

**Security**

- JSON structured logging
- CORS configurable via `[sse] allowed_origins`
- `pip-audit` and `gitleaks` CI gates

**Distribution**

- `pip install weewx-clearskies-realtime[mqtt]`
- `systemd` unit example (see INSTALL.md)
- Docker image published to `ghcr.io/inguy24/weewx-clearskies-realtime`

### Known limitations

- Only MQTT mode is implemented. Direct mode (reading loop packets from a weewx named pipe or socket) is planned for a future release.
- The service carries only the raw loop-packet dict from weewx. No unit conversion, validation, or filtering is applied.

[0.1.0]: https://github.com/inguy24/weewx-clearskies-realtime/releases/tag/v0.1.0

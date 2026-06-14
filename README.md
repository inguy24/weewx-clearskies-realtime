> **⚠️ DEPRECATED**
>
> This service has been merged into [weewx-clearskies-api](https://github.com/inguy24/weewx-clearskies-api) as of 2026-06-14.
> See [ADR-058](https://github.com/inguy24/weather-belchertown/blob/master/docs/decisions/ADR-058-fold-realtime-into-api.md) for the decision rationale.
>
> The SSE endpoint, enrichment pipeline, unit conversion, and all real-time data processing
> now run within the API service. This repository is archived for reference only.

# weewx-clearskies-realtime

Small Python service that bridges [weewx](https://github.com/weewx/weewx) loop packets to browser clients as [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) (SSE). The dashboard connects to the `/sse` endpoint and receives a live stream of current conditions without polling.

Part of [Clear Skies](https://github.com/inguy24/weewx-clearskies-stack) — a modular, modern weather UI stack for weewx.

Distributed AS-IS under [GPL v3](LICENSE).

---

## What it does

```
weewx loop packets
    │
    │  MQTT topic (default: weewx/loop)
    │  published by weewx-mqtt extension
    │
    ▼
weewx-clearskies-realtime
    │  subscribes via paho-mqtt
    │  fans out to connected SSE clients
    │
    ▼
GET /sse  (Server-Sent Events)
    │
    │  EventSource in the browser
    │
    ▼
weewx-clearskies-dashboard
```

Each SSE event has type `loop` and a JSON payload containing the raw weewx loop-packet fields. The dashboard's `useRealtimeObservation` hook consumes the stream and updates current conditions in real time.

---

## Architecture

The service exposes two ports:

- **`/sse` endpoint** (default `0.0.0.0:8766`, all IPv4 interfaces) — the SSE stream. Sits behind the reverse proxy alongside the API.
- **Health port** (default `127.0.0.1:8082`) — `/health/live` and `/health/ready`. Loopback-only; not exposed to the internet.

The paho-mqtt network loop runs in a background thread. Received packets are pushed into an asyncio queue and fanned out to all connected SSE clients concurrently by an `SSEEmitter` task.

---

## Input modes

In v0.1, only MQTT mode is implemented. The `[input] mode` config key exists for future direct-file mode (reading weewx loop packets from a named pipe or socket). Set `mode = mqtt`.

---

## Quick start

```bash
pip install weewx-clearskies-realtime[mqtt]

# Copy and edit the example config
sudo cp /etc/weewx-clearskies/realtime.conf.example \
     /etc/weewx-clearskies/realtime.conf

# Set the MQTT password in a mode-0600 secrets file
echo "WEEWX_CLEARSKIES_MQTT_PASSWORD=<your-mqtt-password>" \
  | sudo tee /etc/weewx-clearskies/realtime-secrets.env
sudo chmod 0600 /etc/weewx-clearskies/realtime-secrets.env

# Start
source /etc/weewx-clearskies/realtime-secrets.env
weewx-clearskies-realtime

# Verify
curl -N http://localhost:8766/sse
# Expected: data: {"dateTime": ..., "outTemp": ..., ...}  (every weewx loop cycle)
```

For the full stack (API + realtime + dashboard + reverse proxy), use [weewx-clearskies-stack](https://github.com/inguy24/weewx-clearskies-stack).

---

## Prerequisites

- weewx running with the [weewx-mqtt](https://github.com/matthewwall/weewx-mqtt) extension installed and publishing loop packets to an MQTT broker.
- An MQTT broker (e.g. [EMQX](https://www.emqx.io/), [Mosquitto](https://mosquitto.org/)).
- Python 3.12.

---

## Documentation

| Doc | Contents |
|---|---|
| [INSTALL.md](INSTALL.md) | Step-by-step install for native (pip + systemd) and Docker |
| [CONFIG.md](CONFIG.md) | Every config option with defaults and examples |
| [SECURITY.md](SECURITY.md) | Auth model, trust boundaries, vulnerability reporting |
| [CHANGELOG.md](CHANGELOG.md) | Release notes and upgrade guidance |

---

## Sibling repositories

| Repo | Role |
|---|---|
| [weewx-clearskies-api](https://github.com/inguy24/weewx-clearskies-api) | REST API — FastAPI + SQLAlchemy, reads weewx archive DB, calls external data providers |
| [weewx-clearskies-dashboard](https://github.com/inguy24/weewx-clearskies-dashboard) | React SPA — the browser UI |
| [weewx-clearskies-stack](https://github.com/inguy24/weewx-clearskies-stack) | Docker Compose deployment, setup wizard, architecture diagrams |

---

## License

[GNU General Public License v3.0](LICENSE)

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

Distributed AS-IS. See LICENSE for full terms.

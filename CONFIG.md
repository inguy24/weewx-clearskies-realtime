# Configuration — weewx-clearskies-realtime

clearskies-realtime uses a ConfigObj/INI-format config file. The MQTT password is never stored in the config file — it comes from an environment variable.

---

## Config file location

The service searches for `realtime.conf` in this order:

1. `CLEARSKIES_CONFIG` environment variable (if set, used directly)
2. `/etc/weewx-clearskies/realtime.conf`
3. `~/.config/weewx-clearskies/realtime.conf`

The service refuses to start if no config file is found.

---

## Secret-leak guard

Any INI key whose name ends in `_KEY`, `_SECRET`, `_TOKEN`, or `_PASSWORD` (case-insensitive) causes the service to refuse to start with a fatal error. Credentials must come from environment variables.

---

## [input] — data source

| Key | Default | Description |
|---|---|---|
| `mode` | `mqtt` | Input mode. Only `mqtt` is implemented in v0.1. |

---

## [input.mqtt] — MQTT broker connection

These keys are nested under `[[mqtt]]` inside `[input]`.

| Key | Default | Description |
|---|---|---|
| `broker_host` | `localhost` | MQTT broker hostname or IP. Accepts IPv4 (e.g. `192.0.2.1`) and IPv6 (e.g. `2001:db8::1`). |
| `broker_port` | `1883` | MQTT broker TCP port. Use `8883` for TLS. |
| `topic` | `weewx/loop` | MQTT topic to subscribe to. Must match the topic configured in the weewx-mqtt extension. |
| `client_id` | `weewx-clearskies-realtime` | MQTT client identifier. Must be unique on the broker. |
| `username` | _(empty)_ | MQTT username. Leave empty for anonymous brokers. |
| `password_env` | `WEEWX_CLEARSKIES_MQTT_PASSWORD` | Name of the environment variable that holds the MQTT password. The variable itself is not the password — it is the name of the variable to read. |
| `tls` | `false` | Enable TLS for the broker connection. Set to `true` when connecting on port 8883. |
| `ca_file` | _(empty)_ | Path to a PEM CA bundle for broker TLS verification. Empty uses the system CA bundle. |
| `qos` | `0` | MQTT QoS level for the subscription. |
| `keepalive` | `60` | MQTT keepalive interval in seconds. |

**Password resolution:** at runtime, the service reads the environment variable named by `password_env`. For example, with the default `password_env = WEEWX_CLEARSKIES_MQTT_PASSWORD`, the service reads `os.environ["WEEWX_CLEARSKIES_MQTT_PASSWORD"]`. If `username` is set but the variable is missing from the environment, a warning is logged and the connection is attempted without a password.

**Reconnection:** paho-mqtt's built-in exponential backoff reconnects automatically from 1 s to 120 s on disconnect. No operator configuration needed.

**Example — local broker, no auth:**

```ini
[input]
mode = mqtt

    [[mqtt]]
    broker_host = 127.0.0.1
    broker_port = 1883
    topic = weewx/loop
    client_id = weewx-clearskies-realtime
```

**Example — remote broker with auth and TLS:**

```ini
[input]
mode = mqtt

    [[mqtt]]
    broker_host = mqtt.example.com
    broker_port = 8883
    topic = weewx/loop
    client_id = weewx-clearskies-realtime
    username = weewx_reader
    password_env = WEEWX_CLEARSKIES_MQTT_PASSWORD
    tls = true
    ca_file = /etc/ssl/certs/ca-certificates.crt
```

With `WEEWX_CLEARSKIES_MQTT_PASSWORD=<password>` in the secrets file.

**Example — IPv6 broker:**

```ini
[input]
mode = mqtt

    [[mqtt]]
    broker_host = 2001:db8::1
    broker_port = 1883
    topic = weewx/loop
```

---

## [sse] — SSE endpoint

| Key | Default | Description |
|---|---|---|
| `bind_host` | `0.0.0.0` | Bind address for the SSE endpoint. Default `0.0.0.0` binds all IPv4 interfaces. Use `127.0.0.1` for loopback-only (single-host deploys behind a reverse proxy). |
| `bind_port` | `8766` | TCP port for the `/sse` endpoint. |
| `allowed_origins` | `*` | Comma-separated CORS origins. Default `*` allows any origin. Restrict to your dashboard's origin in production (e.g. `https://weather.example.com`). |

**Note on bind_host:** the service resolves `bind_host` via `socket.getaddrinfo` and starts one uvicorn server per resolved address. `0.0.0.0` binds all IPv4 interfaces (the default); `localhost` expands to both `127.0.0.1` and `::1` (loopback dual-stack). Do NOT use `::` — uvicorn sets `IPV6_V6ONLY=1` on IPv6 sockets, so `::` is IPv6-only regardless of the kernel `net.ipv6.bindv6only` setting. The same applies to MariaDB `my.cnf`: use `bind-address = *` (not `::`) to bind all interfaces.

**Example — restrict to loopback (single-host deploy):**

```ini
[sse]
bind_host = 127.0.0.1
bind_port = 8766
allowed_origins = https://weather.example.com
```

---

## [health] — health check port

| Key | Default | Description |
|---|---|---|
| `bind_host` | `127.0.0.1` | Bind address. Loopback only — never expose the health port to the internet. |
| `bind_port` | `8082` | TCP port for `/health/live` and `/health/ready`. |

Endpoints:
- `GET /health/live` — returns `{"status": "ok"}` as long as the process is running.
- `GET /health/ready` — returns `{"status": "ok"}` when connected to the broker and subscribed to the topic; `{"status": "degraded"}` otherwise (the SSE stream stays up but emits no events when disconnected).

---

## [logging] — log output

| Key | Default | Description |
|---|---|---|
| `level` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. Overridden by the `CLEARSKIES_LOG_LEVEL` environment variable. |

Logs are structured JSON (one object per line).

---

## Environment variables

| Variable | Description |
|---|---|
| `CLEARSKIES_CONFIG` | Path to `realtime.conf` (overrides the default search path) |
| `CLEARSKIES_LOG_LEVEL` | Log level override (e.g. `DEBUG`) |
| `WEEWX_CLEARSKIES_MQTT_PASSWORD` | MQTT broker password (default `password_env` value) |

Only `WEEWX_CLEARSKIES_MQTT_PASSWORD` is a secret. Place it in a mode-0600 file loaded by the systemd `EnvironmentFile` directive:

```bash
# /etc/weewx-clearskies/realtime-secrets.env
WEEWX_CLEARSKIES_MQTT_PASSWORD=<your-mqtt-password>
```

If you use a different environment variable name for the password, set `password_env` in the config to match.

---

## Complete example config

```ini
[input]
mode = mqtt

    [[mqtt]]
    broker_host = 127.0.0.1
    broker_port = 1883
    topic = weewx/loop
    client_id = weewx-clearskies-realtime
    username =
    password_env = WEEWX_CLEARSKIES_MQTT_PASSWORD
    tls = false
    ca_file =
    qos = 0
    keepalive = 60

[sse]
bind_host = 0.0.0.0
bind_port = 8766
allowed_origins = *

[health]
bind_host = 127.0.0.1
bind_port = 8082

[logging]
level = INFO
```

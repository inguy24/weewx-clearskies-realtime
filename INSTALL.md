# Installation — weewx-clearskies-realtime

This document covers installing and running clearskies-realtime. For the complete stack (API + realtime service + dashboard + reverse proxy), see [weewx-clearskies-stack](https://github.com/inguy24/weewx-clearskies-stack).

---

## Supported environments

| Environment | Recommended install path | Notes |
|---|---|---|
| Debian / Ubuntu (native) | pip + systemd | Recommended for Linux operators already running weewx natively |
| Raspberry Pi OS | pip + systemd | Same as Debian/Ubuntu; Pi OS is Debian-based |
| LXD container (Ubuntu 24.04) | pip + systemd | Supported; used in development |
| Docker / docker-compose | docker-compose via stack repo | Simplest path; reverse proxy included |
| Proxmox VM (Ubuntu 24.04 guest) | pip + systemd or Docker | Same as native Ubuntu |
| macOS | pip | `launchd` service management on macOS; Docker Desktop also works |
| Windows | Docker Desktop with stack repo | Native Python install on Windows is unsupported |

---

## System requirements

| Requirement | Minimum | Notes |
|---|---|---|
| Python | 3.12 | Earlier versions are not supported |
| weewx | 5.x | Running with an MQTT extension publishing loop packets |
| MQTT broker | Any broker supporting MQTT 3.1.1 | EMQX, Mosquitto, or similar |
| weewx-mqtt extension | Any recent version | Publishes loop packets to the broker |

---

## Native install (pip + systemd)

### 1. Install the package with the MQTT extra

The `mqtt` extra installs `paho-mqtt`. It is required for v0.1 (only MQTT mode is implemented).

```bash
pip install weewx-clearskies-realtime[mqtt]
```

Or, in a virtual environment:

```bash
python3.12 -m venv /opt/weewx-clearskies/venv
/opt/weewx-clearskies/venv/bin/pip install "weewx-clearskies-realtime[mqtt]"
```

### 2. Create the configuration directory

```bash
sudo mkdir -p /etc/weewx-clearskies
```

### 3. Create the config file

Create `/etc/weewx-clearskies/realtime.conf`. A minimal config for a local broker with no authentication:

```ini
[input]
mode = mqtt

    [[mqtt]]
    broker_host = 127.0.0.1
    broker_port = 1883
    topic = weewx/loop
    client_id = weewx-clearskies-realtime

[sse]
bind_host = 0.0.0.0
bind_port = 8766
allowed_origins = https://weather.example.com

[health]
bind_host = 127.0.0.1
bind_port = 8082

[logging]
level = INFO
```

See [CONFIG.md](CONFIG.md) for every available option.

### 4. Set MQTT credentials

If your broker requires authentication, add the password to a mode-0600 secrets file:

```bash
sudo tee /etc/weewx-clearskies/realtime-secrets.env <<'EOF'
WEEWX_CLEARSKIES_MQTT_PASSWORD=<your-mqtt-password>
EOF
sudo chmod 0600 /etc/weewx-clearskies/realtime-secrets.env
```

The `[input.mqtt] username` and `password_env` config keys tell the service where to look (see CONFIG.md). The password is never stored in `realtime.conf`.

### 5. Configure the reverse proxy

The `/sse` endpoint must be accessible to browser clients. SSE requires the proxy to disable buffering.

**Nginx example:**

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name weather.example.com;

    # SSE — disable buffering, keep connection open
    location /sse {
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
    }
}
```

**Caddy example:**

```caddy
weather.example.com {
    handle /sse {
        reverse_proxy 127.0.0.1:8766
    }
}
```

Caddy disables response buffering automatically for SSE.

**Apache example:**

```apache
<Location /sse>
    ProxyPass http://127.0.0.1:8766/sse
    ProxyPassReverse http://127.0.0.1:8766/sse
    SetEnv proxy-nokeepalive 1
    SetEnv force-proxy-request-1.0 1
</Location>
```

### 6. Install the systemd unit

```bash
sudo tee /etc/systemd/system/weewx-clearskies-realtime.service <<'EOF'
[Unit]
Description=weewx-clearskies-realtime
After=network.target

[Service]
Type=simple
User=weewx
Group=weewx
EnvironmentFile=/etc/weewx-clearskies/realtime-secrets.env
ExecStart=/usr/local/bin/weewx-clearskies-realtime
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable weewx-clearskies-realtime
sudo systemctl start weewx-clearskies-realtime
sudo systemctl status weewx-clearskies-realtime
```

Adjust `User=` and `Group=` to match your weewx account. If using a virtual environment, change `ExecStart` to:

```
ExecStart=/opt/weewx-clearskies/venv/bin/weewx-clearskies-realtime
```

### 7. Verify

```bash
# Health endpoint (loopback only)
curl http://127.0.0.1:8082/health/live
# Expected: {"status": "ok"}

curl http://127.0.0.1:8082/health/ready
# Expected: {"status": "ok"} when connected to broker and subscribed

# SSE stream (direct, bypassing proxy)
curl -N http://127.0.0.1:8766/sse
# Expected: lines like: data: {"dateTime": 1716163200, "outTemp": 72.3, ...}
# Press Ctrl+C to stop
```

---

## Docker Compose (full stack)

The stack repo ships a pre-configured `docker-compose.yaml` that runs clearskies-realtime alongside clearskies-api, the dashboard, and Caddy:

```
https://github.com/inguy24/weewx-clearskies-stack
```

---

## Updating

**Native (pip):**

```bash
pip install -U weewx-clearskies-realtime
sudo systemctl restart weewx-clearskies-realtime
```

Configuration at `/etc/weewx-clearskies/realtime.conf` is outside the Python package and is preserved automatically.

**Docker:**

```bash
docker compose pull
docker compose up -d
```

Read [CHANGELOG.md](CHANGELOG.md) before upgrading. It documents any manual steps required, config-file migrations, and breaking changes.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Service exits with `No config file found` | `realtime.conf` not at `/etc/weewx-clearskies/realtime.conf` | Create the config file |
| `/health/ready` returns `{"status": "degraded"}` | Not connected to MQTT broker | Check broker host/port; confirm broker is running |
| SSE stream connects but emits no events | Broker connection OK but weewx-mqtt is not publishing | Check weewx-mqtt extension config; verify the topic matches |
| Browser receives no SSE events in production | Reverse proxy is buffering the response | Add `proxy_buffering off` (Nginx) or confirm Caddy config |
| `MQTT mode requires paho-mqtt` at startup | Package installed without the `mqtt` extra | Reinstall with `pip install "weewx-clearskies-realtime[mqtt]"` |

Check service logs:

```bash
journalctl -u weewx-clearskies-realtime -f
```

# Security — weewx-clearskies-realtime

This repository is part of [Clear Skies](https://github.com/inguy24/weewx-clearskies-stack), distributed AS-IS under [GPL v3](LICENSE). There is no support window, no LTS, and no security backport policy — only the current release is available.

---

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:

**Security tab → Advisories → "Report a vulnerability"**

Or open a GitHub issue prefixed with `[security]` if private reporting is unavailable.

---

## Trust model

clearskies-realtime is a **read-only event bridge** for a weather station. It receives weewx loop packets from an MQTT broker and forwards them to browser clients over SSE. It has no write operations and no user accounts.

**Threat boundaries:**

1. **Internet → reverse proxy** — TLS termination. The operator controls this layer.
2. **Reverse proxy → clearskies-realtime** — the `/sse` endpoint. The SSE stream carries only public weather data; no credentials are transmitted.
3. **clearskies-realtime → MQTT broker** — optional TLS. The MQTT password is loaded from an environment variable, never stored in the config file.

---

## Authentication

### No end-user authentication

clearskies-realtime provides no user login or session management. The SSE stream carries publicly available weather station data. Operators who need access control add it at the reverse proxy layer (Apache `mod_auth_basic`, Authelia, Cloudflare Access, etc.).

### MQTT broker credentials

If the MQTT broker requires authentication, the password comes from the environment variable named by `[input.mqtt] password_env` (default: `WEEWX_CLEARSKIES_MQTT_PASSWORD`). Store it in a mode-0600 file loaded by the systemd `EnvironmentFile` directive:

```bash
# /etc/weewx-clearskies/realtime-secrets.env (mode 0600)
WEEWX_CLEARSKIES_MQTT_PASSWORD=<password>
```

The MQTT password must **not** appear in `realtime.conf`. The config loader's secret-leak guard rejects any INI key whose name ends in `_KEY`, `_SECRET`, `_TOKEN`, or `_PASSWORD`.

### MQTT TLS

For brokers on untrusted networks, enable TLS in the config:

```ini
[input]
mode = mqtt

    [[mqtt]]
    broker_port = 8883
    tls = true
    ca_file = /etc/ssl/certs/ca-certificates.crt
```

`ca_file` is optional; absent means the system CA bundle is used.

---

## SSE stream security

The `/sse` endpoint streams JSON objects containing weewx loop-packet fields. The data is the same as what the dashboard displays publicly. No credentials, secrets, or private fields are present in the stream.

**CORS:** the `[sse] allowed_origins` setting controls which browser origins can access the SSE stream. Default is `*` (open). In production, restrict to your dashboard origin:

```ini
[sse]
allowed_origins = https://weather.example.com
```

**Binding:** by default the SSE endpoint binds to `0.0.0.0:8766` (all IPv4 interfaces). Do NOT use `::` — uvicorn sets `IPV6_V6ONLY=1` on IPv6 sockets, making `::` IPv6-only regardless of the kernel `net.ipv6.bindv6only` setting. For single-host deployments behind a reverse proxy, binding to `127.0.0.1` reduces the exposed surface:

```ini
[sse]
bind_host = 127.0.0.1
```

---

## Dependency auditing

The CI pipeline runs `pip-audit` on every pull request and on a nightly cron schedule. The `paho-mqtt` dependency is included under the `[mqtt]` install extra. paho-mqtt is dual-licensed EPL-2.0/EDL-1.0; Clear Skies elects the EDL-1.0 (equivalent to BSD-3-Clause, GPL-compatible) operative license.

---

## Process hardening (systemd)

The example systemd unit in [INSTALL.md](INSTALL.md) includes:

```ini
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true
```

---

## Known limitations and accepted risks

| Item | Status |
|---|---|
| No shared-secret mechanism between proxy and realtime service | The SSE stream carries only public weather data; the absence of a proxy secret is acceptable for typical home deployments |
| CORS default is `*` | Operators must explicitly restrict `allowed_origins` in production |
| MQTT traffic is unencrypted by default | Acceptable for loopback or LAN brokers; operators on untrusted networks enable TLS |

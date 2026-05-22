# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

COPY pyproject.toml .
COPY README.md .
COPY weewx_clearskies_realtime/ weewx_clearskies_realtime/

# mqtt extra is the only supported input mode in v0.1; install it unconditionally
RUN pip install --no-cache-dir ".[mqtt]"


FROM python:3.12-slim-bookworm AS runtime

# Non-root system user — matches UID used by the compose stack
RUN useradd --system --uid 1000 --no-create-home --shell /sbin/nologin clearskies

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/weewx-clearskies-realtime /usr/local/bin/weewx-clearskies-realtime

# SSE endpoint — health port (8082) binds loopback only, not exposed externally
EXPOSE 8766

# Expected volume mounts (operator-supplied, never baked in):
#   /etc/weewx-clearskies/realtime.conf  — required runtime configuration
#   /etc/weewx-clearskies/secrets.env    — optional; source before entrypoint if used

# Faster than the API container: no DB or ephemeris warm-up
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8082/health/ready')"

USER clearskies

ENTRYPOINT ["python", "-m", "weewx_clearskies_realtime"]

"""Settings dataclasses and config-file loader.

Config file search order:
  1. CLEARSKIES_CONFIG env var
  2. /etc/weewx-clearskies/realtime.conf
  3. ~/.config/weewx-clearskies/realtime.conf

No config found → FileNotFoundError (fail-closed).

Secret-leak guard: any key matching _(KEY|SECRET|TOKEN|PASSWORD)$ (case-insensitive)
in the INI tree raises RuntimeError.  Secrets come only from env vars.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Regex that catches common secret-like key names in config files.
_SECRET_KEY_RE = re.compile(r"_(key|secret|token|password)$", re.IGNORECASE)

_SEARCH_PATHS: list[str] = [
    "/etc/weewx-clearskies/realtime.conf",
    str(Path("~/.config/weewx-clearskies/realtime.conf").expanduser()),
]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class InputSettings:
    mode: str = "mqtt"


@dataclass
class MQTTSettings:
    broker_host: str = "localhost"
    broker_port: int = 1883
    topic: str = "weewx/loop"
    client_id: str = "weewx-clearskies-realtime"
    username: str = ""
    # Name of the env var that holds the MQTT password — never the password itself.
    password_env: str = "WEEWX_CLEARSKIES_MQTT_PASSWORD"  # noqa: S105
    tls: bool = False
    # Path to a PEM CA bundle for broker TLS verification.  Empty string → use
    # the system CA bundle (paho default).  ADR-005 §Config.
    ca_file: str = ""
    qos: int = 0
    keepalive: int = 60

    @property
    def password(self) -> str | None:
        """Resolve the password from the configured env var at runtime."""
        value = os.environ.get(self.password_env)
        if value is None and self.username:
            logger.warning(
                "MQTT username is set but env var %s is not set; connecting without password",
                self.password_env,
            )
        return value


@dataclass
class SSESettings:
    bind_host: str = "0.0.0.0"
    bind_port: int = 8765
    # Comma-separated origins for CORS.  Default "*" (open) — operators should
    # restrict in production via config or a reverse proxy.
    allowed_origins: list[str] = field(default_factory=lambda: ["*"])


@dataclass
class HealthSettings:
    bind_host: str = "127.0.0.1"
    bind_port: int = 8082


@dataclass
class LoggingSettings:
    level: str = "INFO"


@dataclass
class Settings:
    input: InputSettings = field(default_factory=InputSettings)
    mqtt: MQTTSettings = field(default_factory=MQTTSettings)
    sse: SSESettings = field(default_factory=SSESettings)
    health: HealthSettings = field(default_factory=HealthSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)


# ---------------------------------------------------------------------------
# Secret-leak guard
# ---------------------------------------------------------------------------


def _check_no_secrets(node: Any, path: str = "") -> None:  # noqa: ANN401
    """Walk a ConfigObj tree and raise if any key looks like a secret.

    Operators must supply secrets via env vars, not the config file.
    """
    if hasattr(node, "items"):
        for key, value in node.items():
            full_key = f"{path}.{key}" if path else key
            if _SECRET_KEY_RE.search(key):
                raise RuntimeError(
                    f"Config key '{full_key}' looks like a secret "
                    "(keys matching _(KEY|SECRET|TOKEN|PASSWORD)$ are forbidden). "
                    "Secrets must come from env vars, not the config file."
                )
            _check_no_secrets(value, full_key)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_settings() -> Settings:
    """Load settings from the first config file found in the search order.

    Raises:
        FileNotFoundError: if no config file is found anywhere in the search path.
        RuntimeError: if the config file contains secret-like keys.
    """
    # Allow operator to point directly at a config file.
    env_path = os.environ.get("CLEARSKIES_CONFIG")
    candidates = [env_path] if env_path else _SEARCH_PATHS

    config_path: str | None = None
    for candidate in candidates:
        if Path(candidate).exists():
            config_path = candidate
            break

    if config_path is None:
        searched = ", ".join(candidates)
        raise FileNotFoundError(
            f"No config file found. Searched: {searched}. "
            "Set CLEARSKIES_CONFIG or create one of the above files."
        )

    # Import configobj lazily so import-time doesn't drag it in for tests
    # that don't exercise the full load path.
    try:
        from configobj import ConfigObj  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "configobj is required. Install with: pip install weewx-clearskies-realtime"
        ) from exc

    raw = ConfigObj(config_path, interpolation=False)
    _check_no_secrets(raw)

    logger.info("Loaded config from %s", config_path)
    return _parse(raw)


def _parse(raw: Any) -> Settings:  # noqa: ANN401
    """Map a raw ConfigObj dict to typed Settings dataclasses."""
    s = Settings()

    inp = raw.get("input", {})
    s.input = InputSettings(
        mode=str(inp.get("mode", "mqtt")).strip(),
    )

    mqtt_raw = inp.get("mqtt", {})
    s.mqtt = MQTTSettings(
        broker_host=str(mqtt_raw.get("broker_host", "localhost")).strip(),
        broker_port=int(mqtt_raw.get("broker_port", 1883)),
        topic=str(mqtt_raw.get("topic", "weewx/loop")).strip(),
        client_id=str(mqtt_raw.get("client_id", "weewx-clearskies-realtime")).strip(),
        username=str(mqtt_raw.get("username", "")).strip(),
        password_env=str(
            mqtt_raw.get("password_env", "WEEWX_CLEARSKIES_MQTT_PASSWORD")
        ).strip(),
        tls=str(mqtt_raw.get("tls", "false")).strip().lower() in ("true", "1", "yes"),
        ca_file=str(mqtt_raw.get("ca_file", "")).strip(),
        qos=int(mqtt_raw.get("qos", 0)),
        keepalive=int(mqtt_raw.get("keepalive", 60)),
    )

    sse_raw = raw.get("sse", {})
    # allowed_origins is a comma-separated string in the INI file.
    _origins_raw = str(sse_raw.get("allowed_origins", "*")).strip()
    _allowed_origins = [o.strip() for o in _origins_raw.split(",") if o.strip()]
    if not _allowed_origins:
        _allowed_origins = ["*"]
    s.sse = SSESettings(
        bind_host=str(sse_raw.get("bind_host", "0.0.0.0")).strip(),
        bind_port=int(sse_raw.get("bind_port", 8765)),
        allowed_origins=_allowed_origins,
    )

    health_raw = raw.get("health", {})
    s.health = HealthSettings(
        bind_host=str(health_raw.get("bind_host", "127.0.0.1")).strip(),
        bind_port=int(health_raw.get("bind_port", 8082)),
    )

    log_raw = raw.get("logging", {})
    level_raw = str(log_raw.get("level", "INFO")).strip().upper()
    # env var overrides config file
    level = os.environ.get("CLEARSKIES_LOG_LEVEL", level_raw).upper()
    s.logging = LoggingSettings(level=level)

    return s

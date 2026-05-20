"""Tests for config.settings — load_settings(), secret-leak guard, _parse()."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from weewx_clearskies_realtime.config.settings import (
    Settings,
    _check_no_secrets,
    _parse,
    load_settings,
)

# ---------------------------------------------------------------------------
# Secret-leak guard
# ---------------------------------------------------------------------------


def test_check_no_secrets_clean_dict() -> None:
    """A config dict with no secret-like keys passes without error."""
    _check_no_secrets({"broker_host": "localhost", "broker_port": "1883"})


def test_check_no_secrets_raises_on_password_key() -> None:
    with pytest.raises(RuntimeError, match="secret"):
        _check_no_secrets({"mqtt_password": "hunter2"})


def test_check_no_secrets_raises_on_token_key() -> None:
    with pytest.raises(RuntimeError, match="secret"):
        _check_no_secrets({"api_token": "abc123"})


def test_check_no_secrets_raises_on_key_suffix() -> None:
    with pytest.raises(RuntimeError, match="secret"):
        _check_no_secrets({"broker_KEY": "value"})


def test_check_no_secrets_nested() -> None:
    """Secret guard walks nested dicts."""
    with pytest.raises(RuntimeError):
        _check_no_secrets({"input": {"mqtt": {"my_secret": "oops"}}})


# ---------------------------------------------------------------------------
# _parse() — defaults when config dict is empty
# ---------------------------------------------------------------------------


def test_parse_defaults() -> None:
    s = _parse({})
    assert s.input.mode == "mqtt"
    assert s.mqtt.broker_host == "localhost"
    assert s.mqtt.broker_port == 1883
    assert s.mqtt.topic == "weewx/loop"
    assert s.mqtt.tls is False
    assert s.mqtt.qos == 0
    assert s.mqtt.keepalive == 60
    assert s.sse.bind_host == "0.0.0.0"
    assert s.sse.bind_port == 8766
    assert s.health.bind_host == "127.0.0.1"
    assert s.health.bind_port == 8082
    assert s.logging.level == "INFO"


def test_parse_overrides() -> None:
    raw = {
        "input": {
            "mode": "mqtt",
            "mqtt": {
                "broker_host": "mqtt.example.com",
                "broker_port": "8883",
                "topic": "custom/topic",
                "tls": "true",
                "qos": "1",
            },
        },
        "sse": {"bind_host": "0.0.0.0", "bind_port": "9000"},
        "health": {"bind_host": "127.0.0.1", "bind_port": "9001"},
        "logging": {"level": "DEBUG"},
    }
    s = _parse(raw)
    assert s.mqtt.broker_host == "mqtt.example.com"
    assert s.mqtt.broker_port == 8883
    assert s.mqtt.tls is True
    assert s.mqtt.qos == 1
    assert s.sse.bind_port == 9000
    assert s.health.bind_port == 9001
    assert s.logging.level == "DEBUG"


def test_parse_tls_false_variants() -> None:
    for falsy in ("false", "0", "no", "False"):
        raw = {"input": {"mqtt": {"tls": falsy}}}
        s = _parse(raw)
        assert s.mqtt.tls is False


def test_parse_env_var_overrides_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLEARSKIES_LOG_LEVEL", "WARNING")
    s = _parse({"logging": {"level": "DEBUG"}})
    assert s.logging.level == "WARNING"


# ---------------------------------------------------------------------------
# load_settings() — file search and fail-closed
# ---------------------------------------------------------------------------


def test_load_settings_no_config_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a config file, load_settings raises FileNotFoundError (fail-closed)."""
    monkeypatch.delenv("CLEARSKIES_CONFIG", raising=False)
    # Point search paths at non-existent files by monkey-patching the module list.
    import weewx_clearskies_realtime.config.settings as settings_mod
    orig = settings_mod._SEARCH_PATHS
    settings_mod._SEARCH_PATHS = ["/nonexistent/path/realtime.conf"]
    try:
        with pytest.raises(FileNotFoundError, match="No config file found"):
            load_settings()
    finally:
        settings_mod._SEARCH_PATHS = orig


def test_load_settings_from_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLEARSKIES_CONFIG env var is honoured."""
    conf = tmp_path / "realtime.conf"
    conf.write_text(
        textwrap.dedent("""
            [input]
            mode = mqtt

            [sse]
            bind_port = 8766

            [health]
            bind_port = 8082

            [logging]
            level = INFO
        """)
    )
    monkeypatch.setenv("CLEARSKIES_CONFIG", str(conf))
    s = load_settings()
    assert isinstance(s, Settings)
    assert s.input.mode == "mqtt"


def test_load_settings_rejects_secret_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Config containing a secret-like key raises RuntimeError."""
    conf = tmp_path / "realtime.conf"
    conf.write_text("[input]\nmqtt_password = oops\n")
    monkeypatch.setenv("CLEARSKIES_CONFIG", str(conf))
    with pytest.raises(RuntimeError, match="secret"):
        load_settings()


# ---------------------------------------------------------------------------
# MQTTSettings.password property
# ---------------------------------------------------------------------------


def test_mqtt_password_env_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from weewx_clearskies_realtime.config.settings import MQTTSettings

    monkeypatch.setenv("WEEWX_CLEARSKIES_MQTT_PASSWORD", "s3cret")
    s = MQTTSettings(username="user", password_env="WEEWX_CLEARSKIES_MQTT_PASSWORD")  # noqa: S106
    assert s.password == "s3cret"  # noqa: S105


def test_mqtt_password_missing_env_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from weewx_clearskies_realtime.config.settings import MQTTSettings

    monkeypatch.delenv("WEEWX_CLEARSKIES_MQTT_PASSWORD", raising=False)
    s = MQTTSettings(username="user", password_env="WEEWX_CLEARSKIES_MQTT_PASSWORD")  # noqa: S106
    with caplog.at_level("WARNING"):
        pwd = s.password
    assert pwd is None
    assert "not set" in caplog.text


def test_mqtt_password_none_when_no_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """No warning when username is empty — anonymous connection is expected."""
    from weewx_clearskies_realtime.config.settings import MQTTSettings

    monkeypatch.delenv("WEEWX_CLEARSKIES_MQTT_PASSWORD", raising=False)
    s = MQTTSettings(username="", password_env="WEEWX_CLEARSKIES_MQTT_PASSWORD")  # noqa: S106
    assert s.password is None  # No warning, no crash

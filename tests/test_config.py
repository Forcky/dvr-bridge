import os
import stat

import pytest

from dvrbridge.config import OPTIONS, ConfigError, Settings, load

BASE = {"DVRBRIDGE_DVR_HOST": "192.0.2.10", "DVRBRIDGE_DVR_PASSWORD": "pw-unique-123"}


def env(tmp_path, **extra):
    return {"DVRBRIDGE_DATA_DIR": str(tmp_path), **BASE, **extra}


def test_env_only_with_defaults(tmp_path):
    s = load(env(tmp_path))
    assert (s.dvr_host, s.dvr_user, s.dvr_port, s.dvr_media_port, s.rtsp_port) == ("192.0.2.10", "admin", 85, 9000, 554)
    assert s.channels == 0 and s.tz == "UTC" and s.max_playbacks == 2 and s.metrics_enabled
    assert s.sources["dvr_host"] == "env" and s.sources["dvr_port"] == "default"
    assert "pw-unique-123" not in repr(s) and s.secret_key not in repr(s)


def test_secret_key_is_generated_once_and_kept_private(tmp_path):
    a = load(env(tmp_path))
    b = load(env(tmp_path))
    assert a.secret_key == b.secret_key and len(a.secret_key) >= 32
    assert a.sources["secret_key"] == "generated"
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "secret.key").stat().st_mode) == 0o600


def test_explicit_secret_key_wins(tmp_path):
    s = load(env(tmp_path, DVRBRIDGE_SERVER_SECRET_KEY="s" * 40))
    assert s.secret_key == "s" * 40 and not (tmp_path / "secret.key").exists()


def test_file_then_env_precedence(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "dvr:\n  host: 192.0.2.99\n  password: from-file\n  channels: 16\n"
        "playback:\n  max_concurrent: 3\nmonitoring:\n  metrics_enabled: false\n",
        encoding="utf-8",
    )
    s = load({"DVRBRIDGE_DATA_DIR": str(tmp_path), "DVRBRIDGE_DVR_HOST": "192.0.2.10"})
    assert s.dvr_host == "192.0.2.10" and s.sources["dvr_host"] == "env"
    assert s.dvr_password == "from-file" and s.sources["dvr_password"] == "file"
    assert s.channels == 16 and s.max_playbacks == 3 and s.metrics_enabled is False


def test_file_secret_from_docker_secret(tmp_path):
    secret = tmp_path / "pw.txt"
    secret.write_text("from-secret-file\n", encoding="utf-8")
    s = load({"DVRBRIDGE_DATA_DIR": str(tmp_path), "DVRBRIDGE_DVR_HOST": "192.0.2.10",
              "DVRBRIDGE_DVR_PASSWORD_FILE": str(secret)})
    assert s.dvr_password == "from-secret-file"
    with pytest.raises(ConfigError, match="DVRBRIDGE_DVR_PASSWORD_FILE"):
        load({"DVRBRIDGE_DATA_DIR": str(tmp_path), "DVRBRIDGE_DVR_HOST": "x",
              "DVRBRIDGE_DVR_PASSWORD_FILE": str(tmp_path / "missing")})


def test_tz_env_is_the_fallback_for_the_dvr_timezone(tmp_path):
    assert load(env(tmp_path, TZ="Europe/London")).tz == "Europe/London"
    assert load(env(tmp_path, TZ="Europe/London", DVRBRIDGE_DVR_TIMEZONE="Australia/Sydney")).tz == "Australia/Sydney"


@pytest.mark.parametrize("extra, message", [
    ({"DVRBRIDGE_DVR_HOST": ""}, "dvr.host is required"),
    ({"DVRBRIDGE_DVR_HTTP_PORT": "70000"}, "dvr.http_port must be a port"),
    ({"DVRBRIDGE_DVR_HTTP_PORT": "eighty"}, "dvr.http_port must be a whole number"),
    ({"DVRBRIDGE_DVR_CHANNELS": "65"}, "dvr.channels"),
    ({"DVRBRIDGE_DVR_TIMEZONE": "Mars/Base"}, "not a known time zone"),
    ({"DVRBRIDGE_PLAYBACK_MAX_CONCURRENT": "0"}, "max_concurrent"),
    ({"DVRBRIDGE_MONITORING_POLL_INTERVAL": "1"}, "poll_interval"),
    ({"DVRBRIDGE_MONITORING_METRICS_ENABLED": "maybe"}, "true or false"),
    ({"DVRBRIDGE_SERVER_PUBLIC_URL": "cameras.example.com"}, "public_url"),
    ({"DVRBRIDGE_SERVER_SECRET_KEY": "short"}, "secret_key"),
])
def test_validation_messages(tmp_path, extra, message):
    e = env(tmp_path, **extra)
    if extra.get("DVRBRIDGE_DVR_HOST") == "":
        del e["DVRBRIDGE_DVR_HOST"]
    with pytest.raises(ConfigError, match=message):
        load(e)


def test_password_is_required(tmp_path):
    with pytest.raises(ConfigError, match="dvr.password is required"):
        load({"DVRBRIDGE_DATA_DIR": str(tmp_path), "DVRBRIDGE_DVR_HOST": "192.0.2.10"})


@pytest.mark.parametrize("text, message", [
    ("dvr: [1, 2]\n", "must be a mapping"),
    ("cameras:\n  x: 1\n", "unknown section"),
    ("dvr:\n  hostname: x\n", "unknown setting dvr.hostname"),
    ("playback:\n  speed: 2\n", "unknown setting playback.speed"),
    ("dvr: {host: [\n", "not valid YAML"),
])
def test_bad_config_files(tmp_path, text, message):
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load(env(tmp_path))


def test_auth_defaults_and_file(tmp_path):
    s = load(env(tmp_path))
    assert (s.auth_mode, s.session_hours, s.local_login, s.totp) == ("local", 12, True, "optional")
    (tmp_path / "config.yaml").write_text("auth:\n  mode: header\n  trusted_proxies: 172.28.3.0/24, 10.0.0.1\n",
                                          encoding="utf-8")
    s = load(env(tmp_path))
    assert s.auth_mode == "header" and s.sources["trusted_proxies"] == "file"


@pytest.mark.parametrize("extra, message", [
    ({"DVRBRIDGE_AUTH_MODE": "magic"}, "auth.mode must be one of"),
    ({"DVRBRIDGE_AUTH_TOTP": "sometimes"}, "auth.totp"),
    ({"DVRBRIDGE_AUTH_MODE": "oidc"}, "auth.oidc_issuer is required"),
    ({"DVRBRIDGE_AUTH_MODE": "oidc", "DVRBRIDGE_AUTH_OIDC_ISSUER": "https://id.example.com",
      "DVRBRIDGE_AUTH_OIDC_CLIENT_ID": "c", "DVRBRIDGE_AUTH_OIDC_CLIENT_SECRET": "s"}, "public_url is required"),
    ({"DVRBRIDGE_AUTH_MODE": "oidc", "DVRBRIDGE_AUTH_OIDC_ISSUER": "http://id.example.com",
      "DVRBRIDGE_AUTH_OIDC_CLIENT_ID": "c", "DVRBRIDGE_AUTH_OIDC_CLIENT_SECRET": "s",
      "DVRBRIDGE_SERVER_PUBLIC_URL": "https://cams.example.com"}, "must be an https"),
    ({"DVRBRIDGE_AUTH_MODE": "header"}, "needs auth.header_secret or auth.trusted_proxies"),
    ({"DVRBRIDGE_AUTH_MODE": "header", "DVRBRIDGE_AUTH_HEADER_SECRET": "short"}, "at least 32"),
    ({"DVRBRIDGE_AUTH_MODE": "header", "DVRBRIDGE_AUTH_TRUSTED_PROXIES": "10.0.0.0/8, nonsense"}, "nonsense"),
])
def test_auth_validation(tmp_path, extra, message):
    with pytest.raises(ConfigError, match=message):
        load(env(tmp_path, **extra))


def test_the_documented_example_config_loads(tmp_path):
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    s = load({"DVRBRIDGE_DATA_DIR": str(tmp_path)}, path=example)
    assert s.dvr_host == "192.168.1.50" and s.tz == "Europe/London" and s.sources["dvr_host"] == "file"


def test_every_option_has_a_settings_field_and_help():
    names = set(Settings.__dataclass_fields__)
    for o in OPTIONS:
        assert o.attr in names and o.help, o

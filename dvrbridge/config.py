"""Configuration: built-in defaults, then a YAML file, then environment variables.

The file (``<data_dir>/config.yaml`` by default, or ``DVRBRIDGE_CONFIG``) is optional and
grouped by section::

    dvr:
      host: 192.168.1.50
      username: admin
      password: change-me
    playback:
      max_concurrent: 2

Every key can also be set as ``DVRBRIDGE_<SECTION>_<KEY>`` (``DVRBRIDGE_DVR_HOST``), which
wins over the file. Secrets can come from a file instead - ``DVRBRIDGE_DVR_PASSWORD_FILE``
- for Docker secrets. ``Settings.sources`` records where each value came from, so the
settings page can show which ones are fixed by the environment.
"""
from __future__ import annotations

import os
import secrets as _secrets
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

ENV_PREFIX = "DVRBRIDGE_"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Option:
    section: str
    key: str
    attr: str
    kind: type
    secret: bool = False
    help: str = ""

    @property
    def env(self) -> str:
        return f"{ENV_PREFIX}{self.section}_{self.key}".upper()


# The settings a user can change. `attr` is the Settings field it fills.
OPTIONS: tuple[Option, ...] = (
    Option("dvr", "host", "dvr_host", str, help="DVR address (IP or host name)"),
    Option("dvr", "username", "dvr_user", str, help="DVR account the bridge logs in with"),
    Option("dvr", "password", "dvr_password", str, secret=True, help="Password for that account"),
    Option("dvr", "http_port", "dvr_port", int, help="DVR web/API port"),
    Option("dvr", "media_port", "dvr_media_port", int, help="DVR client/media port (recorded playback)"),
    Option("dvr", "rtsp_port", "rtsp_port", int, help="DVR RTSP port (live view)"),
    Option("dvr", "channels", "channels", int, help="Number of channels; 0 = ask the DVR"),
    Option("dvr", "timezone", "tz", str, help="Time zone the DVR's clock is set to (IANA name)"),
    Option("playback", "max_concurrent", "max_playbacks", int, help="Recorded clips streamed at once"),
    Option("playback", "max_range_hours", "max_range_hours", int, help="Longest clip that can be played or exported"),
    Option("monitoring", "poll_interval", "poll_interval", float, help="Seconds between DVR status polls"),
    Option("monitoring", "metrics_enabled", "metrics_enabled", bool, help="Serve Prometheus metrics at /metrics"),
    Option("monitoring", "metrics_token", "metrics_token", str, secret=True,
           help="If set, /metrics requires 'Authorization: Bearer <token>'"),
    Option("server", "public_url", "public_url", str, help="External URL, e.g. https://cameras.example.com"),
    Option("server", "secret_key", "secret_key", str, secret=True,
           help="Signs sessions; generated and stored in the data directory if not set"),
    Option("auth", "mode", "auth_mode", str, help="local, oidc, header or none"),
    Option("auth", "session_hours", "session_hours", int, help="How long a login lasts"),
    Option("auth", "local_login", "local_login", bool,
           help="Allow username/password logins (in oidc/header mode: a break-glass admin login)"),
    Option("auth", "totp", "totp", str, help="Two-factor codes: optional or required"),
    Option("auth", "oidc_issuer", "oidc_issuer", str, help="OIDC issuer URL"),
    Option("auth", "oidc_client_id", "oidc_client_id", str, help="OIDC client ID"),
    Option("auth", "oidc_client_secret", "oidc_client_secret", str, secret=True, help="OIDC client secret"),
    Option("auth", "oidc_scopes", "oidc_scopes", str, help="Scopes to request"),
    Option("auth", "oidc_username_claim", "oidc_username_claim", str, help="Claim used as the username"),
    Option("auth", "oidc_groups_claim", "oidc_groups_claim", str, help="Claim that lists the user's groups"),
    Option("auth", "oidc_admin_group", "oidc_admin_group", str, help="Members of this group are admins"),
    Option("auth", "oidc_allowed_group", "oidc_allowed_group", str,
           help="If set, only members of this group (or the admin group) may log in"),
    Option("auth", "oidc_button_label", "oidc_button_label", str, help="Text on the login button"),
    Option("auth", "header_user", "header_user", str, help="Header carrying the username"),
    Option("auth", "header_groups", "header_groups", str, help="Header carrying the user's groups"),
    Option("auth", "header_admin_group", "header_admin_group", str, help="Members of this group are admins"),
    Option("auth", "header_secret", "header_secret", str, secret=True,
           help="Shared secret the proxy sends in the header named by header_secret_name"),
    Option("auth", "header_secret_name", "header_secret_name", str, help="Header carrying the shared secret"),
    Option("auth", "trusted_proxies", "trusted_proxies", str,
           help="Comma-separated addresses/networks allowed to send identity headers"),
)
AUTH_MODES = ("local", "oidc", "header", "none")
BY_ATTR = {o.attr: o for o in OPTIONS}


@dataclass(frozen=True)
class Settings:
    dvr_host: str
    dvr_user: str = field(repr=False)
    dvr_password: str = field(repr=False)
    secret_key: str = field(repr=False)
    dvr_port: int = 85
    dvr_media_port: int = 9000
    rtsp_port: int = 554
    channels: int = 0
    tz: str = "UTC"
    max_playbacks: int = 2
    max_range_hours: int = 3
    poll_interval: float = 60.0
    metrics_enabled: bool = True
    metrics_token: str = field(default="", repr=False)
    public_url: str = ""
    auth_mode: str = "local"
    session_hours: int = 12
    local_login: bool = True
    totp: str = "optional"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = field(default="", repr=False)
    oidc_scopes: str = "openid profile email groups"
    oidc_username_claim: str = "preferred_username"
    oidc_groups_claim: str = "groups"
    oidc_admin_group: str = ""
    oidc_allowed_group: str = ""
    oidc_button_label: str = "Sign in with SSO"
    header_user: str = "Remote-User"
    header_groups: str = "Remote-Groups"
    header_admin_group: str = ""
    header_secret: str = field(default="", repr=False)
    header_secret_name: str = "X-DVR-Bridge-Proxy-Secret"
    trusted_proxies: str = ""
    data_dir: str = "/data"
    go2rtc_port: int = 1984
    go2rtc_bin: str = "/usr/local/bin/go2rtc"
    go2rtc_config_path: str = "/tmp/go2rtc.yaml"
    # attr -> "default" | "file" | "env"; not part of equality.
    sources: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        problems = validate(self)
        if problems:
            raise ConfigError("; ".join(problems))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Load from the environment and the config file it points at (or the default one)."""
        return load(env)


def _coerce(opt: Option, value: Any) -> Any:
    if value is None:
        return None
    if opt.kind is bool:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"{opt.section}.{opt.key} must be true or false")
    try:
        return opt.kind(str(value).strip()) if opt.kind is not str else str(value).strip()
    except ValueError:
        what = "a whole number" if opt.kind is int else "a number"
        raise ConfigError(f"{opt.section}.{opt.key} must be {what}") from None


def validate(s: Settings) -> list[str]:
    p: list[str] = []
    if not s.dvr_host:
        p.append("dvr.host is required")
    if not s.dvr_user:
        p.append("dvr.username is required")
    if not s.dvr_password:
        p.append("dvr.password is required")
    if len(s.secret_key) < 16:
        p.append("server.secret_key must be at least 16 characters")
    for name in ("dvr_port", "dvr_media_port", "rtsp_port"):
        if not 1 <= getattr(s, name) <= 65535:
            p.append(f"dvr.{BY_ATTR[name].key} must be a port number (1-65535)")
    if not 0 <= s.channels <= 64:
        p.append("dvr.channels must be 0 (ask the DVR) to 64")
    if not 1 <= s.max_playbacks <= 8:
        p.append("playback.max_concurrent must be 1-8")
    if not 1 <= s.max_range_hours <= 24:
        p.append("playback.max_range_hours must be 1-24")
    if s.poll_interval < 10:
        p.append("monitoring.poll_interval must be at least 10 seconds")
    try:
        ZoneInfo(s.tz)
    except (ZoneInfoNotFoundError, ValueError):
        p.append(f"dvr.timezone {s.tz!r} is not a known time zone (use an IANA name like Europe/London)")
    if s.public_url:
        u = urlparse(s.public_url)
        if u.scheme not in ("http", "https") or not u.netloc:
            p.append("server.public_url must look like https://cameras.example.com")
    p += _validate_auth(s)
    return p


def _validate_auth(s: Settings) -> list[str]:
    p: list[str] = []
    if s.auth_mode not in AUTH_MODES:
        return [f"auth.mode must be one of {', '.join(AUTH_MODES)}"]
    if not 1 <= s.session_hours <= 24 * 90:
        p.append("auth.session_hours must be 1-2160")
    if s.totp not in ("optional", "required"):
        p.append("auth.totp must be optional or required")
    if s.auth_mode == "oidc":
        for name in ("oidc_issuer", "oidc_client_id", "oidc_client_secret"):
            if not getattr(s, name):
                p.append(f"auth.{name} is required when auth.mode is oidc")
        if not s.public_url:
            p.append("server.public_url is required when auth.mode is oidc (it builds the redirect URL)")
        if s.oidc_issuer:
            u = urlparse(s.oidc_issuer)
            local = u.hostname in ("localhost", "127.0.0.1")
            if u.scheme != "https" and not (u.scheme == "http" and local):
                p.append("auth.oidc_issuer must be an https:// URL")
    if s.auth_mode == "header":
        if not s.header_user:
            p.append("auth.header_user is required when auth.mode is header")
        if not s.header_secret and not s.trusted_proxies:
            p.append("header mode needs auth.header_secret or auth.trusted_proxies (or both): "
                     "otherwise anyone who can reach the app could claim any user")
        if s.header_secret and len(s.header_secret) < 32:
            p.append("auth.header_secret must be at least 32 characters")
    if s.trusted_proxies:
        import ipaddress

        for part in s.trusted_proxies.split(","):
            try:
                ipaddress.ip_network(part.strip(), strict=False)
            except ValueError:
                p.append(f"auth.trusted_proxies: {part.strip()!r} is not an address or network")
    return p


def _read_file(path: Path) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} must be a mapping of sections")
    known = {o.section for o in OPTIONS}
    unknown = sorted(set(doc) - known)
    if unknown:
        raise ConfigError(f"{path}: unknown section(s) {', '.join(unknown)}")
    for section, body in doc.items():
        if body is not None and not isinstance(body, dict):
            raise ConfigError(f"{path}: section {section!r} must be a mapping")
    return doc


def _env_value(opt: Option, e: Mapping[str, str]) -> str | None:
    file_var = opt.env + "_FILE"
    if e.get(file_var):
        try:
            return Path(e[file_var]).read_text(encoding="utf-8").strip()
        except OSError as err:
            raise ConfigError(f"{file_var}: cannot read {e[file_var]} ({err.strerror})") from None
    v = e.get(opt.env)
    return v if v not in (None, "") else None


def persisted_secret_key(data_dir: str) -> str:
    """The session-signing key from <data_dir>/secret.key, created on first start."""
    path = Path(data_dir) / "secret.key"
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    key = _secrets.token_urlsafe(48)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
    except OSError as err:
        raise ConfigError(
            f"no server.secret_key set and {path} cannot be written ({err.strerror}); "
            f"mount a writable data directory or set DVRBRIDGE_SERVER_SECRET_KEY"
        ) from None
    return key


def load(env: Mapping[str, str] | None = None, *, path: str | Path | None = None) -> Settings:
    e = os.environ if env is None else env
    data_dir = e.get(ENV_PREFIX + "DATA_DIR", "/data")
    cfg_path = Path(path or e.get(ENV_PREFIX + "CONFIG") or Path(data_dir) / "config.yaml")
    doc = _read_file(cfg_path)

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for opt in OPTIONS:
        raw = _env_value(opt, e)
        if raw is not None:
            values[opt.attr], sources[opt.attr] = _coerce(opt, raw), "env"
            continue
        section = doc.get(opt.section) or {}
        if opt.key in section and section[opt.key] is not None:
            values[opt.attr], sources[opt.attr] = _coerce(opt, section[opt.key]), "file"
    for section, body in doc.items():
        for key in (body or {}):
            if key not in {o.key for o in OPTIONS if o.section == section}:
                raise ConfigError(f"{cfg_path}: unknown setting {section}.{key}")

    if "tz" not in values and e.get("TZ"):
        values["tz"], sources["tz"] = e["TZ"], "env"
    if "dvr_user" not in values:
        values["dvr_user"] = "admin"
    if not values.get("secret_key"):
        values["secret_key"] = persisted_secret_key(data_dir)
        sources["secret_key"] = "generated"
    for f in fields(Settings):
        if f.name in BY_ATTR and f.name not in sources:
            sources[f.name] = "default"
    values.setdefault("dvr_host", "")
    values.setdefault("dvr_password", "")
    return Settings(**values, data_dir=data_dir, sources=sources)

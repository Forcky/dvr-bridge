"""Who is asking: resolves a request to a user, whatever the auth mode, and provisions external users."""
from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from .netutil import client_ip, header, is_trusted, parse_networks
from .oidc import Identity, OidcClient
from .ratelimit import LoginThrottle
from .store import Session, Store, User

log = logging.getLogger("dvrbridge.auth")

SESSION_COOKIE = "dvrbridge_session"
ANONYMOUS = User(id=0, username="anonymous", role="admin", source="local", totp_enabled=False, disabled=False,
                 created_at=0.0, last_login_at=None)


class AccessDenied(Exception):
    """A login that is valid but not allowed here; the message is safe to show."""


@dataclass(frozen=True)
class Who:
    user: User | None
    session_token: str | None = None
    mfa_pending: bool = False


class AuthService:
    def __init__(self, s: Settings, store: Store | None = None, oidc_transport=None) -> None:
        self.s = s
        self.store = store or Store(Path(s.data_dir) / "dvrbridge.db")
        self.trusted = parse_networks(s.trusted_proxies)
        self.throttle = LoginThrottle()
        self.oidc = OidcClient(s, oidc_transport) if s.auth_mode == "oidc" else None
        self.setup_code: str | None = None

    # ---- lifecycle ---------------------------------------------------------------------------
    @property
    def local_login(self) -> bool:
        return self.s.auth_mode == "local" or (self.s.local_login and self.s.auth_mode in ("oidc", "header"))

    @property
    def setup_required(self) -> bool:
        return self.local_login and self.store.count_users() == 0

    def startup(self) -> None:
        self.store.purge_expired()
        if self.s.auth_mode == "none":
            log.warning("auth.mode is none: EVERYONE who can reach this service is an admin. "
                        "Only use this behind an authenticating reverse proxy you control.")
        if self.setup_required:
            raw = secrets.token_hex(8).upper()
            self.setup_code = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
            where = (self.s.public_url.rstrip("/") or "http://<this host>:8099") + "/setup"
            log.warning("First start: no accounts yet. Create the admin account at %s with setup code %s",
                        where, self.setup_code)

    async def close(self) -> None:
        if self.oidc:
            await self.oidc.close()
        self.store.close()

    def check_setup_code(self, code: str) -> bool:
        return bool(self.setup_code) and hmac.compare_digest((code or "").strip().upper().encode(),
                                                             self.setup_code.encode())

    # ---- per request -------------------------------------------------------------------------
    def client_ip(self, scope) -> str:
        return client_ip(scope, self.trusted)

    def resolve(self, scope, cookies: dict[str, str]) -> Who:
        if self.s.auth_mode == "none":
            return Who(ANONYMOUS)
        if self.s.auth_mode == "header":
            user = self._from_headers(scope)
            if user:
                return Who(user)
        token = cookies.get(SESSION_COOKIE)
        # Sessions come from a password or OIDC login; in header mode only from the break-glass login.
        if token and (self.s.auth_mode in ("local", "oidc") or self.local_login):
            sess: Session | None = self.store.session(token)
            if sess:
                return Who(sess.user, token, sess.mfa_pending)
        return Who(None)

    def _from_headers(self, scope) -> User | None:
        peer = (scope.get("client") or ("", 0))[0]
        if self.trusted and not is_trusted(peer, self.trusted):
            return None
        if self.s.header_secret:
            sent = header(scope, self.s.header_secret_name) or ""
            if not hmac.compare_digest(sent.encode(), self.s.header_secret.encode()):
                return None
        username = (header(scope, self.s.header_user) or "").strip()
        if not username:
            return None
        groups = tuple(g.strip() for g in (header(scope, self.s.header_groups) or "").replace("|", ",").split(",")
                       if g.strip())
        try:
            return self.provision(Identity(username, groups, username), source="header",
                                  admin_group=self.s.header_admin_group, allowed_group="")
        except AccessDenied as e:
            log.warning("header login refused for %r: %s", username, e)
            return None

    # ---- external users ----------------------------------------------------------------------
    def provision(self, ident: Identity, *, source: str, admin_group: str, allowed_group: str) -> User:
        """Find or create the local record for an externally authenticated user."""
        if allowed_group and allowed_group not in ident.groups and (not admin_group or admin_group not in ident.groups):
            raise AccessDenied("Your account is not in a group that may use DVR Bridge.")
        role = "admin" if admin_group and admin_group in ident.groups else "viewer"
        user = self.store.find_user(ident.username)
        if user is None:
            user = self.store.create_user(ident.username, role, source)
            self.store.audit("user_created", ident.username, f"first {source} login, role {role}")
            return user
        if user.source != source:
            # Never let an external identity take over a local account (or the other way round).
            raise AccessDenied(f"The username {ident.username!r} belongs to a different kind of account.")
        if user.disabled:
            raise AccessDenied("This account is disabled.")
        if admin_group and user.role != role:
            user = self.store.update_user(user.id, role=role)
            self.store.audit("role_changed", ident.username, f"{role} (from {source} groups)")
        return user

"""Who is asking: resolves a request to a user, whatever the auth mode, and provisions external users."""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from .netutil import client_ip, header, is_trusted, parse_networks
from .oidc import Identity, OidcClient
from .passwords import HashLimiter
from .ratelimit import LoginThrottle
from .store import Session, Store, User

log = logging.getLogger("dvrbridge.auth")

SESSION_COOKIE = "dvrbridge_session"
# Used instead when the cookie is Secure: browsers then refuse to let another (sub)domain or plain http set it.
SECURE_SESSION_COOKIE = "__Host-" + SESSION_COOKIE
USERNAME_MAX = 64
TOTP_PENDING_TTL = 600
# A proxy user has no login event (every request carries the headers), so "last login" is their
# last visit, written at most this often to keep the database out of the request path.
HEADER_SEEN_INTERVAL = 600


def session_cookie(cookies) -> str | None:
    return cookies.get(SECURE_SESSION_COOKIE) or cookies.get(SESSION_COOKIE)
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
        self.hashing = HashLimiter()
        # user id -> (session token, secret, expiry): a two-factor secret offered but not confirmed yet.
        self.pending_totp: dict[int, tuple[str | None, str, float]] = {}
        self._warned_forwarded = False

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
        for w in self.warnings():
            log.warning("%s", w)
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

    def warnings(self) -> list[str]:
        """Deployment problems worth telling the admin about (start-up log and the settings page)."""
        s, out = self.s, []
        if s.auth_mode == "none":
            return out
        if s.public_url and not self.trusted:
            out.append("server.public_url is set, so DVR Bridge is behind a reverse proxy, but auth.trusted_proxies "
                       "is empty: every visitor appears to come from the proxy's address, which weakens login "
                       "throttling and makes the activity log useless. Set auth.trusted_proxies to the proxy's address.")
        if s.public_url.startswith("http://") and not s.public_url.startswith(("http://localhost", "http://127.0.0.1")):
            out.append("server.public_url is http: logins and session cookies travel unencrypted. Put HTTPS in front "
                       "and set server.public_url to the https address.")
        return out

    # ---- per request -------------------------------------------------------------------------
    def client_ip(self, scope) -> str:
        return client_ip(scope, self.trusted)

    def _peer_trusted(self, scope) -> bool:
        return bool(self.trusted) and is_trusted((scope.get("client") or ("", 0))[0], self.trusted)

    def shared_address(self, scope) -> bool:
        """True when the request came through a proxy we were not told to trust: its address stands for
        every visitor, so the per-address login limit is relaxed (per-account limits still apply)."""
        if self._peer_trusted(scope) or header(scope, "x-forwarded-for") is None:
            return False
        if not self._warned_forwarded:
            self._warned_forwarded = True
            log.warning("Requests arrive through a proxy that is not in auth.trusted_proxies; set it so that login "
                        "throttling and the activity log see real client addresses.")
        return True

    def forwarded_https(self, scope) -> bool:
        """A trusted proxy says the browser used HTTPS."""
        return self._peer_trusted(scope) and (header(scope, "x-forwarded-proto") or "").split(",")[0].strip().lower() == "https"

    def resolve(self, scope, cookies: dict[str, str]) -> Who:
        if self.s.auth_mode == "none":
            return Who(ANONYMOUS)
        if self.s.auth_mode == "header":
            user = self._from_headers(scope)
            if user:
                return Who(user)
        token = session_cookie(cookies)
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
        if len(username) > USERNAME_MAX or any(ord(c) < 32 or ord(c) == 127 for c in username):
            log.warning("header login refused: unusable username (%d characters)", len(username))
            return None
        groups = tuple(g.strip() for g in (header(scope, self.s.header_groups) or "").replace("|", ",").split(",")
                       if g.strip())
        try:
            user = self.provision(Identity(username, groups, username), source="header",
                                  admin_group=self.s.header_admin_group, allowed_group="")
        except AccessDenied as e:
            log.warning("header login refused for %r: %s", username, e)
            return None
        if user.last_login_at is None or time.time() - user.last_login_at > HEADER_SEEN_INTERVAL:
            self.store.touch_login(user.id)
        return user

    def role_group(self, source: str) -> str:
        """The identity-provider group that decides this kind of account's role ('' = set by an admin here)."""
        return {"header": self.s.header_admin_group, "oidc": self.s.oidc_admin_group}.get(source, "")

    # ---- external users ----------------------------------------------------------------------
    def provision(self, ident: Identity, *, source: str, admin_group: str, allowed_group: str) -> User:
        """Find or create the local record for an externally authenticated user.

        An OIDC user is identified by the provider's subject (``ident.subject``), never by the username
        alone: usernames can be changed or reused at the provider. A proxy (header) user has no such
        identifier, so there the username the proxy sends is the identity."""
        if allowed_group and allowed_group not in ident.groups and (not admin_group or admin_group not in ident.groups):
            raise AccessDenied("Your account is not in a group that may use DVR Bridge.")
        role = "admin" if admin_group and admin_group in ident.groups else "viewer"
        subject = ident.subject if source == "oidc" else None
        user = self.store.find_subject(subject) if subject else None
        if user is None:
            user = self.store.find_user(ident.username)
            if user is None:
                user = self.store.create_user(ident.username, role, source, subject=subject)
                self.store.audit("user_created", ident.username, f"first {source} login, role {role}")
                return user
            if user.source != source:
                # Never let an external identity take over a local account (or the other way round).
                raise AccessDenied(f"The username {ident.username!r} belongs to a different kind of account.")
            if subject:
                known = self.store.subject(user.id)
                if known is None:
                    self.store.set_subject(user.id, subject)   # account from before subjects were recorded
                elif known != subject:
                    raise AccessDenied(f"The username {ident.username!r} belongs to another sign-in at the "
                                       f"identity provider.")
        if user.disabled:
            raise AccessDenied("This account is disabled.")
        if admin_group and user.role != role:
            user = self.store.update_user(user.id, role=role)
            self.store.audit("role_changed", user.username, f"{role} (from {source} groups)")
        return user

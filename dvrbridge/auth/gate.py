"""The one place every request passes: CSRF check, who is asking, and what they may reach.

Plain ASGI so it covers the live-view websocket as well as HTTP. Routes find the user in
``request.state.user`` (and the client address in ``request.state.client_ip``).
"""
from __future__ import annotations

import json
import logging
import re
from http.cookies import SimpleCookie
from urllib.parse import quote

from starlette.datastructures import Headers

from ..channels import csrf_problem
from .service import AuthService

log = logging.getLogger("dvrbridge.auth")

PUBLIC_PATHS = frozenset({
    "/healthz", "/metrics", "/login", "/setup",
    "/api/auth/config", "/api/auth/login", "/api/auth/totp", "/api/auth/setup", "/api/auth/logout",
    "/auth/oidc/start", "/auth/oidc/callback",
})
PUBLIC_PREFIXES = ("/static/",)
ADMIN_PATHS = frozenset({"/settings", "/settings/system", "/users"})
ADMIN_PREFIXES = ("/api/settings/", "/api/users", "/api/audit")
# Reachable while a required second factor is not set up yet.
ENROL_PATHS = frozenset({"/account", "/api/me", "/api/account/totp/begin", "/api/account/totp/confirm"})
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Every request body here is a small JSON document. Reading more would let anyone (the login
# endpoint is public) fill the container's memory.
MAX_BODY = 64 * 1024
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")


def security_headers(host: str, api: bool) -> list[tuple[bytes, bytes]]:
    """Browser hardening sent with every response. Scripts, styles and media only from this site; never
    framed; live view's websocket to this host only (named, for browsers whose 'self' excludes ws:)."""
    ws = f" ws://{host} wss://{host}" if _HOST_RE.match(host or "") else ""
    csp = ("default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
           f"connect-src 'self'{ws}; style-src 'self' 'unsafe-inline'; object-src 'none'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    out = [(b"content-security-policy", csp.encode("latin-1")), (b"x-content-type-options", b"nosniff"),
           (b"x-frame-options", b"DENY"), (b"referrer-policy", b"same-origin")]
    if api:
        out.append((b"cache-control", b"no-store"))
    return out


def _cookies(headers: Headers) -> dict[str, str]:
    jar = SimpleCookie()
    try:
        jar.load(headers.get("cookie", ""))
    except Exception:  # malformed cookie header: treat as none
        return {}
    return {k: m.value for k, m in jar.items()}


class AuthGate:
    def __init__(self, app, *, service: AuthService) -> None:
        self.app = app
        self.svc = service

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope["path"]
        headers = Headers(scope=scope)
        method = scope.get("method", "GET")
        if scope["type"] == "http":
            send = self._with_headers(send, headers.get("host", ""), path.startswith("/api/"))
        if scope["type"] == "http" and method in UNSAFE:
            problem = csrf_problem(headers)
            if problem:
                return await self._json(send, 403, {"error": "forbidden", "detail": problem})
            receive = await self._bounded_body(headers, receive)
            if receive is None:
                return await self._json(send, 413, {"error": "too_large",
                                                    "detail": f"Request bodies are limited to {MAX_BODY // 1024} KB."})

        who = self.svc.resolve(scope, _cookies(headers))
        state = scope.setdefault("state", {})
        state["user"] = who.user if not who.mfa_pending else None
        state["session_token"] = who.session_token
        state["client_ip"] = self.svc.client_ip(scope)
        state["shared_address"] = self.svc.shared_address(scope)
        user = state["user"]

        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)
        if user is None:
            return await self._unauthenticated(scope, receive, send, path)
        if (self.svc.s.totp == "required" and user.source == "local" and not user.totp_enabled
                and self.svc.s.auth_mode != "none" and path not in ENROL_PATHS):
            if self._wants_page(scope, path):
                return await self._redirect(send, "/account?enrol=1")
            return await self._deny(scope, receive, send, 403, "totp_required",
                                    "Set up two-factor authentication on the Account page first.")
        if (path in ADMIN_PATHS or path.startswith(ADMIN_PREFIXES)) and not user.is_admin:
            return await self._deny(scope, receive, send, 403, "admin_only", "This needs an admin account.")
        return await self.app(scope, receive, send)

    # ---- request bodies and response headers --------------------------------------------------
    @staticmethod
    async def _bounded_body(headers: Headers, receive):
        """Read the whole (small) body up front and hand the app a receive() that replays it.
        None if it is larger than MAX_BODY, declared or actual."""
        try:
            if int(headers.get("content-length") or 0) > MAX_BODY:
                return None
        except ValueError:
            return None
        body, more = bytearray(), True
        while more:
            message = await receive()
            if message["type"] != "http.request":      # client went away
                break
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_BODY:
                return None
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return replay

    @staticmethod
    def _with_headers(send, host: str, api: bool):
        extra = security_headers(host, api)

        async def wrapped(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", ())}
                message = {**message, "headers": list(message.get("headers", ())) + [h for h in extra if h[0] not in have]}
            await send(message)

        return wrapped

    # ---- responses ---------------------------------------------------------------------------
    @staticmethod
    def _wants_page(scope, path: str) -> bool:
        return (scope["type"] == "http" and scope.get("method") == "GET" and not path.startswith("/api/")
                and "text/html" in Headers(scope=scope).get("accept", ""))

    async def _unauthenticated(self, scope, receive, send, path: str):
        if self._wants_page(scope, path):
            target = path + ("?" + scope["query_string"].decode("latin-1") if scope.get("query_string") else "")
            return await self._redirect(send, "/login?next=" + quote(target, safe=""))
        return await self._deny(scope, receive, send, 401, "login_required", "Please log in.")

    async def _deny(self, scope, receive, send, status: int, code: str, detail: str):
        if scope["type"] == "websocket":
            if (await receive())["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
            return
        if self._wants_page(scope, scope["path"]):
            body = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                    f'<link rel="stylesheet" href="/static/style.css"><main><div class="card"><h2>Not available</h2>'
                    f'<p>{detail}</p><div class="actions"><a class="btn" href="/">Back to Live</a></div></div></main>'
                    ).encode()
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
            return await send({"type": "http.response.body", "body": body})
        return await self._json(send, status, {"error": code, "detail": detail})

    @staticmethod
    async def _json(send, status: int, doc: dict):
        body = json.dumps(doc).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _redirect(send, location: str):
        await send({"type": "http.response.start", "status": 302,
                    "headers": [(b"location", location.encode("latin-1")), (b"content-length", b"0"),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})

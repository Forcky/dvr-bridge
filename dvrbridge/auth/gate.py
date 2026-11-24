"""The one place every request passes: CSRF check, who is asking, and what they may reach.

Plain ASGI so it covers the live-view websocket as well as HTTP. Routes find the user in
``request.state.user`` (and the client address in ``request.state.client_ip``).
"""
from __future__ import annotations

import json
import logging
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
ADMIN_PATHS = frozenset({"/settings", "/users"})
ADMIN_PREFIXES = ("/api/settings/", "/api/users", "/api/audit")
# Reachable while a required second factor is not set up yet.
ENROL_PATHS = frozenset({"/account", "/api/me", "/api/account/totp/begin", "/api/account/totp/confirm"})
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


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
        if scope["type"] == "http" and method in UNSAFE:
            problem = csrf_problem(headers)
            if problem:
                return await self._json(send, 403, {"error": "forbidden", "detail": problem})

        who = self.svc.resolve(scope, _cookies(headers))
        state = scope.setdefault("state", {})
        state["user"] = who.user if not who.mfa_pending else None
        state["session_token"] = who.session_token
        state["client_ip"] = self.svc.client_ip(scope)
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

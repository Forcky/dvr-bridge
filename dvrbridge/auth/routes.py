"""Login, logout, first-run setup, OIDC callbacks, account self-service and user administration."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import segno
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .. import __version__
from . import passwords, totp
from .oidc import FLOW_COOKIE, OidcError, read_flow, safe_next
from .passwords import HashingBusy
from .ratelimit import ACCOUNT_LIMIT
from .service import (SECURE_SESSION_COOKIE, SESSION_COOKIE, TOTP_PENDING_TTL, USERNAME_MAX, AccessDenied, AuthService,
                      session_cookie)
from .store import ROLES, User

log = logging.getLogger("dvrbridge.auth")
STATIC = Path(__file__).resolve().parent.parent / "static"


def _err(status: int, code: str, detail: str, **headers: str) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status, headers=headers or None)


async def _body(request: Request) -> dict:
    try:
        doc = await request.json()
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _str(doc: dict, key: str, limit: int = 256) -> str:
    v = doc.get(key)
    return v[:limit] if isinstance(v, str) else ""


def _valid_username(name: str) -> str | None:
    name = name.strip()
    if not 1 <= len(name) <= USERNAME_MAX or any(ord(c) < 32 or c in "<>\"'&" for c in name):
        return None
    return name


def build_router(svc: AuthService) -> APIRouter:
    r = APIRouter()
    s = svc.s

    def secure(request: Request) -> bool:
        return (s.public_url.startswith("https://") or request.url.scheme == "https"
                or svc.forwarded_https(request.scope))

    def busy() -> JSONResponse:
        return _err(503, "busy", "The server is busy checking other logins. Try again in a moment.", **{"Retry-After": "2"})

    async def check_password(request: Request, password: str, stored: str | None) -> bool:
        """verify_password, bounded: a few at a time overall and per address. Raises HashingBusy."""
        ip, shared = request.state.client_ip, request.state.shared_address
        if not svc.throttle.enter(ip, shared=shared):
            raise HashingBusy
        try:
            return await svc.hashing.run(passwords.verify_password, password, stored)
        finally:
            svc.throttle.leave(ip, shared=shared)

    async def account_password_ok(request: Request, user: User, password: str) -> bool | JSONResponse:
        """The signed-in user's own password, for sensitive account changes. Guesses are limited per
        account, so a stolen session cannot be used to find the password."""
        key = f"acct:{user.id}"
        wait = svc.throttle.blocked(key)
        if wait:
            return _err(429, "too_many_attempts", "Too many wrong passwords. Try again later.", **{"Retry-After": str(wait)})
        try:
            ok = await check_password(request, password, svc.store.password_hash(user.id))
        except HashingBusy:
            return busy()
        if ok:
            svc.throttle.clear(key)
        else:
            svc.throttle.hit(key, ACCOUNT_LIMIT)
        return ok

    def start_session(request: Request, response: Response, user: User, *, mfa_pending: bool = False) -> None:
        token = svc.store.create_session(user.id, s.session_hours, mfa_pending=mfa_pending,
                                         ip=request.state.client_ip, user_agent=request.headers.get("user-agent", ""))
        is_secure = secure(request)
        response.set_cookie(SECURE_SESSION_COOKIE if is_secure else SESSION_COOKIE, token,
                            max_age=300 if mfa_pending else s.session_hours * 3600,
                            httponly=True, secure=is_secure, samesite="lax", path="/")
        if is_secure and request.cookies.get(SESSION_COOKIE):
            response.delete_cookie(SESSION_COOKIE, path="/")   # left over from before the cookie was Secure
        if not mfa_pending:
            svc.store.touch_login(user.id)

    def page(name: str):
        async def handler() -> FileResponse:
            return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})

        return handler

    for path, name in (("/login", "login.html"), ("/setup", "setup.html"), ("/account", "account.html"),
                       ("/users", "users.html")):
        r.add_api_route(path, page(name), include_in_schema=False)

    def drop_session_cookies(response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(SECURE_SESSION_COOKIE, path="/", secure=True)

    # ---- login -------------------------------------------------------------------------------
    @r.get("/api/auth/config")
    async def auth_config():
        return {"mode": s.auth_mode, "local_login": svc.local_login, "setup_required": svc.setup_required,
                "oidc": s.oidc_button_label if svc.oidc else None}

    @r.post("/api/auth/login")
    async def login(request: Request):
        ip, shared = request.state.client_ip, request.state.shared_address
        if not svc.local_login:
            return _err(404, "not_enabled", "Password login is not enabled.")
        doc = await _body(request)
        username, password = _str(doc, "username", USERNAME_MAX).strip(), _str(doc, "password")
        wait = svc.throttle.retry_after(ip, username)
        if wait:
            return _err(429, "too_many_attempts", f"Too many failed logins. Try again in {wait // 60 + 1} minutes.",
                        **{"Retry-After": str(wait)})
        user = svc.store.find_user(username) if username else None
        stored = svc.store.password_hash(user.id) if user and user.source == "local" else None
        try:
            ok = await check_password(request, password, stored)
        except HashingBusy:
            return busy()
        if not ok or user is None or user.disabled:
            svc.throttle.failure(ip, username, shared=shared)
            svc.store.audit("login_failed", username or None, "password", ip)
            return _err(401, "bad_credentials", "Wrong username or password.")
        resp = JSONResponse({"mfa": user.totp_enabled, "next": safe_next(_str(doc, "next", 512))})
        start_session(request, resp, user, mfa_pending=user.totp_enabled)
        if not user.totp_enabled:
            # Only a finished login clears the counters: with a second factor still to come, clearing
            # them here would hand out a fresh set of code guesses for every repeat of the password.
            svc.throttle.success(ip, username)
            svc.store.audit("login", user.username, "password", ip)
        return resp

    @r.post("/api/auth/totp")
    async def login_totp(request: Request):
        ip, shared = request.state.client_ip, request.state.shared_address
        token = session_cookie(request.cookies) or ""
        sess = svc.store.session(token)
        if not sess or not sess.mfa_pending:
            return _err(401, "login_required", "Your login expired. Please start again.")
        user = sess.user
        wait = svc.throttle.retry_after(ip, user.username, mfa=True)
        if wait:
            return _err(429, "too_many_attempts", "Too many failed codes. Try again later.", **{"Retry-After": str(wait)})
        doc = await _body(request)
        secret, last = svc.store.totp(user.id)
        try:
            step = totp.verify(secret, _str(doc, "code", 16), last_used_step=last) if secret else None
        except ValueError:      # an unusable stored secret
            step = None
        if step is None or not svc.store.use_totp_step(user.id, step):
            svc.throttle.failure(ip, user.username, shared=shared, mfa=True)
            if svc.throttle.retry_after("", user.username, mfa=True):
                svc.store.audit("two_factor_locked", user.username, "too many wrong codes", ip)
            svc.store.audit("login_failed", user.username, "two-factor code", ip)
            return _err(401, "bad_code", "That code is not right. Check your authenticator app's clock and try again.")
        svc.throttle.success(ip, user.username)
        svc.store.revoke(token)
        resp = JSONResponse({"ok": True, "next": safe_next(_str(doc, "next", 512))})
        start_session(request, resp, user)
        svc.store.audit("login", user.username, "password + two-factor", ip)
        return resp

    @r.post("/api/auth/logout")
    async def logout(request: Request):
        user = request.state.user
        for name in (SECURE_SESSION_COOKIE, SESSION_COOKIE):
            if request.cookies.get(name):
                svc.store.revoke(request.cookies[name])
        if user and user.id:
            svc.store.audit("logout", user.username, "", request.state.client_ip)
        resp = JSONResponse({"ok": True, "next": "/login"})
        drop_session_cookies(resp)
        return resp

    # ---- first run ---------------------------------------------------------------------------
    @r.post("/api/auth/setup")
    async def setup(request: Request):
        ip, shared = request.state.client_ip, request.state.shared_address
        if not svc.setup_required:
            return _err(409, "already_set_up", "An account already exists. Log in instead.")
        wait = svc.throttle.retry_after(ip, "#setup")
        if wait:
            return _err(429, "too_many_attempts", "Too many wrong setup codes. Try again later.")
        doc = await _body(request)
        if not svc.check_setup_code(_str(doc, "setup_code", 64)):
            svc.throttle.failure(ip, "#setup", shared=shared)
            return _err(403, "bad_setup_code", "That setup code is wrong. It is in the container's log.")
        username = _valid_username(_str(doc, "username", USERNAME_MAX))
        if not username:
            return _err(400, "bad_username", "Choose a username of 1-64 characters without < > \" ' &.")
        problem = passwords.password_problem(_str(doc, "password"), username)
        if problem:
            return _err(400, "weak_password", problem)
        try:
            hashed = await svc.hashing.run(passwords.hash_password, _str(doc, "password"))
        except HashingBusy:
            return busy()
        if not svc.setup_required:      # someone else finished the setup while this password was hashed
            return _err(409, "already_set_up", "An account already exists. Log in instead.")
        user = svc.store.create_user(username, "admin", "local", hashed)
        svc.setup_code = None
        svc.store.audit("setup", username, "first admin created", ip)
        resp = JSONResponse({"ok": True, "next": "/"})
        start_session(request, resp, user)
        return resp

    # ---- OIDC --------------------------------------------------------------------------------
    @r.get("/auth/oidc/start")
    async def oidc_start(request: Request, next: str = "/"):
        if not svc.oidc:
            return RedirectResponse("/login", status_code=302)
        try:
            url, flow = await svc.oidc.start(next)
        except OidcError as e:
            log.warning("OIDC start failed: %s", e)
            return RedirectResponse("/login?error=" + _q(str(e)), status_code=302)
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(FLOW_COOKIE, flow, max_age=600, httponly=True, secure=secure(request), samesite="lax",
                        path="/auth/oidc")
        return resp

    @r.get("/auth/oidc/callback")
    async def oidc_callback(request: Request):
        ip, shared = request.state.client_ip, request.state.shared_address
        if not svc.oidc:
            return RedirectResponse("/login", status_code=302)
        flow = read_flow(s.secret_key, request.cookies.get(FLOW_COOKIE))
        if flow is None:
            # No login was started in this browser: nothing to record (anyone can request this URL).
            return RedirectResponse("/login?error=" + _q("The login took too long or was started in another "
                                                         "browser. Please try again."), status_code=302)
        if svc.throttle.retry_after(ip, "#oidc"):
            return RedirectResponse("/login?error=" + _q("Too many failed logins. Try again later."), status_code=302)
        try:
            ident, next_path = await svc.oidc.finish(flow, dict(request.query_params))
            user = svc.provision(ident, source="oidc", admin_group=s.oidc_admin_group,
                                 allowed_group=s.oidc_allowed_group)
        except (OidcError, AccessDenied) as e:
            log.warning("OIDC login refused: %s", e)
            svc.throttle.failure(ip, "#oidc", shared=shared)
            svc.store.audit("login_failed", None, f"oidc: {e}"[:200], ip)
            resp = RedirectResponse("/login?error=" + _q(str(e)), status_code=302)
            resp.delete_cookie(FLOW_COOKIE, path="/auth/oidc")
            return resp
        resp = RedirectResponse(next_path, status_code=302)
        resp.delete_cookie(FLOW_COOKIE, path="/auth/oidc")
        start_session(request, resp, user)
        svc.store.audit("login", user.username, "oidc", ip)
        return resp

    # ---- the signed-in user ------------------------------------------------------------------
    @r.get("/api/me")
    async def me(request: Request):
        u = request.state.user
        return {"username": u.username, "role": u.role, "source": u.source, "totp_enabled": u.totp_enabled,
                "totp_required": s.totp == "required" and u.source == "local",
                "can_change_password": u.source == "local" and bool(u.id), "mode": s.auth_mode,
                "can_logout": s.auth_mode in ("local", "oidc") or bool(request.state.session_token),
                "version": __version__}

    @r.post("/api/account/password")
    async def change_password(request: Request):
        u = request.state.user
        if u.source != "local" or not u.id:
            return _err(400, "not_local", "This account's password is managed elsewhere.")
        doc = await _body(request)
        ok = await account_password_ok(request, u, _str(doc, "current"))
        if ok is not True:
            if ok is False:
                svc.store.audit("password_change_failed", u.username, "wrong current password", request.state.client_ip)
                return _err(403, "bad_password", "Your current password is not right.")
            return ok
        problem = passwords.password_problem(_str(doc, "new"), u.username)
        if problem:
            return _err(400, "weak_password", problem)
        try:
            hashed = await svc.hashing.run(passwords.hash_password, _str(doc, "new"))
        except HashingBusy:
            return busy()
        svc.store.update_user(u.id, password_hash=hashed)
        svc.store.revoke_user_sessions(u.id, keep=request.state.session_token)
        svc.store.audit("password_changed", u.username, "", request.state.client_ip)
        return {"ok": True}

    @r.post("/api/account/totp/begin")
    async def totp_begin(request: Request):
        """Offer a new secret. It stays on the server until confirmed with a code from it; the account's
        password is asked for first, so a borrowed session cannot add (or replace) the second factor."""
        u = request.state.user
        if u.source != "local" or not u.id:
            return _err(400, "not_local", "Two-factor for this account is handled by your identity provider.")
        doc = await _body(request)
        ok = await account_password_ok(request, u, _str(doc, "password"))
        if ok is not True:
            return ok if ok is not False else _err(403, "bad_password", "Your password is not right.")
        if u.totp_enabled:
            # Replacing an authenticator also needs a code from the current one.
            current, last = svc.store.totp(u.id)
            try:
                step = totp.verify(current or "", _str(doc, "code", 16), last_used_step=last) if current else None
            except ValueError:
                step = None
            if step is None or not svc.store.use_totp_step(u.id, step):
                svc.throttle.hit(f"acct:{u.id}", ACCOUNT_LIMIT)
                return _err(403, "bad_code", "Enter a current code from your existing authenticator to replace it.")
        secret = totp.new_secret()
        svc.pending_totp[u.id] = (request.state.session_token, secret, time.monotonic() + TOTP_PENDING_TTL)
        uri = totp.provisioning_uri(secret, u.username)
        return {"secret": secret, "uri": uri, "qr_svg": segno.make_qr(uri, error="m").svg_inline(scale=5)}

    @r.post("/api/account/totp/confirm")
    async def totp_confirm(request: Request):
        u = request.state.user
        if u.source != "local" or not u.id:
            return _err(400, "not_local", "Two-factor for this account is handled by your identity provider.")
        doc = await _body(request)
        pending = svc.pending_totp.get(u.id)
        if not pending or pending[0] != request.state.session_token or pending[2] < time.monotonic():
            svc.pending_totp.pop(u.id, None)
            return _err(400, "not_started", "Start the set-up again: it was not begun in this session or it expired.")
        secret = pending[1]
        wait = svc.throttle.blocked(f"acct:{u.id}")
        if wait:
            return _err(429, "too_many_attempts", "Too many wrong codes. Try again later.", **{"Retry-After": str(wait)})
        step = totp.verify(secret, _str(doc, "code", 16))
        if step is None:
            svc.throttle.hit(f"acct:{u.id}", ACCOUNT_LIMIT)
            return _err(400, "bad_code", "That code does not match. Scan the code again and enter the current 6 digits.")
        svc.pending_totp.pop(u.id, None)
        svc.store.set_totp(u.id, secret, step)
        svc.store.revoke_user_sessions(u.id, keep=request.state.session_token)
        svc.store.audit("totp_enabled", u.username, "", request.state.client_ip)
        return {"ok": True}

    @r.post("/api/account/totp/disable")
    async def totp_disable(request: Request):
        u = request.state.user
        if s.totp == "required":
            return _err(400, "required", "Two-factor is required on this server.")
        if u.source != "local" or not u.id:
            return _err(400, "not_local", "Two-factor for this account is handled by your identity provider.")
        doc = await _body(request)
        ok = await account_password_ok(request, u, _str(doc, "password"))
        if ok is not True:
            return ok if ok is not False else _err(403, "bad_password", "Your password is not right.")
        svc.store.set_totp(u.id, None)
        svc.pending_totp.pop(u.id, None)
        svc.store.audit("totp_disabled", u.username, "", request.state.client_ip)
        return {"ok": True}

    # ---- administration ----------------------------------------------------------------------
    @r.get("/api/users")
    async def list_users():
        return {"users": [u.public() for u in svc.store.users()], "local_login": svc.local_login}

    @r.post("/api/users")
    async def create_user(request: Request):
        doc = await _body(request)
        username = _valid_username(_str(doc, "username", USERNAME_MAX))
        role = _str(doc, "role", 16) or "viewer"
        if not username:
            return _err(400, "bad_username", "Usernames are 1-64 characters without < > \" ' &.")
        if role not in ROLES:
            return _err(400, "bad_role", "Role must be admin or viewer.")
        if svc.store.find_user(username):
            return _err(409, "exists", "That username is taken.")
        problem = passwords.password_problem(_str(doc, "password"), username)
        if problem:
            return _err(400, "weak_password", problem)
        try:
            hashed = await svc.hashing.run(passwords.hash_password, _str(doc, "password"))
        except HashingBusy:
            return busy()
        if svc.store.find_user(username):
            return _err(409, "exists", "That username is taken.")
        user = svc.store.create_user(username, role, "local", hashed)
        svc.store.audit("user_created", request.state.user.username, f"{username} ({role})", request.state.client_ip)
        return user.public()

    @r.patch("/api/users/{user_id}")
    async def update_user(user_id: int, request: Request):
        me_ = request.state.user
        target = svc.store.get_user(user_id)
        if not target:
            return _err(404, "not_found", "No such user.")
        doc = await _body(request)
        changes: dict = {}
        if "role" in doc:
            if doc["role"] not in ROLES:
                return _err(400, "bad_role", "Role must be admin or viewer.")
            changes["role"] = doc["role"]
        if "disabled" in doc:
            changes["disabled"] = bool(doc["disabled"])
        if doc.get("password"):
            if target.source != "local":
                return _err(400, "not_local", "This account's password is managed elsewhere.")
            problem = passwords.password_problem(_str(doc, "password"), target.username)
            if problem:
                return _err(400, "weak_password", problem)
            try:
                changes["password_hash"] = await svc.hashing.run(passwords.hash_password, _str(doc, "password"))
            except HashingBusy:
                return busy()
        if not changes and not doc.get("reset_totp"):
            return _err(400, "nothing_to_change", "Nothing to change.")
        losing_admin = target.is_admin and not target.disabled and (
            changes.get("role") == "viewer" or changes.get("disabled"))
        if losing_admin and svc.store.count_active_admins() <= 1:
            return _err(409, "last_admin", "This is the only active admin; make someone else admin first.")
        if changes:
            target = svc.store.update_user(user_id, **changes)
        if doc.get("reset_totp"):
            svc.store.set_totp(user_id, None)
            svc.pending_totp.pop(user_id, None)
        if changes.get("disabled") or "password_hash" in changes or doc.get("reset_totp"):
            svc.store.revoke_user_sessions(user_id, keep=request.state.session_token if user_id == me_.id else None)
        what = ", ".join(k if k != "password_hash" else "password" for k in changes) + (
            ", two-factor reset" if doc.get("reset_totp") else "")
        svc.store.audit("user_updated", me_.username, f"{target.username}: {what}", request.state.client_ip)
        return target.public()

    @r.delete("/api/users/{user_id}")
    async def delete_user(user_id: int, request: Request):
        me_ = request.state.user
        target = svc.store.get_user(user_id)
        if not target:
            return _err(404, "not_found", "No such user.")
        if target.id == me_.id:
            return _err(409, "self", "You cannot delete your own account.")
        if target.is_admin and not target.disabled and svc.store.count_active_admins() <= 1:
            return _err(409, "last_admin", "This is the only active admin.")
        svc.store.delete_user(user_id)
        svc.pending_totp.pop(user_id, None)
        svc.store.audit("user_deleted", me_.username, target.username, request.state.client_ip)
        return {"ok": True}

    @r.get("/api/audit")
    async def audit(limit: int = 200):
        return {"events": svc.store.audit_log(limit)}

    return r


def _q(text: str) -> str:
    from urllib.parse import quote

    return quote(text[:300], safe="")

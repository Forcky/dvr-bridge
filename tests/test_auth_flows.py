import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from dvrbridge.auth import totp
from dvrbridge.auth.gate import AuthGate
from dvrbridge.auth.netutil import client_ip, parse_networks
from dvrbridge.auth.oidc import safe_next
from dvrbridge.auth.service import AuthService
from dvrbridge.config import Settings
from dvrbridge.dvr import DvrClient
from dvrbridge.main import create_app

JSON = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
HTML = {"Accept": "text/html"}
PW = "correct horse battery"


class StubG2R:
    running = True

    async def start(self): ...
    async def stop(self): ...


@asynccontextmanager
async def app_client(fake_dvr, tmp_path, *, oidc_transport=None, client=("127.0.0.1", 50000), **kw):
    s = Settings(dvr_host="dvr.test", dvr_user="tester", dvr_password="s3cret-pw", secret_key="k" * 32,
                 poll_interval=3600, data_dir=str(tmp_path), **kw)
    dvr = DvrClient("dvr.test", "tester", "s3cret-pw", transport=fake_dvr.transport(), min_relogin_interval=0)
    auth = AuthService(s, oidc_transport=oidc_transport)
    app = create_app(s, dvr=dvr, go2rtc=StubG2R(), auth=auth)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        transport = httpx.ASGITransport(app=app, client=client)
        # Session cookies are Secure when the public URL is https; a browser sends them back only over https.
        base = "https://gw" if s.public_url.startswith("https://") else "http://gw"
        async with httpx.AsyncClient(transport=transport, base_url=base) as c:
            c.auth_service = auth
            yield c


async def post(c, path, doc=None, **kw):
    return await c.post(path, content=json.dumps(doc or {}), headers={**JSON, **kw.pop("headers", {})}, **kw)


async def setup_admin(c, username="alice"):
    r = await post(c, "/api/auth/setup", {"setup_code": c.auth_service.setup_code, "username": username, "password": PW})
    assert r.status_code == 200, r.text


# ---- local accounts -----------------------------------------------------------------------------
async def test_everything_needs_a_login_except_the_public_bits(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        assert (await c.get("/api/channels")).status_code == 401
        r = await c.get("/recordings?date=2026-11-22", headers=HTML)
        assert r.status_code == 302 and r.headers["location"] == "/login?next=%2Frecordings%3Fdate%3D2026-11-22"
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/metrics")).status_code == 200
        assert (await c.get("/static/style.css")).status_code == 200
        assert (await c.get("/login")).status_code == 200
        cfg = (await c.get("/api/auth/config")).json()
        assert cfg == {"mode": "local", "local_login": True, "setup_required": True, "oidc": None}


async def test_first_run_setup(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        code = c.auth_service.setup_code
        assert code and len(code) == 19
        assert (await post(c, "/api/auth/setup", {"setup_code": "WRONG", "username": "a", "password": PW})).status_code == 403
        r = await post(c, "/api/auth/setup", {"setup_code": code, "username": "alice", "password": "short"})
        assert r.status_code == 400 and r.json()["error"] == "weak_password"
        await setup_admin(c)
        me = (await c.get("/api/me")).json()
        assert me["username"] == "alice" and me["role"] == "admin"
        assert (await post(c, "/api/auth/setup", {"setup_code": code, "username": "bob", "password": PW})).status_code == 409
        assert c.auth_service.setup_code is None


async def test_login_logout_and_generic_errors(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        await setup_admin(c)
        await post(c, "/api/auth/logout")
        assert (await c.get("/api/channels")).status_code == 401
        bad_user = await post(c, "/api/auth/login", {"username": "nobody", "password": PW})
        bad_pw = await post(c, "/api/auth/login", {"username": "alice", "password": "nope nope nope"})
        assert bad_user.status_code == bad_pw.status_code == 401 and bad_user.json() == bad_pw.json()
        r = await post(c, "/api/auth/login", {"username": "ALICE", "password": PW, "next": "//evil.example/x"})
        assert r.status_code == 200 and r.json() == {"mfa": False, "next": "/"}
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert (await c.get("/api/channels")).status_code == 200
        events = [e["action"] for e in (await c.get("/api/audit")).json()["events"]]
        assert events[:3] == ["login", "login_failed", "login_failed"]


async def test_csrf_applies_to_login(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        r = await c.post("/api/auth/login", content=json.dumps({"username": "a", "password": "b"}),
                         headers={"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403


async def test_failed_logins_are_throttled(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        await setup_admin(c)
        for _ in range(10):
            await post(c, "/api/auth/login", {"username": "alice", "password": "wrong wrong wrong"})
        r = await post(c, "/api/auth/login", {"username": "alice", "password": PW})
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


async def test_two_factor_enrol_and_login(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        await setup_admin(c)
        begin = (await post(c, "/api/account/totp/begin")).json()
        assert begin["qr_svg"].startswith("<svg") and begin["uri"].startswith("otpauth://")
        secret = begin["secret"]
        bad = await post(c, "/api/account/totp/confirm", {"secret": secret, "code": "000000"})
        assert bad.status_code == 400
        step = int(time.time() // 30)
        assert (await post(c, "/api/account/totp/confirm",
                           {"secret": secret, "code": totp.code_at(secret, step)})).status_code == 200
        await post(c, "/api/auth/logout")

        r = await post(c, "/api/auth/login", {"username": "alice", "password": PW})
        assert r.json()["mfa"] is True
        assert (await c.get("/api/channels")).status_code == 401          # half-logged-in is not logged in
        assert (await post(c, "/api/auth/totp", {"code": "123456"})).status_code == 401
        # the code used at enrolment cannot be replayed; the next step's code works
        assert (await post(c, "/api/auth/totp", {"code": totp.code_at(secret, step)})).status_code == 401
        ok = await post(c, "/api/auth/totp", {"code": totp.code_at(secret, step + 1)})
        assert ok.status_code == 200
        assert (await c.get("/api/channels")).status_code == 200


async def test_roles_and_user_admin(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        await setup_admin(c)
        r = await post(c, "/api/users", {"username": "bob", "password": "bobs password 1", "role": "viewer"})
        assert r.status_code == 200
        bob_id = r.json()["id"]
        assert (await post(c, "/api/users", {"username": "BOB", "password": "x" * 12})).status_code == 409
        await post(c, "/api/auth/logout")

        await post(c, "/api/auth/login", {"username": "bob", "password": "bobs password 1"})
        assert (await c.get("/api/channels")).status_code == 200
        assert (await c.get("/api/settings/channels")).status_code == 403
        assert (await c.get("/api/users")).status_code == 403
        assert (await c.get("/settings", headers=HTML)).status_code == 403
        assert (await post(c, "/api/settings/channels/1", {"name": "x"})).status_code == 403
        await post(c, "/api/auth/logout")

        await post(c, "/api/auth/login", {"username": "alice", "password": PW})
        me_id = (await c.get("/api/me")).json()
        users = (await c.get("/api/users")).json()["users"]
        alice_id = next(u["id"] for u in users if u["username"] == "alice")
        r = await c.request("PATCH", f"/api/users/{alice_id}", content=json.dumps({"role": "viewer"}), headers=JSON)
        assert r.status_code == 409 and r.json()["error"] == "last_admin"
        assert (await c.request("DELETE", f"/api/users/{alice_id}", headers=JSON)).status_code == 409
        r = await c.request("PATCH", f"/api/users/{bob_id}", content=json.dumps({"disabled": True}), headers=JSON)
        assert r.status_code == 200 and r.json()["disabled"] is True
        assert (await c.request("DELETE", f"/api/users/{bob_id}", headers=JSON)).status_code == 200
        assert me_id["role"] == "admin"


async def test_disabling_a_user_ends_their_sessions(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        await setup_admin(c)
        bob = (await post(c, "/api/users", {"username": "bob", "password": "bobs password 1"})).json()
        store = c.auth_service.store
        token = store.create_session(bob["id"], 1)
        assert store.session(token)
        await c.request("PATCH", f"/api/users/{bob['id']}", content=json.dumps({"disabled": True}), headers=JSON)
        assert store.session(token) is None


async def test_required_two_factor_forces_enrolment(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path, totp="required") as c:
        await setup_admin(c)
        r = await c.get("/api/channels")
        assert r.status_code == 403 and r.json()["error"] == "totp_required"
        assert (await c.get("/", headers=HTML)).headers["location"] == "/account?enrol=1"
        assert (await c.get("/api/me")).json()["totp_required"] is True
        secret = (await post(c, "/api/account/totp/begin")).json()["secret"]
        await post(c, "/api/account/totp/confirm", {"secret": secret, "code": totp.code_at(secret, int(time.time() // 30))})
        assert (await c.get("/api/channels")).status_code == 200


async def test_password_change_signs_out_other_sessions(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        await setup_admin(c)
        store = c.auth_service.store
        other = store.create_session(store.find_user("alice").id, 1)
        assert (await post(c, "/api/account/password", {"current": "nope", "new": "another long one"})).status_code == 403
        assert (await post(c, "/api/account/password", {"current": PW, "new": "another long one"})).status_code == 200
        assert store.session(other) is None
        assert (await c.get("/api/channels")).status_code == 200


# ---- header mode --------------------------------------------------------------------------------
SECRET = "s" * 40


async def test_header_mode_needs_the_secret_and_a_trusted_peer(fake_dvr, tmp_path):
    kw = dict(auth_mode="header", header_secret=SECRET, trusted_proxies="127.0.0.1/32",
              header_user="X-User", header_groups="X-Groups", header_admin_group="cctv-admins", local_login=False)
    async with app_client(fake_dvr, tmp_path, **kw) as c:
        assert (await c.get("/api/channels")).status_code == 401
        assert (await c.get("/api/channels", headers={"X-User": "carol"})).status_code == 401
        good = {"X-User": "carol", "X-DVR-Bridge-Proxy-Secret": SECRET}
        assert (await c.get("/api/channels", headers=good)).status_code == 200
        assert (await c.get("/api/settings/channels", headers=good)).status_code == 403
        admin = {**good, "X-Groups": "family|cctv-admins"}
        assert (await c.get("/api/me", headers=admin)).json()["role"] == "admin"
        assert (await c.get("/api/me", headers=good)).json()["role"] == "viewer"  # follows the groups each time
        assert (await post(c, "/api/auth/login", {"username": "carol", "password": PW})).status_code == 404
    async with app_client(fake_dvr, tmp_path / "2", client=("10.9.9.9", 1), **kw) as c:
        assert (await c.get("/api/channels", headers=good)).status_code == 401   # peer is not a trusted proxy


async def test_header_user_cannot_take_over_a_local_account(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path, auth_mode="header", header_secret=SECRET) as c:
        await setup_admin(c, "alice")       # break-glass local admin (local_login defaults to true)
    # A fresh client (no session cookie) on the same data directory claims to be "alice" by header.
    async with app_client(fake_dvr, tmp_path, auth_mode="header", header_secret=SECRET) as c:
        r = await c.get("/api/channels", headers={"Remote-User": "alice", "X-DVR-Bridge-Proxy-Secret": SECRET})
        assert r.status_code == 401
        r = await c.get("/api/channels", headers={"Remote-User": "erin", "X-DVR-Bridge-Proxy-Secret": SECRET})
        assert r.status_code == 200


# ---- websocket ----------------------------------------------------------------------------------
async def test_websocket_without_login_is_closed(fake_dvr, tmp_path):
    s = Settings(dvr_host="dvr.test", dvr_user="t", dvr_password="p", secret_key="k" * 32, data_dir=str(tmp_path))
    svc = AuthService(s)
    called, sent = [], []

    async def inner(scope, receive, send):
        called.append(scope["path"])

    async def receive():
        return {"type": "websocket.connect"}

    async def send(m):
        sent.append(m)

    await AuthGate(inner, service=svc)({"type": "websocket", "path": "/ws/live/1/sub", "headers": [],
                                        "client": ("127.0.0.1", 1)}, receive, send)
    assert called == [] and sent == [{"type": "websocket.close", "code": 1008}]
    svc.store.close()


# ---- OIDC ---------------------------------------------------------------------------------------
ISSUER = "https://id.example.com/app/"


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class FakeIdp:
    def __init__(self):
        self.claims: dict = {}
        self.issuer = ISSUER
        self.token_requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.issuer, "authorization_endpoint": "https://id.example.com/authorize",
                "token_endpoint": "https://id.example.com/token", "userinfo_endpoint": "https://id.example.com/userinfo",
                "token_endpoint_auth_methods_supported": ["client_secret_basic"]})
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            self.token_requests.append({"form": form, "auth": request.headers.get("authorization")})
            return httpx.Response(200, json={"id_token": jwt(self.claims), "access_token": "at"})
        if request.url.path == "/userinfo":
            return httpx.Response(200, json={"sub": self.claims.get("sub"), "groups": ["cctv-admins"]})
        return httpx.Response(404)


OIDC = dict(auth_mode="oidc", oidc_issuer=ISSUER, oidc_client_id="dvr-bridge", oidc_client_secret="shh",
            public_url="https://cams.example.com", oidc_admin_group="cctv-admins")


async def oidc_login(c, idp, **claims_override):
    r = await c.get("/auth/oidc/start", params={"next": "/recordings"})
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["https://cams.example.com/auth/oidc/callback"]
    idp.claims = {"iss": idp.issuer, "aud": "dvr-bridge", "sub": "u-123", "exp": time.time() + 300,
                  "iat": time.time(), "nonce": q["nonce"][0], "preferred_username": "dave", **claims_override}
    return await c.get("/auth/oidc/callback", params={"state": q["state"][0], "code": "abc"}), q


async def test_oidc_login_end_to_end(fake_dvr, tmp_path):
    idp = FakeIdp()
    async with app_client(fake_dvr, tmp_path, oidc_transport=httpx.MockTransport(idp.handler), local_login=False,
                          **OIDC) as c:
        assert (await c.get("/api/auth/config")).json()["oidc"] == "Sign in with SSO"
        r, q = await oidc_login(c, idp)
        assert r.status_code == 302 and r.headers["location"] == "/recordings", r.headers
        me = (await c.get("/api/me")).json()
        assert me["username"] == "dave" and me["role"] == "admin" and me["source"] == "oidc"   # groups via userinfo
        tr = idp.token_requests[0]
        assert tr["auth"].startswith("Basic ") and tr["form"]["code_verifier"] and tr["form"]["code"] == ["abc"]


@pytest.mark.parametrize("override, why", [
    ({"nonce": "other"}, "nonce"),
    ({"aud": "someone-else"}, "audience"),
    ({"iss": "https://evil.example.com/"}, "issuer"),
    ({"exp": time.time() - 3600}, "expiry"),
    ({"aud": ["dvr-bridge", "x"], "azp": "x"}, "authorized party"),
])
async def test_oidc_rejects_bad_id_tokens(fake_dvr, tmp_path, override, why):
    idp = FakeIdp()
    async with app_client(fake_dvr, tmp_path, oidc_transport=httpx.MockTransport(idp.handler), **OIDC) as c:
        r, _ = await oidc_login(c, idp, **override)
        assert r.status_code == 302 and r.headers["location"].startswith("/login?error=")
        assert why.replace(" ", "%20") in r.headers["location"]
        assert (await c.get("/api/channels")).status_code == 401


async def test_oidc_rejects_a_forged_state(fake_dvr, tmp_path):
    idp = FakeIdp()
    async with app_client(fake_dvr, tmp_path, oidc_transport=httpx.MockTransport(idp.handler), **OIDC) as c:
        await c.get("/auth/oidc/start")
        r = await c.get("/auth/oidc/callback", params={"state": "forged", "code": "abc"})
        assert r.headers["location"].startswith("/login?error=") and not idp.token_requests


async def test_oidc_cannot_take_over_a_local_account(fake_dvr, tmp_path):
    idp = FakeIdp()
    async with app_client(fake_dvr, tmp_path, oidc_transport=httpx.MockTransport(idp.handler), **OIDC) as c:
        await setup_admin(c, "dave")
        await post(c, "/api/auth/logout")
        r, _ = await oidc_login(c, idp)
        assert r.headers["location"].startswith("/login?error=") and "different%20kind" in r.headers["location"]


async def test_oidc_issuer_mismatch_in_discovery(fake_dvr, tmp_path):
    idp = FakeIdp()
    idp.issuer = "https://other.example.com/"
    async with app_client(fake_dvr, tmp_path, oidc_transport=httpx.MockTransport(idp.handler), **OIDC) as c:
        r = await c.get("/auth/oidc/start")
        assert r.headers["location"].startswith("/login?error=")


async def test_oidc_allowed_group(fake_dvr, tmp_path):
    idp = FakeIdp()
    async with app_client(fake_dvr, tmp_path, oidc_transport=httpx.MockTransport(idp.handler),
                          **{**OIDC, "oidc_admin_group": "", "oidc_allowed_group": "family"}) as c:
        r, _ = await oidc_login(c, idp, groups=["neighbours"])
        assert r.headers["location"].startswith("/login?error=")
        r, _ = await oidc_login(c, idp, groups=["family"])
        assert r.headers["location"] == "/recordings"
        assert (await c.get("/api/me")).json()["role"] == "viewer"


# ---- helpers ------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [("/recordings?date=1", "/recordings?date=1"), ("//evil.example", "/"),
                                       ("https://evil.example", "/"), ("/\\evil", "/"), (None, "/"), ("x", "/")])
def test_safe_next(raw, want):
    assert safe_next(raw) == want


def test_client_ip_trusts_forwarded_for_only_from_proxies():
    trusted = parse_networks("172.28.3.0/24")
    scope = lambda peer, xff: {"client": (peer, 1), "headers": [(b"x-forwarded-for", xff.encode())]}  # noqa: E731
    assert client_ip(scope("203.0.113.9", "1.2.3.4"), trusted) == "203.0.113.9"
    assert client_ip(scope("172.28.3.5", "1.2.3.4"), trusted) == "1.2.3.4"
    assert client_ip(scope("172.28.3.5", "6.6.6.6, 1.2.3.4, 172.28.3.7"), trusted) == "1.2.3.4"
    assert client_ip(scope("172.28.3.5", "junk"), trusted) == "172.28.3.5"
    assert client_ip(scope("172.28.3.5", "1.2.3.4"), ()) == "172.28.3.5"

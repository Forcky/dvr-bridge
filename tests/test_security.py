"""Regression tests for the pre-release security review (2026-11): each one failed before its fix."""
import asyncio
import json
import sqlite3
import time

import httpx
import pytest

from dvrbridge.auth import totp
from dvrbridge.auth.gate import MAX_BODY
from dvrbridge.auth.oidc import Identity
from dvrbridge.auth.ratelimit import MFA_LIMIT, LoginThrottle, ip_key
from dvrbridge.auth.service import AccessDenied, AuthService
from dvrbridge.auth.store import SCHEMA, Store
from dvrbridge.config import load
from dvrbridge.dvr import DvrClient
from dvrbridge.frigate import EVENT_ID
from dvrbridge.main import create_app, mse_request, public_go2rtc_text
from dvrbridge.state import Monitor

from test_auth_flows import JSON, PW, StubG2R, app_client, post, setup_admin
from test_system_settings import base_env, put
from test_system_settings import client as settings_client

OIDC = dict(auth_mode="oidc", oidc_issuer="https://idp.test", oidc_client_id="x", oidc_client_secret="y",
            public_url="https://gw", trusted_proxies="127.0.0.1")


async def enrol_totp(c) -> str:
    secret = (await post(c, "/api/account/totp/begin", {"password": PW})).json()["secret"]
    r = await post(c, "/api/account/totp/confirm", {"code": totp.code_at(secret, int(time.time() // 30))})
    assert r.status_code == 200, r.text
    return secret


# ---- H1 / M3: memory -----------------------------------------------------------------------------
async def test_a_burst_of_logins_hashes_only_a_few_passwords_at_once(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        res = await asyncio.gather(*(post(c, "/api/auth/login", {"username": f"u{i}", "password": "p" * 40})
                                     for i in range(50)))
        codes = [r.status_code for r in res]
        assert c.auth_service.hashing.peak <= 2
        assert set(codes) <= {401, 429, 503} and 503 in codes       # the excess is turned away, not queued
        assert all(r.headers.get("retry-after") for r in res if r.status_code == 503)


async def test_request_bodies_are_capped(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        big = json.dumps({"username": "x", "password": "y", "pad": "A" * (MAX_BODY + 1)})
        assert (await c.post("/api/auth/login", content=big, headers=JSON)).status_code == 413

        async def chunks():                    # no Content-Length: the cap must hold while reading
            for _ in range(40):
                yield b"A" * 4096

        assert (await c.post("/api/auth/login", content=chunks(), headers=JSON)).status_code == 413
        assert (await post(c, "/api/auth/login", {"username": "x", "password": "y"})).status_code == 401


# ---- H2: second factor ---------------------------------------------------------------------------
async def test_two_factor_guesses_are_limited_even_when_the_password_is_repeated(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        await setup_admin(c)
        secret = await enrol_totp(c)
        await post(c, "/api/auth/logout")
        c.cookies.clear()
        wrong = "000000" if "000000" not in {totp.code_at(secret, int(time.time() // 30) + d) for d in (-1, 0, 1)} else "111111"
        statuses = []
        for _ in range(3):
            r = await post(c, "/api/auth/login", {"username": "alice", "password": PW})
            if r.status_code != 200:
                statuses.append(r.status_code)
                continue
            for _ in range(4):
                statuses.append((await post(c, "/api/auth/totp", {"code": wrong})).status_code)
        assert statuses[:MFA_LIMIT] == [401] * MFA_LIMIT
        assert set(statuses[MFA_LIMIT:]) == {429}                   # a fresh password login buys no new guesses
        assert any(e["action"] == "two_factor_locked" for e in c.auth_service.store.audit_log())


# ---- M1: metrics ---------------------------------------------------------------------------------
async def test_metrics_need_a_token_an_allowed_address_or_an_admin(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path, metrics_token="t" * 24) as c:
        assert (await c.get("/metrics")).status_code == 401
        assert (await c.get("/metrics", headers={"Authorization": "Bearer " + "t" * 24})).status_code == 200
        await setup_admin(c)
        assert "dvr_channel_info" in (await c.get("/metrics")).text          # signed-in admin
        store = c.auth_service.store
        store.update_user(store.find_user("alice").id, role="viewer")
        assert (await c.get("/metrics")).status_code == 401                 # a viewer is not enough


async def test_metrics_allow_list(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path / "a", metrics_allow="172.20.0.9, 10.1.0.0/16", client=("172.20.0.9", 1)) as c:
        assert (await c.get("/metrics")).status_code == 200
    async with app_client(fake_dvr, tmp_path / "b", metrics_allow="172.20.0.9", client=("172.20.0.10", 1)) as c:
        assert (await c.get("/metrics")).status_code == 401


# ---- M2: OIDC identity ---------------------------------------------------------------------------
async def test_oidc_accounts_follow_the_subject_not_the_username(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path, **OIDC) as c:
        svc = c.auth_service
        kw = dict(source="oidc", admin_group="", allowed_group="")
        boss = svc.provision(Identity("boss", (), "https://idp.test sub-A"), **kw)
        svc.store.update_user(boss.id, role="admin")
        with pytest.raises(AccessDenied):                            # someone else took the name (any case)
            svc.provision(Identity("Boss", (), "https://idp.test sub-B"), **kw)
        renamed = svc.provision(Identity("the-boss", (), "https://idp.test sub-A"), **kw)
        assert renamed.id == boss.id and renamed.is_admin            # same person, new name at the provider
        # An account from before subjects were stored is bound at its next login, then protected too.
        old = svc.store.create_user("legacy", "viewer", "oidc")
        assert svc.provision(Identity("legacy", (), "https://idp.test sub-L"), **kw).id == old.id
        with pytest.raises(AccessDenied):
            svc.provision(Identity("legacy", (), "https://idp.test sub-M"), **kw)


def test_database_upgrades_from_version_1(tmp_path):
    path = tmp_path / "dvrbridge.db"
    db = sqlite3.connect(path)
    db.executescript("BEGIN;" + SCHEMA[0] + "PRAGMA user_version = 1; COMMIT;")
    db.execute("INSERT INTO users (username, role, source, created_at) VALUES ('old', 'admin', 'oidc', 0)")
    db.commit()
    db.close()
    store = Store(path)
    assert store.find_user("old").is_admin and store.subject(store.find_user("old").id) is None
    store.set_subject(store.find_user("old").id, "iss sub")
    assert store.find_subject("iss sub").username == "old"
    assert (path.stat().st_mode & 0o077) == 0
    store.close()


# ---- M4 / L7 / L10: proxies and throttling ---------------------------------------------------------
async def test_an_untrusted_proxy_address_is_not_locked_out_by_one_visitor(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path, client=("172.18.0.2", 4000)) as c:      # trusted_proxies not set
        await setup_admin(c)
        await post(c, "/api/auth/logout")
        c.cookies.clear()
        for i in range(12):
            r = await post(c, "/api/auth/login", {"username": f"nobody{i}", "password": "x" * 12},
                           headers={"X-Forwarded-For": "203.0.113.66"})
            assert r.status_code == 401
        r = await post(c, "/api/auth/login", {"username": "alice", "password": PW}, headers={"X-Forwarded-For": "198.51.100.7"})
        assert r.status_code == 200
        for _ in range(30):                                          # the per-account limit still holds
            await post(c, "/api/auth/login", {"username": "alice", "password": "wrong wrong"},
                       headers={"X-Forwarded-For": "203.0.113.66"})
        r = await post(c, "/api/auth/login", {"username": "alice", "password": PW}, headers={"X-Forwarded-For": "198.51.100.7"})
        assert r.status_code == 429


async def test_deployment_warnings(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path / "a", public_url="https://cams.example.com") as c:
        assert any("trusted_proxies" in w for w in c.auth_service.warnings())
        await setup_admin(c)
        assert (await c.get("/api/settings/system")).json()["warnings"]
    async with app_client(fake_dvr, tmp_path / "b", public_url="https://cams.example.com", trusted_proxies="10.0.0.1") as c:
        assert c.auth_service.warnings() == []


async def test_session_cookie_is_secure_behind_a_trusted_tls_proxy(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path / "a", trusted_proxies="127.0.0.1") as c:
        r = await post(c, "/api/auth/setup", {"setup_code": c.auth_service.setup_code, "username": "alice", "password": PW},
                       headers={"X-Forwarded-Proto": "https"})
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-dvrbridge_session=") and "Secure" in cookie and "Path=/" in cookie
    async with app_client(fake_dvr, tmp_path / "b") as c:           # the header from an untrusted peer means nothing
        r = await post(c, "/api/auth/setup", {"setup_code": c.auth_service.setup_code, "username": "alice", "password": PW},
                       headers={"X-Forwarded-Proto": "https"})
        assert r.headers["set-cookie"].startswith("dvrbridge_session=") and "Secure" not in r.headers["set-cookie"]


async def test_secure_session_cookie_logs_in_and_out(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path, public_url="https://gw", trusted_proxies="127.0.0.1") as c:
        await setup_admin(c)
        assert "__Host-dvrbridge_session" in c.cookies and (await c.get("/api/me")).status_code == 200
        await post(c, "/api/auth/logout")
        assert "__Host-dvrbridge_session" not in c.cookies and (await c.get("/api/me")).status_code == 401


def test_throttle_counts_ipv6_per_64_and_stays_bounded():
    assert ip_key("2001:db8:1:2::1") == ip_key("2001:db8:1:2:ffff::9") != ip_key("2001:db8:1:3::1")
    assert ip_key("::ffff:192.0.2.1") == ip_key("192.0.2.1")
    now = [0.0]
    t = LoginThrottle(clock=lambda: now[0])
    for i in range(10):
        t.failure(f"2001:db8:1:2::{i + 1}", f"user{i}")
    assert t.retry_after("2001:db8:1:2::abcd", "someone-else") > 0
    now[0] += 16 * 60
    t.failure("192.0.2.9", "x")                                      # any later call prunes what has expired
    assert not t._blocked


# ---- M5: header mode -----------------------------------------------------------------------------
async def test_header_mode_refuses_unusable_usernames(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path, auth_mode="header", header_secret="s" * 32, local_login=False) as c:
        h = {"X-DVR-Bridge-Proxy-Secret": "s" * 32}
        assert (await c.get("/api/me", headers={**h, "Remote-User": "pat"})).status_code == 200
        assert (await c.get("/api/me", headers={**h, "Remote-User": "p" * 65})).status_code == 401
        assert c.auth_service.store.count_users() == 1


# ---- L1: two-factor enrolment --------------------------------------------------------------------
async def test_two_factor_secret_is_chosen_by_the_server_and_needs_the_password(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        await setup_admin(c)
        step = int(time.time() // 30)
        evil = totp.new_secret()
        r = await post(c, "/api/account/totp/confirm", {"secret": evil, "code": totp.code_at(evil, step)})
        assert r.status_code == 400 and r.json()["error"] == "not_started"
        assert (await post(c, "/api/account/totp/begin", {"password": "not the password"})).status_code == 403
        secret = await enrol_totp(c)
        # Replacing it needs the password and a code from the current authenticator.
        assert (await post(c, "/api/account/totp/begin", {"password": PW})).status_code == 403
        r = await post(c, "/api/account/totp/confirm", {"secret": evil, "code": totp.code_at(evil, step)})
        assert r.status_code == 400
        assert c.auth_service.store.totp(c.auth_service.store.find_user("alice").id)[0] == secret
        r = await post(c, "/api/account/totp/begin", {"password": PW, "code": totp.code_at(secret, step + 1)})
        assert r.status_code == 200 and r.json()["secret"] != secret


def test_short_totp_secrets_are_refused():
    with pytest.raises(ValueError):
        totp.code_at("AA", 1)


# ---- L2: the saved DVR password stays with its DVR --------------------------------------------------
async def test_changing_the_dvr_address_needs_the_password_again(fake_dvr, tmp_path):
    (tmp_path / "config.yaml").write_text("dvr:\n  host: 192.0.2.10\n  password: saved-pw\n")
    async with settings_client(fake_dvr, base_env(tmp_path)) as c:
        r = await put(c, {"dvr.host": "203.0.113.5"})
        assert r.status_code == 400 and "password" in r.json()["detail"]
        assert (await put(c, {"dvr.media_port": 9001})).status_code == 400
        assert (await put(c, {"playback.max_concurrent": 3})).status_code == 200      # unrelated changes are unaffected
        assert (await put(c, {"dvr.host": "203.0.113.5", "dvr.password": "new-dvr-pw"})).status_code == 200


# ---- L3: live-view websocket ---------------------------------------------------------------------
def test_only_the_mse_request_reaches_go2rtc():
    assert json.loads(mse_request('{"type":"mse","value":"avc1.640029,hvc1.1.6.L153.B0"}')) == {
        "type": "mse", "value": "avc1.640029,hvc1.1.6.L153.B0"}
    for text in ('{"type":"webrtc/offer","value":"v=0"}', '{"type":"hls","value":""}', '{"type":"mp4","value":""}',
                 '{"type":"mse","value":"a b"}', '{"type":"mse","value":["x"]}', "[]", "not json", None, "",
                 '{"type":"mse","value":"' + "a" * 500 + '"}'):
        assert mse_request(text) is None, text


def test_go2rtc_error_text_is_not_shown_to_viewers():
    ok = '{"type":"mse","value":"video/mp4; codecs=\\"avc1.640029\\""}'
    assert public_go2rtc_text(ok) == ok
    leaked = public_go2rtc_text('{"type":"error","value":"mse: streams: dial tcp 192.0.2.10:554: refused"}')
    assert "192.168" not in leaked and json.loads(leaked) == {"type": "error", "value": "stream error"}


# ---- L4: password guessing with a session ----------------------------------------------------------
async def test_wrong_current_passwords_are_throttled_per_account(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        await setup_admin(c)
        codes = [(await post(c, "/api/account/password", {"current": f"guess-number-{i}", "new": "whatever-long-1"})).status_code
                 for i in range(7)]
        assert codes == [403] * 5 + [429] * 2
        assert (await post(c, "/api/account/totp/disable", {"password": PW})).status_code == 429    # same lock


# ---- L5: activity log ----------------------------------------------------------------------------
async def test_anonymous_oidc_callbacks_leave_no_audit_rows(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path, **OIDC) as c:
        before = len(c.auth_service.store.audit_log(1000))
        for _ in range(20):
            r = await c.get("/auth/oidc/callback?error=x&error_description=" + "A" * 300)
            assert r.status_code == 302 and r.headers["location"].startswith("/login?error=")
        assert len(c.auth_service.store.audit_log(1000)) == before


def test_audit_log_is_capped(tmp_path):
    store = Store(":memory:")
    for i in range(30):
        store.audit("x", "u", str(i))
    store.trim_audit(keep=10)
    rows = store.audit_log(1000)
    assert len(rows) == 10 and rows[0]["detail"] == "29" and rows[-1]["detail"] == "20"
    store.close()


# ---- L6: browser hardening -----------------------------------------------------------------------
async def test_every_response_carries_the_hardening_headers(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path) as c:
        for path in ("/login", "/static/mse.js", "/api/auth/config", "/api/channels", "/nope"):
            h = (await c.get(path, headers={"Host": "cams.example.com"})).headers
            csp = h["content-security-policy"]
            assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "wss://cams.example.com" in csp
            assert "script-src" not in csp                         # scripts fall back to default-src 'self'
            assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
            assert h["referrer-policy"] == "same-origin"
        assert (await c.get("/api/auth/config")).headers["cache-control"] == "no-store"
        r = await c.get("/login", headers={"Host": "evil.example\"; script-src *"})
        assert "evil" not in r.headers["content-security-policy"]


def test_pages_have_no_inline_scripts():
    from dvrbridge.main import STATIC
    for page in STATIC.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        assert "<script>" not in text and " onclick=" not in text and "javascript:" not in text, page.name


# ---- L8: a DVR that misbehaves -------------------------------------------------------------------
async def test_the_status_poller_survives_a_reply_that_is_not_an_object(fake_dvr, make_client):
    fake_dvr.responses["/API/SystemInfo/Record/Get"] = ["not", "an", "object"]
    m = Monitor(make_client(), interval=0.01)
    await m.start()
    await asyncio.sleep(0.2)
    assert not m._task.done() and m.up is False
    fake_dvr.responses["/API/SystemInfo/Record/Get"] = {"result": "success", "data": {}}
    await asyncio.sleep(0.2)
    assert m.up is True
    await m.stop()


async def test_an_oversized_dvr_reply_is_refused(fake_dvr, make_client, monkeypatch):
    from dvrbridge.dvr import DvrError
    monkeypatch.setattr("dvrbridge.dvr.client.MAX_RESPONSE", 2000)
    fake_dvr.responses["/API/Event/Check"] = {"result": "success", "data": {"pad": "A" * 5000}}
    with pytest.raises(DvrError):
        await make_client().event_check()


# ---- L9: lock-out guard --------------------------------------------------------------------------
async def test_switching_to_local_logins_needs_a_local_admin(fake_dvr, tmp_path):
    env = {"DVRBRIDGE_DATA_DIR": str(tmp_path), "DVRBRIDGE_DVR_HOST": "dvr.test", "DVRBRIDGE_DVR_PASSWORD": "s3cret-pw",
           "DVRBRIDGE_SERVER_SECRET_KEY": "k" * 32}
    (tmp_path / "config.yaml").write_text(
        "auth:\n  mode: header\n  trusted_proxies: 172.18.0.2\n  header_admin_group: adm\n  local_login: false\n")
    s = load(env)
    app = create_app(s, dvr=DvrClient("dvr.test", "tester", "s3cret-pw", transport=fake_dvr.transport()),
                     go2rtc=StubG2R(), auth=AuthService(s), env=env)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("172.18.0.2", 1)), base_url="http://gw") as c:
            h = {"Remote-User": "pat", "Remote-Groups": "adm"}
            r = await c.put("/api/settings/system", content=json.dumps({"updates": {"auth.mode": "local"}}), headers={**JSON, **h})
            assert r.status_code == 400 and "local admin" in r.json()["detail"]
    assert load(env).auth_mode == "header"


# ---- Info ----------------------------------------------------------------------------------------
def test_frigate_event_id_has_no_trailing_newline():
    assert EVENT_ID.fullmatch("1700000000.123456-abc123")
    assert not EVENT_ID.fullmatch("1700000000.123456-abc123\n")


async def test_playback_of_a_channel_the_dvr_does_not_have(fake_dvr, tmp_path):
    async with app_client(fake_dvr, tmp_path, channels=4) as c:
        await setup_admin(c)
        r = await c.get("/api/playback", params={"channel": 9, "start": "2026-11-22T10:00:00", "end": "2026-11-22T10:01:00"})
        assert r.status_code == 404 and r.json()["error"] == "no_such_channel"

import asyncio
import json
import os
import stat
from contextlib import asynccontextmanager

import httpx
import pytest
import yaml

from dvrbridge import connection_test
from dvrbridge.config import ConfigError, load, save
from dvrbridge.dvr import DvrClient
from dvrbridge.main import create_app
from dvrbridge.server import ReloadingServer
from tests.fake_media import PASSWORD, USER, FakeMediaDvr

JSON = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
HTML = {"Accept": "text/html"}


class StubG2R:
    running = True

    async def start(self): ...
    async def stop(self): ...


def base_env(tmp_path, **extra):
    return {"DVRBRIDGE_DATA_DIR": str(tmp_path), "DVRBRIDGE_AUTH_MODE": "none", **extra}


@asynccontextmanager
async def client(fake_dvr, env, reloads=None):
    s = load(env)
    dvr = DvrClient(s.dvr_host or "x.invalid", s.dvr_user, s.dvr_password, transport=fake_dvr.transport(),
                    min_relogin_interval=0)
    app = create_app(s, dvr=dvr, go2rtc=StubG2R(), env=env,
                     request_reload=(lambda: reloads.append(1)) if reloads is not None else None)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            yield c


async def put(c, updates):
    return await c.put("/api/settings/system", content=json.dumps({"updates": updates}), headers=JSON)


# ---- unconfigured start / wizard ----------------------------------------------------------------
async def test_starts_without_a_dvr_and_sends_admins_to_the_wizard(fake_dvr, tmp_path):
    async with client(fake_dvr, base_env(tmp_path)) as c:
        ch = (await c.get("/api/channels")).json()
        assert ch["configured"] is False and ch["channels"] == []
        r = await c.get("/", headers=HTML)
        assert r.status_code == 302 and r.headers["location"] == "/settings/system?wizard=1"
        assert (await c.get("/recordings", headers=HTML)).status_code == 302
        assert (await c.get("/api/recordings", params={"date": "2026-11-22"})).json()["error"] == "not_configured"
        assert (await c.get("/settings/system", headers=HTML)).status_code == 200
        assert fake_dvr.logins == 0                      # nothing talked to a DVR


async def test_schema_never_contains_secret_values(fake_dvr, tmp_path):
    env = base_env(tmp_path, DVRBRIDGE_DVR_HOST="192.0.2.10", DVRBRIDGE_DVR_PASSWORD=PASSWORD,
                   DVRBRIDGE_MONITORING_METRICS_TOKEN="tok-" + "q" * 30)
    async with client(fake_dvr, env) as c:
        doc = (await c.get("/api/settings/system")).json()
        assert doc["configured"] is True
        text = json.dumps(doc)
        assert PASSWORD not in text and "tok-qqq" not in text
        opts = {o["key"]: o for o in doc["options"]}
        assert opts["dvr.password"]["is_set"] is True and opts["dvr.password"]["value"] is None
        assert opts["dvr.host"]["locked"] and opts["dvr.host"]["env"] == "DVRBRIDGE_DVR_HOST"
        assert "server.secret_key" not in opts
        assert opts["auth.mode"]["choices"] == ["local", "oidc", "header", "none"]


# ---- saving -------------------------------------------------------------------------------------
async def test_save_writes_a_private_file_and_asks_for_a_reload(fake_dvr, tmp_path):
    reloads = []
    async with client(fake_dvr, base_env(tmp_path), reloads) as c:
        r = await put(c, {"dvr.host": "192.0.2.10", "dvr.password": PASSWORD, "playback.max_concurrent": "3",
                          "monitoring.metrics_enabled": False})
        assert r.status_code == 200 and r.json()["reloading"] is True
        await asyncio.sleep(0)
        assert reloads == [1]
    path = tmp_path / "config.yaml"
    doc = yaml.safe_load(path.read_text())
    assert doc["dvr"] == {"host": "192.0.2.10", "password": PASSWORD}
    assert doc["playback"]["max_concurrent"] == 3 and doc["monitoring"]["metrics_enabled"] is False
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_invalid_changes_are_refused_and_nothing_is_written(fake_dvr, tmp_path):
    async with client(fake_dvr, base_env(tmp_path)) as c:
        r = await put(c, {"dvr.host": "192.0.2.10", "dvr.http_port": "99999"})
        assert r.status_code == 400 and any("http_port" in p for p in r.json()["problems"])
        r = await put(c, {"dvr.host": "not a host!", "dvr.password": "x"})
        assert r.status_code == 400 and "dvr.host" in r.json()["detail"]
        r = await put(c, {"server.secret_key": "x" * 40})
        assert r.status_code == 400
        r = await put(c, {"auth.mode": "sometimes"})
        assert r.status_code == 400
    assert not (tmp_path / "config.yaml").exists()


async def test_environment_settings_are_locked(fake_dvr, tmp_path):
    async with client(fake_dvr, base_env(tmp_path, DVRBRIDGE_PLAYBACK_MAX_CONCURRENT="2")) as c:
        r = await put(c, {"playback.max_concurrent": "4"})
        assert r.status_code == 400 and "DVRBRIDGE_PLAYBACK_MAX_CONCURRENT" in r.json()["detail"]


async def test_auth_lockout_guards(fake_dvr, tmp_path):
    # a local admin cannot switch to SSO-only before proving SSO works, nor turn logins off
    env = {"DVRBRIDGE_DATA_DIR": str(tmp_path), "DVRBRIDGE_SERVER_PUBLIC_URL": "https://cams.example.com"}
    s = load(env)
    dvr = DvrClient("x.invalid", "u", "p", transport=fake_dvr.transport())
    app = create_app(s, dvr=dvr, go2rtc=StubG2R(), env=env)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gw") as c:
            code = app.state.auth.setup_code
            await c.post("/api/auth/setup", content=json.dumps({"setup_code": code, "username": "alice",
                                                                "password": "correct horse battery"}), headers=JSON)
            r = await put(c, {"auth.mode": "none"})
            assert r.status_code == 400 and "turns logins off" in r.json()["detail"]
            r = await put(c, {"auth.mode": "oidc", "auth.local_login": False, "auth.oidc_issuer": "https://id.example.com",
                              "auth.oidc_client_id": "c", "auth.oidc_client_secret": "s"})
            assert r.status_code == 400 and "Keep password logins on" in r.json()["detail"]
            r = await put(c, {"auth.mode": "oidc", "auth.oidc_issuer": "https://id.example.com",
                              "auth.oidc_client_id": "c", "auth.oidc_client_secret": "s"})
            assert r.status_code == 200                   # with the break-glass login kept: fine
            assert (await c.get("/api/audit")).json()["events"][0]["action"] == "settings_changed"


def test_save_helper_clears_with_none(tmp_path):
    env = base_env(tmp_path)
    save({"dvr.host": "192.0.2.10", "dvr.password": "pw", "monitoring.metrics_token": "t" * 20}, env)
    s = save({"monitoring.metrics_token": None}, env)
    assert s.metrics_token == "" and "metrics_token" not in (yaml.safe_load((tmp_path / "config.yaml").read_text())
                                                             .get("monitoring") or {})
    with pytest.raises(ConfigError):
        save({"nonsense.key": 1}, env)


# ---- connection test ----------------------------------------------------------------------------
async def ok_probe(host, port):
    return None


async def test_connection_test_all_good(fake_dvr):
    media = FakeMediaDvr()
    port = await media.start()
    try:
        t = connection_test.Target("127.0.0.1", 85, port, 554, USER, PASSWORD)
        res = await connection_test.run(t, probe=ok_probe, http_transport=fake_dvr.transport())
    finally:
        await media.stop()
    assert res["ok"] and res["all_ok"], res
    assert res["dvr"] == {"model": "DVR8-4580V", "firmware": "V8.1.0-20220301", "hardware": "DM-307", "channels": 8}
    assert PASSWORD not in json.dumps(res)


async def test_connection_test_reports_each_failure(fake_dvr):
    async def probe(host, port):
        return "refused (nothing listening on that port)" if port == 9000 else None

    t = connection_test.Target("127.0.0.1", 85, 9000, 554, USER, "wrong password")
    res = await connection_test.run(t, probe=probe, http_transport=fake_dvr.transport())
    by = {c["check"]: c for c in res["checks"]}
    assert not res["ok"]
    assert by["Log in to the web API"]["detail"] == "the DVR refused the username or password"
    assert not by["Recordings (client port): port 9000"]["ok"]
    assert "Log in for recordings" not in by            # not attempted when the port is closed


async def test_connection_test_media_login_refused(fake_dvr):
    media = FakeMediaDvr(login_ack=12)
    port = await media.start()
    try:
        res = await connection_test.run(connection_test.Target("127.0.0.1", 85, port, 554, USER, PASSWORD),
                                        probe=ok_probe, http_transport=fake_dvr.transport())
    finally:
        await media.stop()
    by = {c["check"]: c for c in res["checks"]}
    assert res["ok"] and not res["all_ok"] and not by["Log in for recordings"]["ok"]


async def test_test_endpoint_validates_and_throttles(fake_dvr, tmp_path, monkeypatch):
    calls = []

    async def fake_run(target):
        calls.append(target)
        return {"ok": True, "all_ok": True, "checks": [], "dvr": {}}

    monkeypatch.setattr("dvrbridge.main.run_connection_test", fake_run)
    env = base_env(tmp_path, DVRBRIDGE_DVR_HOST="192.0.2.10", DVRBRIDGE_DVR_PASSWORD="saved-pw")
    async with client(fake_dvr, env) as c:
        post = lambda body: c.post("/api/settings/test-dvr", content=json.dumps(body), headers=JSON)  # noqa: E731
        assert (await post({"host": "bad host!"})).status_code == 400
        assert (await post({"host": "192.0.2.20", "http_port": "0"})).status_code == 400
        assert (await post({"host": "192.0.2.20"})).status_code == 200
        assert calls[-1].password == "saved-pw" and calls[-1].host == "192.0.2.20"   # blank = saved password
        assert (await post({})).status_code == 429                                  # too soon after the last one


# ---- the reloading server ------------------------------------------------------------------------
@asynccontextmanager
async def running(server):
    inbox: asyncio.Queue = asyncio.Queue()
    outbox: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(server({"type": "lifespan"}, inbox.get, outbox.put))
    await inbox.put({"type": "lifespan.startup"})
    msg = await asyncio.wait_for(outbox.get(), 10)
    assert msg["type"] == "lifespan.startup.complete", msg
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server), base_url="http://gw") as c:
            yield c
    finally:
        await inbox.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(outbox.get(), 10)
        await task


def fake_factory(fake_dvr, fail_on_host=None):
    def factory(s, **kw):
        if fail_on_host and s.dvr_host == fail_on_host:
            raise RuntimeError("cannot start with these settings")
        dvr = DvrClient(s.dvr_host or "x.invalid", s.dvr_user, s.dvr_password, transport=fake_dvr.transport(),
                        min_relogin_interval=0)
        return create_app(s, dvr=dvr, go2rtc=StubG2R(), **kw)
    return factory


async def wait_for(c, predicate, seconds=10):
    for _ in range(seconds * 20):
        r = await c.get("/api/channels")
        if r.status_code == 200 and predicate(r.json()):
            return r.json()
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_saving_settings_reloads_the_app_in_place(fake_dvr, tmp_path):
    server = ReloadingServer(base_env(tmp_path), factory=fake_factory(fake_dvr))
    async with running(server) as c:
        assert (await c.get("/api/channels")).json()["configured"] is False
        r = await c.put("/api/settings/system", content=json.dumps(
            {"updates": {"dvr.host": "192.0.2.10", "dvr.username": USER, "dvr.password": PASSWORD}}), headers=JSON)
        assert r.status_code == 200 and r.json()["reloading"]
        doc = await wait_for(c, lambda d: d["configured"] and d["channels"])
        assert len(doc["channels"]) == 8 and server.reloads == 1


async def test_a_reload_that_fails_falls_back_to_the_previous_settings(fake_dvr, tmp_path):
    env = base_env(tmp_path)
    server = ReloadingServer(env, factory=fake_factory(fake_dvr, fail_on_host="192.0.2.66"))
    async with running(server) as c:
        save({"dvr.host": "192.0.2.66", "dvr.password": "pw"}, env)
        assert await server.reload() is False
        r = await c.get("/api/channels")
        assert r.status_code == 200 and r.json()["configured"] is False      # still serving the old settings


async def test_requests_during_a_reload_get_503(fake_dvr, tmp_path):
    server = ReloadingServer(base_env(tmp_path), factory=fake_factory(fake_dvr))
    async with running(server) as c:
        server.app = None                       # what a request sees between stop and start
        r = await c.get("/api/channels")
        assert r.status_code == 503 and r.headers["retry-after"] == "2"

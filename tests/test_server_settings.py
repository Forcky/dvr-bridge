import asyncio
from contextlib import asynccontextmanager

import httpx

from dvrbridge.config import Settings
from dvrbridge.dvr import DvrClient
from dvrbridge.main import create_app


class StubG2R:
    running = True

    async def start(self): ...
    async def stop(self): ...


@asynccontextmanager
async def client_for(fake_dvr, **kw):
    s = Settings(dvr_host="dvr.test", dvr_user="tester", dvr_password="s3cret-pw", secret_key="k" * 32,
                 poll_interval=3600, **kw)
    dvr = DvrClient("dvr.test", "tester", "s3cret-pw", transport=fake_dvr.transport(), min_relogin_interval=0)
    app = create_app(s, dvr=dvr, go2rtc=StubG2R())
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            yield c


async def test_channels_zero_uses_what_the_dvr_reports(fake_dvr):
    async with client_for(fake_dvr) as c:
        assert len((await c.get("/api/channels")).json()["channels"]) == 8


async def test_configured_channel_count_limits_the_ui(fake_dvr):
    async with client_for(fake_dvr, channels=4) as c:
        assert [x["channel"] for x in (await c.get("/api/channels")).json()["channels"]] == [1, 2, 3, 4]
        r = await c.post("/api/settings/channels/5", json={"name": "x"}, headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 404


async def test_metrics_can_be_disabled(fake_dvr):
    async with client_for(fake_dvr, metrics_enabled=False) as c:
        assert (await c.get("/metrics")).status_code == 404


async def test_metrics_token(fake_dvr):
    async with client_for(fake_dvr, metrics_token="t0ken-" + "x" * 20) as c:
        assert (await c.get("/metrics")).status_code == 401
        assert (await c.get("/metrics", headers={"Authorization": "Bearer nope"})).status_code == 401
        ok = await c.get("/metrics", headers={"Authorization": "Bearer t0ken-" + "x" * 20})
        assert ok.status_code == 200 and "dvr_up" in ok.text


async def test_playback_range_limit_follows_settings(fake_dvr):
    async with client_for(fake_dvr, max_range_hours=1) as c:
        r = await c.get("/api/playback", params={"channel": 1, "start": "2026-11-22T10:00:00",
                                                  "end": "2026-11-22T11:30:00"})
        assert r.status_code == 400 and "1 hours" in r.json()["detail"]

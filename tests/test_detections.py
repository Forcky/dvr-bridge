import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from dvrbridge.config import ConfigError, Settings, parse_camera_map
from dvrbridge.dvr import DvrClient
from dvrbridge.frigate import FrigateClient, FrigateError, camera_map, normalise
from dvrbridge.main import create_app

TZ = "Australia/Hobart"
DAY = date(2026, 11, 22)
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def epoch(hms: str, day: date = DAY) -> float:
    h, m, s = map(int, hms.split(":"))
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=ZoneInfo(TZ)).timestamp()


def event(eid, camera, label, start, end=None, score=0.9, snap=True, fp=False):
    return {"id": eid, "camera": camera, "label": label, "start_time": epoch(start) if isinstance(start, str) else start,
            "end_time": (epoch(end) if isinstance(end, str) else end) if end else None,
            "has_snapshot": snap, "false_positive": fp, "data": {"top_score": score}}


class FakeFrigate:
    def __init__(self):
        self.cameras = ["channel_1", "channel_3", "porch", "garage"]
        self.events = [
            event("1795000001.1-aaa111", "channel_1", "person", "10:05:51", "10:06:30", 0.97),
            event("1795000002.2-bbb222", "channel_3", "car", "11:20:00", "11:21:00", 0.88),
            event("1795000003.3-ccc333", "porch", "dog", "12:00:00", None, 0.81),              # ongoing, channel 2
            event("1795000004.4-ddd444", "channel_1", "person", "13:00:00", "13:00:05", fp=True),
            event("1795000005.5-eee555", "garage", "cat", "14:00:00", "14:01:00"),            # unmapped camera
            event("1795000006.6-fff666", "channel_1", "car", epoch("23:59:00", date(2026, 11, 21)), "00:00:30", 0.9),
        ]
        self.requests: list[httpx.Request] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("refused")
        path = request.url.path
        if path == "/api/config":
            return httpx.Response(200, json={"cameras": {c: {} for c in self.cameras}})
        if path == "/api/events":
            cams = set(request.url.params.get("cameras", "").split(","))
            return httpx.Response(200, json=[e for e in self.events if e["camera"] in cams])
        for e in self.events:
            if path == f"/api/events/{e['id']}":
                return httpx.Response(200, json=e)
            if path == f"/api/events/{e['id']}/snapshot.jpg":
                return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404, json={"message": "not found"})


# ---- settings and mapping -------------------------------------------------------------------------
def test_camera_map_setting():
    assert parse_camera_map(" 1=driveway , 12=front_door ") == {1: "driveway", 12: "front_door"}
    assert parse_camera_map("") == {}
    for bad in ("1", "x=cam", "0=cam", "65=cam", "1=bad name", "1=a/b"):
        with pytest.raises(ValueError):
            parse_camera_map(bad)


def test_settings_validation():
    base = dict(dvr_host="dvr.test", dvr_user="u", dvr_password="p", secret_key="k" * 32)
    Settings(**base, frigate_url="http://frigate:5000", frigate_cameras="1=driveway")
    with pytest.raises(ConfigError, match="frigate_url"):
        Settings(**base, frigate_url="frigate:5000")
    with pytest.raises(ConfigError, match="detection.cameras"):
        Settings(**base, frigate_cameras="driveway")


def test_mapping_explicit_then_by_name():
    names = {1: "Driveway 1", 2: "Front door", 3: "Backyard - Outside garage", 4: "Pool"}
    m = camera_map("3=backyard,4=missing_cam", names, ["driveway_1", "front_door", "backyard", "pool"])
    assert m == {1: "driveway_1", 2: "front_door", 3: "backyard"}   # 4: explicit name unknown to Frigate is dropped
    assert normalise("Backyard - Outside garage") == "backyard_outside_garage"
    # a camera claimed explicitly is not also given to another channel by name
    assert camera_map("1=pool", {1: "x", 4: "Pool"}, ["pool"]) == {1: "pool"}


# ---- client -----------------------------------------------------------------------------------------
async def test_client_converts_times_and_filters():
    fake = FakeFrigate()
    fc = FrigateClient("http://frigate:5000", TZ, transport=httpx.MockTransport(fake.handler))
    found, truncated = await fc.detections(DAY, {1: "channel_1", 2: "porch", 3: "channel_3"})
    await fc.close()
    assert not truncated
    got = [(d.channel, d.label, d.start.strftime("%Y-%m-%d %H:%M:%S"), d.end and d.end.strftime("%H:%M:%S")) for d in found]
    assert got == [
        (1, "car", "2026-11-21 23:59:00", "00:00:30"),       # overlaps midnight: kept, with its real start
        (1, "person", "2026-11-22 10:05:51", "10:06:30"),
        (3, "car", "2026-11-22 11:20:00", "11:21:00"),
        (2, "dog", "2026-11-22 12:00:00", None),              # still being tracked
    ]
    q = fake.requests[-1].url.params
    assert q["cameras"] == "channel_1,channel_3,porch" and int(q["before"]) - int(q["after"]) == 25 * 3600


async def test_client_errors():
    fake = FakeFrigate()
    fake.down = True
    fc = FrigateClient("http://frigate:5000", TZ, transport=httpx.MockTransport(fake.handler))
    with pytest.raises(FrigateError, match="did not answer"):
        await fc.cameras()
    assert fc.up is False
    with pytest.raises(FrigateError, match="bad event id"):
        await fc.snapshot("../../api/config", {"channel_1"})
    await fc.close()


# ---- endpoints ----------------------------------------------------------------------------------
class StubG2R:
    running = True

    async def start(self): ...
    async def stop(self): ...


async def client_for(fake_dvr, fake, **kw):
    s = Settings(auth_mode="none", dvr_host="dvr.test", dvr_user="tester", dvr_password="s3cret-pw", secret_key="k" * 32,
                 poll_interval=3600, tz=TZ, frigate_url="http://frigate:5000" if fake else "", **kw)
    dvr = DvrClient("dvr.test", "tester", "s3cret-pw", transport=fake_dvr.transport(), min_relogin_interval=0)
    fc = FrigateClient("http://frigate:5000", TZ, transport=httpx.MockTransport(fake.handler)) if fake else None
    return create_app(s, dvr=dvr, go2rtc=StubG2R(), frigate=fc)


async def run(app, fn):
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            return await fn(c)


async def test_detections_off_without_frigate(fake_dvr):
    app = await client_for(fake_dvr, None)
    r = await run(app, lambda c: c.get("/api/detections", params={"date": "2026-11-22"}))
    assert r.json() == {"enabled": False}


async def test_detections_grouped_by_channel(fake_dvr):
    fake = FakeFrigate()
    app = await client_for(fake_dvr, fake, frigate_cameras="2=porch")

    async def go(c):
        doc = (await c.get("/api/detections", params={"date": "2026-11-22"})).json()
        n = len(fake.requests)
        again = (await c.get("/api/detections", params={"date": "2026-11-22"})).json()
        metrics = (await c.get("/metrics")).text
        return doc, again, n, metrics

    doc, again, n, metrics = await run(app, go)
    assert doc["enabled"] and doc["cameras"] == {"1": "channel_1", "2": "porch", "3": "channel_3"}
    assert [d["label"] for d in doc["channels"]["1"]] == ["car", "person"]
    assert doc["channels"]["1"][1] == {"id": "1795000001.1-aaa111", "label": "person", "score": 0.97,
                                       "start": "2026-11-22T10:05:51", "end": "2026-11-22T10:06:30", "has_snapshot": True}
    assert doc["channels"]["2"][0]["end"] is None and "garage" not in str(doc)
    assert again == doc and len(fake.requests) == n          # cached: no second round-trip to Frigate
    assert "dvrbridge_frigate_up 1" in metrics


async def test_detections_when_frigate_is_down(fake_dvr):
    fake = FakeFrigate()
    fake.down = True
    app = await client_for(fake_dvr, fake)
    r = await run(app, lambda c: c.get("/api/detections", params={"date": "2026-11-22"}))
    assert r.status_code == 502 and r.json()["error"] == "frigate_unavailable"


async def test_snapshot_proxy(fake_dvr):
    fake = FakeFrigate()
    app = await client_for(fake_dvr, fake)

    async def go(c):
        ok = await c.get("/api/detections/1795000001.1-aaa111/snapshot.jpg")
        bad = await c.get("/api/detections/nope/snapshot.jpg")
        other = await c.get("/api/detections/1795000005.5-eee555/snapshot.jpg")   # unmapped camera
        missing = await c.get("/api/detections/1795000099.9-zzz999/snapshot.jpg")
        return ok, bad, other, missing

    ok, bad, other, missing = await run(app, go)
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg" and ok.content == JPEG
    assert "private" in ok.headers["cache-control"]
    assert bad.status_code == 400 and other.status_code == 404 and missing.status_code == 404
    snap_req = [r for r in fake.requests if r.url.path.endswith("snapshot.jpg")][0]
    assert snap_req.url.params["bounding_box"] == "1"


async def test_snapshot_must_be_a_jpeg(fake_dvr):
    fake = FakeFrigate()
    orig = fake.handler

    def html_instead(request):
        if request.url.path.endswith("snapshot.jpg"):
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return orig(request)

    fake.handler = html_instead
    app = await client_for(fake_dvr, fake)
    r = await run(app, lambda c: c.get("/api/detections/1795000001.1-aaa111/snapshot.jpg"))
    assert r.status_code == 404 and "JPEG" in r.json()["detail"]

import asyncio
import shutil
import subprocess
from datetime import datetime

import httpx
import pytest

from dvrbridge.config import Settings
from dvrbridge.dvr import DvrClient
from dvrbridge.main import create_app
from dvrbridge.playback import NotImplementedPlayback, PlaybackNotImplemented, PlaybackProvider, remux_to_fmp4


class StubG2R:
    running = True

    async def start(self): ...
    async def stop(self): ...


@pytest.fixture
async def client(fake_dvr):
    s = Settings(auth_mode="none", dvr_host="dvr.test", dvr_user="tester", dvr_password="s3cret-pw", secret_key="k" * 32, poll_interval=3600)
    dvr = DvrClient("dvr.test", "tester", "s3cret-pw", transport=fake_dvr.transport(), min_relogin_interval=0)
    app = create_app(s, dvr=dvr, go2rtc=StubG2R())
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            yield c


async def test_healthz_has_no_dvr_details(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.text.strip() == "ok"


async def test_metrics_endpoint(client):
    await asyncio.sleep(0.2)  # let the monitor's first poll finish
    r = await client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "dvr_up 1" in r.text and "s3cret-pw" not in r.text


async def test_recordings_api(client, fake_dvr):
    r = await client.get("/api/recordings", params={"date": "2026-11-22"})
    assert r.status_code == 200
    doc = r.json()
    assert len(doc["channels"]) == 8 and doc["channels"][0]["name"] == "Channel 1"
    ch1 = doc["channels"][0]["clips"]
    assert [c["start"] for c in ch1][0] == "2026-11-22T14:04:47" and ch1[0]["duration_s"] == 36
    n = len(fake_dvr.calls)
    await client.get("/api/recordings", params={"date": "2026-11-22"})
    assert len(fake_dvr.calls) == n  # cached: light on the DVR


async def test_recordings_bad_date(client):
    assert (await client.get("/api/recordings", params={"date": "nope"})).status_code == 422


async def test_pages_served(client):
    for p in ("/", "/recordings", "/camera/3", "/static/mse.js"):
        assert (await client.get(p)).status_code == 200


async def test_provider_protocol():
    p = NotImplementedPlayback()
    assert isinstance(p, PlaybackProvider)
    with pytest.raises(PlaybackNotImplemented):
        await p.open_session(1, datetime(2026, 1, 1), datetime(2026, 1, 1, 0, 1))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_remux_h264_to_fragmented_mp4():
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15", "-t", "2",
         "-c:v", "libx264", "-f", "h264", "pipe:1"], capture_output=True, check=True).stdout
    assert raw.startswith(b"\x00\x00\x00\x01")

    async def source():
        for i in range(0, len(raw), 4096):
            yield raw[i:i + 4096]

    out = b"".join([c async for c in remux_to_fmp4(source())])
    assert out[4:8] == b"ftyp" and b"moov" in out and b"moof" in out

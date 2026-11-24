import asyncio
import re
import shutil
import subprocess
from datetime import datetime

import httpx
import pytest
import uvicorn

from dvrbridge.config import Settings
from dvrbridge.dvr import DvrClient
from dvrbridge.dvr9000 import Dvr9000Playback, MainSession
from dvrbridge.main import create_app
from dvrbridge.playback import NotImplementedPlayback, PlaybackStats
from tests.fake_media import FakeMediaDvr

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
Q = {"channel": 1, "start": "2026-11-22T14:04:47", "end": "2026-11-22T14:05:23"}


def real_units(seconds=3, fps=15) -> list[bytes]:
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate={fps}", "-t", str(seconds),
         "-c:v", "libx264", "-x264-params", "aud=1:keyint=15:bframes=0", "-f", "h264", "pipe:1"],
        capture_output=True, check=True).stdout
    parts = raw.split(b"\0\0\0\1\x09")
    return [(b"\0\0\0\1\x09" + p) for p in parts[1:]]


def ffmpeg_stats(data: bytes) -> tuple[int, float]:
    """(frame count, pts of the last frame in seconds) as ffmpeg decodes the MP4."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", "pipe:0", "-vf", "showinfo", "-f", "null", "-"],
                         input=data, capture_output=True).stderr.decode()
    pts = re.findall(r"n:\s*(\d+) pts:\s*-?\d+ pts_time:([\d.]+)", err)
    return int(pts[-1][0]) + 1, float(pts[-1][1])


class StubG2R:
    running = True

    async def start(self): ...
    async def stop(self): ...


async def wait_for(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    t = loop.time()
    while not cond():
        if loop.time() - t > timeout:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.02)


class Server:
    """The real app served by uvicorn on a local port, so streaming and disconnects are real."""

    def __init__(self, fake_dvr, media_port=None, playback=None, max_concurrent=2):
        s = Settings(auth_mode="none", dvr_host="dvr.test", dvr_user="tester", dvr_password="s3cret-pw", secret_key="k" * 32,
                     poll_interval=3600)
        dvr = DvrClient("dvr.test", "tester", "s3cret-pw", transport=fake_dvr.transport(), min_relogin_interval=0)
        self.media = MainSession("127.0.0.1", media_port or 1, "tester", "s3cret-pw", keepalive=0.2,
                                 min_login_interval=0, timeout=2.0)
        self.provider = playback or Dvr9000Playback(self.media, max_concurrent=max_concurrent,
                                                    stats=PlaybackStats(), timeout=2.0, stall_timeout=0.5)
        self.app = create_app(s, dvr=dvr, go2rtc=StubG2R(), playback=self.provider, media=self.media)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning"))

    async def __aenter__(self):
        self.task = asyncio.create_task(self.server.serve())
        await wait_for(lambda: self.server.started)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20)
        return self

    async def __aexit__(self, *a):
        await self.client.aclose()
        self.server.should_exit = True
        await self.task


@pytest.fixture
async def media():
    d = FakeMediaDvr(h264_units=real_units(10), fps=15.0)
    await d.start()
    yield d
    await d.stop()


async def test_playback_streams_fmp4_with_correct_speed(fake_dvr, media):
    async with Server(fake_dvr, media.port) as srv:
        r = await srv.client.get("/api/playback", params=Q)
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        assert "content-disposition" not in r.headers
        assert r.content[4:8] == b"ftyp" and b"moof" in r.content
        frames, secs = ffmpeg_stats(r.content)
        assert frames == 150
        assert 9.8 <= secs <= 10.0, secs  # last frame at 149/15; ffmpeg's default 25 fps would give 5.96
        await wait_for(lambda: media.playback_closed == 1 and srv.provider.stats.active == 0)
        assert srv.provider.stats.started == 1 and srv.provider.stats.bytes_streamed >= len(r.content)
        assert media.opens[0]["channel"] == 0
        m = (await srv.client.get("/metrics")).text
        assert "dvrbridge_playback_active 0" in m and "dvrbridge_playback_started_total 1" in m
        assert "dvrbridge_playback_failed_total 0" in m and "dvrbridge_dvr_media_session_up 1" in m
        assert re.search(r"dvrbridge_playback_bytes_total [1-9]", m) and "s3cret-pw" not in m


async def test_export_is_an_attachment(fake_dvr, media):
    async with Server(fake_dvr, media.port) as srv:
        r = await srv.client.get("/api/export", params={**Q, "channel": 3})
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        assert cd == 'attachment; filename="ch3-20261122-140447-140523.mp4"'
        assert ffmpeg_stats(r.content)[0] == 150


async def test_client_disconnect_releases_dvr_and_ffmpeg(fake_dvr, media):
    media.units = media.units * 100
    async with Server(fake_dvr, media.port) as srv:
        async with srv.client.stream("GET", "/api/playback", params=Q) as r:
            assert r.status_code == 200
            got = 0
            async for chunk in r.aiter_bytes():
                got += len(chunk)
                if got > 2000:
                    break
        # leaving the block closes the connection mid-stream
        await wait_for(lambda: media.playback_closed == 1 and srv.provider.stats.active == 0, timeout=10)
        assert srv.provider.stats.failed == 0


async def test_error_mapping(fake_dvr, media):
    async with Server(fake_dvr, media.port) as srv:
        assert (await srv.client.get("/api/playback", params={**Q, "end": Q["start"]})).status_code == 400
        assert (await srv.client.get("/api/playback", params={**Q, "end": "2026-11-23T14:05:23"})).status_code == 400
        assert (await srv.client.get("/api/playback", params={**Q, "channel": 0})).status_code == 422
        a = await srv.provider.open_session(1, datetime(2026, 11, 22, 14, 0, 0), datetime(2026, 11, 22, 14, 1, 0))
        b = await srv.provider.open_session(2, datetime(2026, 11, 22, 14, 0, 0), datetime(2026, 11, 22, 14, 1, 0))
        r = await srv.client.get("/api/playback", params=Q)
        assert r.status_code == 429 and r.json()["error"] == "busy" and r.headers["retry-after"]
        await a.aclose()
        await b.aclose()
        assert (await srv.client.get("/api/playback/status")).json() == {"active": 0, "max": 2, "last_error": "busy"}
    media.open_ack = 1
    async with Server(fake_dvr, media.port) as srv:
        r = await srv.client.get("/api/playback", params=Q)
        assert r.status_code == 502 and r.json()["error"] == "dvr_refused"
        assert (await srv.client.get("/api/export", params=Q)).status_code == 502


async def test_dvr_unreachable_is_502(fake_dvr):
    async with Server(fake_dvr, media_port=1) as srv:  # nothing listens on port 1
        r = await srv.client.get("/api/playback", params=Q)
        assert r.status_code == 502 and r.json()["error"] == "dvr_unavailable"


async def test_no_footage_is_404(fake_dvr):
    class EmptySession:
        async def frames(self):
            return
            yield

        async def aclose(self): ...

    class Empty(NotImplementedPlayback):
        async def open_session(self, *a, **k):
            return EmptySession()

    async with Server(fake_dvr, playback=Empty()) as srv:
        r = await srv.client.get("/api/playback", params=Q)
        assert r.status_code == 404 and r.json()["error"] == "no_footage"


async def test_not_implemented_provider_is_501(fake_dvr):
    async with Server(fake_dvr, playback=NotImplementedPlayback()) as srv:
        r = await srv.client.get("/api/playback", params=Q)
        assert r.status_code == 501

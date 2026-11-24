import asyncio
import struct
from datetime import date, datetime

import pytest

from dvrbridge.dvr9000 import (
    Dvr9000AuthError,
    Dvr9000Playback,
    MainSession,
    PlaybackBusy,
    PlaybackRefused,
    PlaybackStalled,
)
from dvrbridge.dvr9000.client import pack_time, parse_rsfm
from dvrbridge.playback import NoFootage, PlaybackStats, estimate_fps, prime
from tests.fake_media import PASSWORD, SESSION_ID, USER, FakeMediaDvr, end_marker, packets, rsfm

START, END = datetime(2026, 11, 22, 14, 4, 47), datetime(2026, 11, 22, 14, 5, 23)


async def wait_for(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    t = loop.time()
    while not cond():
        if loop.time() - t > timeout:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.01)


@pytest.fixture
async def dvr():
    d = FakeMediaDvr()
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
async def make(dvr):
    made = []

    def _make(user=USER, password=PASSWORD, max_concurrent=2, **kw):
        sess = MainSession("127.0.0.1", dvr.port, user, password, keepalive=kw.pop("keepalive", 0.05),
                           min_login_interval=kw.pop("min_login_interval", 0.0), timeout=2.0, **kw)
        prov = Dvr9000Playback(sess, max_concurrent=max_concurrent, stats=PlaybackStats(), timeout=2.0,
                               stall_timeout=0.2)
        made.append(sess)
        return sess, prov

    yield _make
    for s in made:
        await s.close()


async def collect(sess):
    out = []
    async for f in sess.frames():
        out.append(f)
    return out


async def test_login_keepalive_and_search(dvr, make):
    sess, _ = make()
    sid = await sess.acquire()
    sess.release()
    assert sid == SESSION_ID and dvr.logins == 1
    await wait_for(lambda: dvr.keepalives >= 2)
    recs = await sess.search_day(0, date(2026, 11, 22))
    assert len(recs) == 2 and recs[0]["size"] == 999 and dvr.logins == 1  # one shared session


async def test_playback_open_reassembly_end_and_cleanup(dvr, make):
    sess, prov = make()
    ps = await prov.open_session(1, START, END)
    o = dvr.opens[0]
    assert o["session"] == SESSION_ID and o["channel"] == 0 and o["mask"] == 0xFFFFFFFF and o["len"] == 0x44
    assert o["begin"] == pack_time(START) and o["end"] == pack_time(END)
    assert prov.stats.active == 1
    frames = await collect(ps)
    await ps.aclose()
    # 12 video frames; the audio-ish frame is skipped; frames are ~3 KB so span three packets
    assert len(frames) == 12 and all(len(f.data) > 3000 for f in frames)
    assert frames[0].data.startswith(b"\0\0\0\1\x65")
    ts = [f.ts_us for f in frames]
    assert estimate_fps(ts) == pytest.approx(15.0, abs=0.01)
    await wait_for(lambda: dvr.playback_closed == 1)
    assert prov.stats.active == 0 and prov.stats.started == 1 and prov.stats.failed == 0
    assert dvr.logins == 1


async def test_client_disconnect_closes_socket(dvr, make):
    dvr.units = dvr.units * 200
    sess, prov = make()
    ps = await prov.open_session(1, START, END)
    it = ps.frames()
    got = [await it.__anext__() for _ in range(3)]
    assert len(got) == 3
    await ps.aclose()
    await it.aclose()
    await wait_for(lambda: dvr.playback_closed == 1)
    assert prov.stats.active == 0


async def test_cancelled_reader_still_cleans_up(dvr, make):
    dvr.hold_after = 2
    sess, prov = make()
    ps = await prov.open_session(1, START, END)

    async def run():
        try:
            async for _ in ps.frames():
                pass
        finally:
            await ps.aclose()

    t = asyncio.create_task(run())
    await asyncio.sleep(0.1)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    await wait_for(lambda: dvr.playback_closed == 1)
    assert prov.stats.active == 0


async def test_open_refused(dvr, make):
    dvr.open_ack = 1
    sess, prov = make()
    with pytest.raises(PlaybackRefused) as e:
        await prov.open_session(1, START, END)
    assert e.value.ack == 1
    await wait_for(lambda: dvr.playback_closed == 1)
    assert prov.stats.active == 0 and prov.stats.failed == 1 and prov.stats.last_error == "dvr_refused"


async def test_concurrency_limit(dvr, make):
    dvr.hold_after = 1
    sess, prov = make(max_concurrent=2)
    a = await prov.open_session(1, START, END)
    b = await prov.open_session(2, START, END)
    with pytest.raises(PlaybackBusy):
        await prov.open_session(3, START, END)
    assert prov.stats.rejected == 1 and prov.stats.active == 2
    await a.aclose()
    c = await prov.open_session(3, START, END)
    await b.aclose()
    await c.aclose()
    assert prov.stats.active == 0
    await wait_for(lambda: dvr.playback_closed == 3)


async def test_stall_resends_data_request(dvr, make):
    dvr.drop_data_requests = 2
    sess, prov = make()
    ps = await prov.open_session(1, START, END)
    frames = await collect(ps)
    await ps.aclose()
    assert len(frames) == 12 and dvr.data_requests >= 3


async def test_stall_gives_up(dvr, make):
    dvr.hold_after = 1
    sess, prov = make()
    ps = await prov.open_session(1, START, END)
    with pytest.raises(PlaybackStalled):
        await collect(ps)
    await ps.aclose()
    assert prov.stats.failed == 1 and prov.stats.active == 0


async def test_bad_login_locks_out(dvr, make):
    sess, prov = make(password="wrong")
    with pytest.raises(Dvr9000AuthError):
        await sess.acquire()
    for _ in range(3):
        with pytest.raises(Dvr9000AuthError):
            await sess.acquire()
    assert dvr.bad_logins == 1  # no login loop: later attempts never reach the DVR
    assert sess._leases == 0


async def test_relogin_after_drop_is_rate_limited(dvr, make):
    sess, _ = make(min_login_interval=0.3)
    await sess.acquire()
    sess.release()
    sess._drop()
    with pytest.raises(Exception):
        await sess.acquire()  # too soon after the first login
    await asyncio.sleep(0.35)
    await sess.acquire()
    sess.release()
    assert dvr.logins == 2


async def test_prime_no_footage():
    class Empty:
        closed = False

        async def frames(self):
            return
            yield

        async def aclose(self):
            self.closed = True

    e = Empty()
    with pytest.raises(NoFootage):
        await prime(e)
    assert e.closed


def test_estimate_fps():
    step = 66_667
    assert estimate_fps([i * step for i in range(60)]) == pytest.approx(15.0, abs=0.01)
    assert estimate_fps([i * 40_000 for i in range(10)]) == 25.0
    assert estimate_fps([]) is None and estimate_fps([5]) is None
    assert estimate_fps([5, 5, 5]) is None            # no time passes
    assert estimate_fps([0, 10_000_000]) is None       # 0.1 fps: not credible


def test_parse_rsfm_skips_non_video():
    assert parse_rsfm(rsfm(7, b"\0\0\0\1abc")).ts_us == 7
    assert parse_rsfm(rsfm(7, b"\x01\x02\x03\x04\x05")) is None
    assert parse_rsfm(b"XXXX" + bytes(60)) is None


def test_packet_helpers_roundtrip():
    f = rsfm(1, b"\0\0\0\1" + bytes(4000))
    ps = packets(f)
    assert len(ps) == 3 and struct.unpack_from("<H", ps[0], 2)[0] == 3
    assert len(end_marker()) == 12

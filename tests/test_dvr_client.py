from datetime import date, datetime

import pytest

from dvrbridge.dvr import DvrAuthError, DvrError
from dvrbridge.dvr.client import build_digest_header, parse_challenge


def test_parse_challenge_and_userhash_header():
    ch = parse_challenge('Digest realm="R",nonce="n1",qop="auth",userhash="true",opaque="o"')
    assert ch["realm"] == "R" and ch["userhash"] == "true" and ch["opaque"] == "o"
    h = build_digest_header("POST", "/x", "bob", "pw", ch, cnonce="c")
    h2 = build_digest_header("POST", "/x", "bob", "pw", ch, cnonce="c", hash_username=True)
    assert 'username="bob"' in h and "userhash" not in h  # the DVR wants the plain name
    assert 'username="bob"' not in h2 and "userhash=true" in h2
    assert 'qop="auth"' in h and 'opaque="o"' in h


async def test_login_sets_session_and_csrf_and_reuses_it(make_client, fake_dvr):
    c = make_client()
    info = await c.system_info()
    assert info.device_type == "DVR8-4580V" and info.channel_count == 8
    await c.disk_info()
    await c.record_state()
    assert fake_dvr.logins == 1  # one login session shared by every call
    assert [p for p, _ in fake_dvr.calls] == [
        "/API/Login/DeviceInfo/Get", "/API/StorageConfig/Disk/Get", "/API/SystemInfo/Record/Get"]
    assert c.last_success_age is not None and c.last_success_age < 5
    await c.close()


async def test_bad_password_raises_and_does_not_loop(make_client, fake_dvr):
    c = make_client(password="wrong")
    with pytest.raises(DvrAuthError):
        await c.system_info()
    with pytest.raises(DvrAuthError):  # locked out locally; no second attempt hits the DVR
        await c.system_info()
    assert fake_dvr.logins == 0
    assert fake_dvr.calls == []
    await c.close()


async def test_relogin_on_401(make_client, fake_dvr):
    c = make_client()
    await c.system_info()
    fake_dvr.invalidate_sessions()
    await c.channel_info()
    assert fake_dvr.logins == 2
    await c.close()


async def test_no_heartbeat_triggers_heartbeat_then_retry(make_client, fake_dvr):
    fake_dvr.require_heartbeat_for_playback = True
    c = make_client()
    clips = await c.search_records([1, 3], date(2026, 11, 22))
    assert fake_dvr.heartbeat_seen and len(clips) == 4
    await c.close()


async def test_background_heartbeat(make_client, fake_dvr):
    import asyncio

    c = make_client(heartbeat_interval=0.05)
    await c.system_info()
    await c.start()
    await asyncio.sleep(0.3)
    assert any(p == "/API/Login/Heartbeat" for p, _ in fake_dvr.calls)
    await c.close()


async def test_search_request_body(make_client, fake_dvr):
    c = make_client()
    await c.search_records([1, 2], date(2026, 11, 22))
    path, body = fake_dvr.calls[-1]
    assert path == "/API/Playback/SearchRecord/Search"
    assert body["version"] == "1.0"
    d = body["data"]
    assert d["channel"] == ["CH1", "CH2"]
    assert d["start_date"] == d["end_date"] == "11/22/2026"
    assert d["record_type"] == 4294967295 and d["stream_mode"] == "Mainstream"
    await c.close()


async def test_refuses_write_endpoints(make_client, fake_dvr):
    c = make_client()
    for p in ["/API/SystemConfig/NTP/Set", "/API/Maintenance/DeviceReboot/Set", "/API/StorageConfig/Disk/Format", "/API/Maintenance/Upgrade/Control"]:
        with pytest.raises(DvrError):
            await c.call(p)
    assert fake_dvr.logins == 0 and fake_dvr.calls == []
    await c.close()


async def test_typed_calls(make_client):
    c = make_client()
    chans = await c.channel_info()
    assert len(chans) == 8 and chans[0].number == 1 and chans[0].name == "Channel 1" and chans[0].online
    disks = await c.disk_info()
    assert disks[0].status == "Full" and disks[0].total_mb == 953869
    rec = await c.record_state()
    assert len(rec) == 8 and all(r.switch_enabled and not r.recording for r in rec)
    ev = await c.event_check()
    assert ev.hdd_alarm == "Ok" and len(ev.channels) == 8 and not any(e.videoloss for e in ev.channels)
    await c.close()


async def test_search_parsing(make_client):
    c = make_client()
    clips = await c.search_records([1, 3], date(2026, 11, 22))
    assert [(x.channel, x.start.time().isoformat(), x.size) for x in clips][:2] == [
        (1, "14:04:47", 19419136), (1, "14:05:24", 18350080)]
    assert clips[0].duration_s == 36
    over = [x for x in clips if x.channel == 3]
    assert len(over) == 1  # the malformed entry was skipped
    assert over[0].end == datetime(2026, 11, 23, 0, 0, 12) and over[0].duration_s == 32
    await c.close()

"""Renaming cameras: the DVR client's read-modify-write, the write allow-list, and the HTTP API
(validation, CSRF guard, error mapping) against the fake DVR."""
import asyncio
import copy

import httpx
import pytest

from dvrbridge.channels import InvalidName, csrf_problem, validate_name
from dvrbridge.config import Settings
from dvrbridge.dvr import DvrApiError, DvrClient, DvrError
from dvrbridge.dvr.client import ALLOWED_PATHS, OSD_GET, OSD_RANGE, OSD_SET, osd_names, writable_osd
from dvrbridge.main import create_app

ORIGIN = {"Origin": "http://gw"}


# ---- allow-list ---------------------------------------------------------------------------
def test_allowlist_is_exactly_the_known_endpoints():
    assert ALLOWED_PATHS == frozenset({
        "/API/Web/Login", "/API/Login/Heartbeat", "/API/Login/DeviceInfo/Get", "/API/Login/ChannelInfo/Get",
        "/API/StorageConfig/Disk/Get", "/API/SystemInfo/Record/Get", "/API/Playback/SearchRecord/Search",
        "/API/Event/Check", "/API/ChannelConfig/OSD/Get", "/API/ChannelConfig/OSD/Range",
        "/API/ChannelConfig/OSD/Set"})


def test_only_one_write_endpoint_is_callable():
    writes = {p for p in ALLOWED_PATHS if not p.endswith(("/Get", "/Range", "/Search", "/Check", "/Login", "/Heartbeat"))}
    assert writes == {OSD_SET}


async def test_other_writes_still_refused_and_never_sent(make_client, fake_dvr):
    c = make_client()
    for p in ["/API/ChannelConfig/OSD/Format", "/API/ChannelConfig/Anything/Set", "/API/SystemConfig/NTP/Set",
              "/API/Maintenance/DeviceReboot/Set", "/API/StorageConfig/Disk/Format", "/API/Maintenance/Upgrade/Control",
              "/API/Maintenance/Reset/Set", "/API/RecordConfig/Set"]:
        with pytest.raises(DvrError):
            await c.call(p)
    assert fake_dvr.logins == 0 and fake_dvr.calls == []
    await c.close()


# ---- client read-modify-write ----------------------------------------------------------------
async def test_rename_is_read_modify_write_and_changes_only_the_name(make_client, fake_dvr):
    before = copy.deepcopy(fake_dvr.osd)
    c = make_client()
    assert await c.set_channel_name(8, "Test 8") == "Test 8"
    assert [p for p, _ in fake_dvr.calls] == [OSD_RANGE, OSD_GET, OSD_SET, OSD_GET]  # read, modify, write, verify
    sent = fake_dvr.osd_sets[0]
    # the object sent is the one read, with exactly one field different
    expect = writable_osd(before, fake_dvr.osd_range)
    expect["channel_info"]["CH8"]["name"]["text"] = "Test 8"
    assert sent == expect
    assert sent["channel_info"]["CH8"]["name"]["pos"] == {"x": 15, "y": 10}  # position, show flag etc. untouched
    for k in (f"CH{i}" for i in range(1, 8)):
        assert sent["channel_info"][k] == before["channel_info"][k]
    # placeholders the DVR's Range does not list are not sent (the DVR answers 400 to them)
    assert not [k for k in sent["channel_info"] if k.startswith("IP_")]
    assert osd_names(fake_dvr.osd)[8] == "Test 8" and osd_names(fake_dvr.osd)[7] == "Channel 7"
    await c.close()


async def test_restore_to_the_original_value_round_trips(make_client, fake_dvr):
    original = copy.deepcopy(fake_dvr.osd["channel_info"]["CH8"])
    c = make_client()
    await c.set_channel_name(8, "Test 8")
    await c.set_channel_name(8, "Channel 8")
    assert fake_dvr.osd["channel_info"]["CH8"] == original
    await c.close()


async def test_read_only_status_field_is_not_sent(make_client, fake_dvr):
    fake_dvr.osd["channel_info"]["CH3"]["status"] = "Offline"
    c = make_client()
    await c.set_channel_name(8, "Test 8")
    assert "status" not in fake_dvr.osd_sets[0]["channel_info"]["CH3"]
    await c.close()


async def test_dvr_refusal_surfaces_its_reason(make_client, fake_dvr):
    fake_dvr.osd_set_result = "param_error"
    c = make_client()
    with pytest.raises(DvrApiError) as ei:
        await c.set_channel_name(8, "Test 8")
    assert ei.value.result == "param_error"
    await c.close()


async def test_unknown_channel_sends_no_write(make_client, fake_dvr):
    c = make_client()
    with pytest.raises(DvrApiError) as ei:
        await c.set_channel_name(9, "Nine")
    assert ei.value.result == "unknown_channel" and fake_dvr.osd_sets == []
    await c.close()


async def test_readback_mismatch_is_an_error(make_client, fake_dvr):
    fake_dvr.osd_ignore_set = True
    c = make_client()
    with pytest.raises(DvrApiError) as ei:
        await c.set_channel_name(8, "Test 8")
    assert ei.value.result == "verify_failed"
    await c.close()


async def test_name_max_len_comes_from_range(make_client, fake_dvr):
    c = make_client()
    assert await c.osd_name_max_len(8) == 31
    fake_dvr.osd_range["channel_info"]["items"]["CH8"]["items"]["name"]["items"]["text"]["max_len"] = 12
    assert await c.osd_name_max_len(8) == 12
    assert await c.osd_name_max_len(1) == 31
    await c.close()


# ---- validation and CSRF (pure) -----------------------------------------------------------------
@pytest.mark.parametrize("raw,ok", [
    ("Front door", "Front door"), ("  Back Yard  ", "Back Yard"), ("Garage-2 (east) & side_1.5", "Garage-2 (east) & side_1.5"),
    ("O'Brien", "O'Brien"), ("x" * 31, "x" * 31)])
def test_validate_name_accepts(raw, ok):
    assert validate_name(raw, 31) == ok


@pytest.mark.parametrize("raw", ["", "   ", "x" * 32, "a<b", "<script>", 'say "hi"', "a;b", "a/b", "a\\b", "café", "line\nbreak",
                                 "tab\there", "a%20b", "a`b", "a=b", "‮evil", None, 5, ["a"], {"a": 1}])
def test_validate_name_rejects(raw):
    with pytest.raises(InvalidName):
        validate_name(raw, 31)


def test_validate_name_uses_dvr_limit():
    assert validate_name("x" * 12, 12)
    with pytest.raises(InvalidName):
        validate_name("x" * 13, 12)


JSON = {"content-type": "application/json", "host": "gw"}


@pytest.mark.parametrize("headers,bad", [
    ({**JSON, "origin": "http://gw"}, False),
    ({**JSON, "origin": "https://gw"}, False),  # behind the TLS proxy the scheme differs, the host must match
    ({**JSON, "sec-fetch-site": "same-origin"}, False),
    ({**JSON, "origin": "http://gw", "sec-fetch-site": "same-origin"}, False),
    ({**JSON, "origin": "https://cameras.example", "sec-fetch-site": "same-origin"}, False),  # a proxy rewrote Host
    ({**JSON, "origin": "http://gw; charset=x"}, True),
    ({**JSON}, True),
    ({**JSON, "origin": "http://evil.example"}, True),
    ({**JSON, "origin": "null"}, True),
    ({**JSON, "origin": "http://gw.evil.example"}, True),
    ({**JSON, "origin": "http://gw", "sec-fetch-site": "cross-site"}, True),
    ({**JSON, "origin": "http://gw", "sec-fetch-site": "same-site"}, True),
    ({**JSON, "sec-fetch-site": "none"}, True),
    ({"content-type": "text/plain", "host": "gw", "origin": "http://gw"}, True),
    ({"content-type": "application/x-www-form-urlencoded", "host": "gw", "origin": "http://gw"}, True),
    ({"host": "gw", "origin": "http://gw"}, True),
    ({"content-type": "application/json; charset=utf-8", "host": "gw", "origin": "http://gw"}, False),
])
def test_csrf_problem(headers, bad):
    assert bool(csrf_problem(headers)) is bad


# ---- HTTP API --------------------------------------------------------------------------------
@pytest.fixture
async def api(fake_dvr):
    from tests.test_app_and_playback import StubG2R
    s = Settings(dvr_host="dvr.test", dvr_user="tester", dvr_password="s3cret-pw", secret_key="k" * 32, poll_interval=3600)
    dvr = DvrClient("dvr.test", "tester", "s3cret-pw", transport=fake_dvr.transport(), min_relogin_interval=0)
    app = create_app(s, dvr=dvr, go2rtc=StubG2R())
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            yield c


async def post(api, channel=8, body='{"name": "Test 8"}', headers=None):
    h = {"Content-Type": "application/json", **ORIGIN}
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}
    return await api.post(f"/api/settings/channels/{channel}", content=body, headers=h)


async def test_get_settings_lists_names_and_limit(api):
    r = await api.get("/api/settings/channels")
    assert r.status_code == 200
    j = r.json()
    assert j["max_length"] == 31
    assert [c["channel"] for c in j["channels"]] == list(range(1, 9))
    assert j["channels"][0] == {"channel": 1, "name": "Channel 1", "max_length": 31}


async def test_rename_updates_dvr_channel_list_and_cached_searches(api, fake_dvr):
    r0 = await api.get("/api/recordings", params={"date": "2026-11-22"})
    assert r0.json()["channels"][7]["name"] == "Channel 8"
    r = await post(api)
    assert r.status_code == 200 and r.json() == {"channel": 8, "name": "Test 8"}
    ch = (await api.get("/api/channels")).json()["channels"]
    assert ch[7] == {"channel": 8, "name": "Test 8"} and ch[0]["name"] == "Channel 1"
    # the cached day search was dropped, so recordings carry the new name too
    r1 = await api.get("/api/recordings", params={"date": "2026-11-22"})
    assert r1.json()["channels"][7]["name"] == "Test 8"
    assert (await api.get("/api/settings/channels")).json()["channels"][7]["name"] == "Test 8"
    assert len(fake_dvr.osd_sets) == 1


async def test_rename_trims_and_accepts_max_length(api):
    assert (await post(api, body='{"name": "  Front door  "}')).json()["name"] == "Front door"
    assert (await post(api, body='{"name": "' + "x" * 31 + '"}')).status_code == 200


@pytest.mark.parametrize("body", [
    '{"name": ""}', '{"name": "   "}', '{"name": "' + "x" * 32 + '"}', '{"name": "<b>hi</b>"}', '{"name": "a\\u00e9"}',
    '{"name": "a\\nb"}', '{"name": 5}', '{"name": null}', '{}', '[]', '"Test 8"', 'not json', '', '{"name": ["a"]}'])
async def test_rename_rejects_invalid_names_without_touching_the_dvr(api, fake_dvr, body):
    r = await post(api, body=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"] in ("bad_name", "bad_request")
    assert fake_dvr.osd_sets == []


async def test_rename_unknown_channel(api, fake_dvr):
    for ch in (0, 9, 99):
        assert (await post(api, channel=ch)).status_code == 404
    assert fake_dvr.osd_sets == []


async def test_dvr_refusal_is_reported_clearly(api, fake_dvr):
    fake_dvr.osd_set_result = "param_error"
    r = await post(api)
    assert r.status_code == 502
    j = r.json()
    assert j["error"] == "dvr_refused" and "param_error" in j["detail"]
    assert (await api.get("/api/channels")).json()["channels"][7]["name"] == "Channel 8"  # unchanged


@pytest.mark.parametrize("headers", [
    {"Content-Type": "text/plain"},
    {"Content-Type": "application/x-www-form-urlencoded"},
    {"Content-Type": None},
])
async def test_csrf_rejects_wrong_content_type(api, fake_dvr, headers):
    r = await post(api, headers=headers)
    assert r.status_code == 403 and fake_dvr.osd_sets == []


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example"},
    {"Origin": "null"},
    {"Origin": "http://gw.evil.example"},
    {"Origin": None},                                              # no Origin and no Sec-Fetch-Site
    {"Origin": "http://gw", "Sec-Fetch-Site": "cross-site"},
    {"Origin": None, "Sec-Fetch-Site": "same-site"},
    {"Origin": None, "Sec-Fetch-Site": "none"},
])
async def test_csrf_rejects_cross_origin_and_missing_origin(api, fake_dvr, headers):
    r = await post(api, headers=headers)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert fake_dvr.osd_sets == []


async def test_csrf_accepts_sec_fetch_site_same_origin_without_origin(api, fake_dvr):
    r = await post(api, headers={"Origin": None, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and len(fake_dvr.osd_sets) == 1


async def test_csrf_runs_before_validation(api, fake_dvr):
    r = await post(api, body="garbage", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


async def test_get_is_read_only(api, fake_dvr):
    await api.get("/api/settings/channels")
    assert fake_dvr.osd_sets == []
    assert (await api.put("/api/settings/channels/8", content="{}")).status_code == 405


async def test_settings_page_is_served(api):
    r = await api.get("/settings")
    assert r.status_code == 200 and "Camera names" in r.text
    assert (await api.get("/static/settings.js")).status_code == 200


def test_channel_list_reads_channel_name_not_alias():
    from dvrbridge.dvr.models import ChannelInfo
    doc = {"channel_param": {"items": [
        {"channel": "CH1", "channel_name": "Gate", "channel_alias": "", "connect_status": "Online"},
        {"channel": "CH2", "channel_name": "", "channel_alias": "Alias only", "connect_status": "Online"},
        {"channel": "CH3", "channel_name": "Real", "channel_alias": "Stale alias", "connect_status": "Offline"}]}}
    assert [c.name for c in ChannelInfo.parse_list(doc)] == ["Gate", "Alias only", "Real"]

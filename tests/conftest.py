import hashlib
import json
import re
import secrets
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

FIX = Path(__file__).parent / "fixtures"
USER, PASSWORD = "tester", "s3cret-pw"
REALM = "FakeDVR"


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def osd_range() -> dict:
    """Shape of the real DVR's /API/ChannelConfig/OSD/Range (name.text: string, min_len 1, max_len 31)."""
    def ch() -> dict:
        xy = {"type": "object", "items": {"x": {"type": "int32", "min": 0, "max": 704}, "y": {"type": "int32", "min": 0, "max": 576}}}
        return {"type": "object", "items": {
            "status": {"description": "Only offline channel has this variable.", "type": "string", "mode": "r", "items": ["Offline", "Online"]},
            "channel_enable": {"type": "bool"},
            "name": {"type": "object", "items": {"show": {"type": "bool"}, "text": {"type": "string", "min_len": 1, "max_len": 31}, "pos": xy}},
            "datetime": {"type": "object", "items": {"show": {"type": "bool"}, "pos": xy}},
            "covert": {"type": "bool"},
            "camera_type": {"type": "string", "items": ["AUTO", "TVI", "AHD"]},
            "copy_ch": {"type": "string", "items": ["digit", "analog", "wifi"]}}}
    return {"support_copy": True, "channel_max": 8,
            "channel_info": {"type": "object", "items": {f"CH{i}": ch() for i in range(1, 9)}}}


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class FakeDvr:
    """Stand-in for the DVR: digest login with userhash, session cookie + csrf header."""

    def __init__(self, user=USER, password=PASSWORD):
        self.user, self.password = user, password
        self.nonce = secrets.token_hex(8)
        self.sessions: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.logins = 0
        self.require_heartbeat_for_playback = False
        self.heartbeat_seen = False
        self.responses = {
            "/API/Login/DeviceInfo/Get": fixture("login_deviceinfo.json"),
            "/API/Login/ChannelInfo/Get": fixture("login_channelinfo.json"),
            "/API/StorageConfig/Disk/Get": fixture("storageconfig_disk.json"),
            "/API/SystemInfo/Record/Get": fixture("systeminfo_record.json"),
            "/API/Event/Check": fixture("event_check.json"),
            "/API/Playback/SearchRecord/Search": fixture("search_day.json"),
            "/API/Login/Heartbeat": {"result": "success"},
        }
        # OSD: Get returns the real device's shape (incl. the IP_CH placeholders Range does not list);
        # Set is validated like the real DVR (HTTP 400 for entries Range does not describe) and stateful.
        self.osd = fixture("osd_get.json")["data"]
        self.osd_range = osd_range()
        self.osd_set_result = "success"  # set to e.g. "param_error" to simulate a refusal
        self.osd_sets: list[dict] = []
        self.osd_ignore_set = False
        self.rename_changes_channel_info = True
        self.app = Starlette(routes=[Route("/{path:path}", self.handle, methods=["POST"])])

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)

    def invalidate_sessions(self):
        self.sessions.clear()

    def _digest_ok(self, header: str, method: str, path: str) -> bool:
        p = {k: (a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', header)}
        if p.get("nonce") != self.nonce or p.get("uri") != path:
            return False
        if p.get("userhash") or p.get("username") != self.user:
            return False
        ha1 = md5(f"{self.user}:{REALM}:{self.password}")
        ha2 = md5(f"{method}:{path}")
        want = md5(f"{ha1}:{self.nonce}:{p['nc']}:{p['cnonce']}:auth:{ha2}")
        return p.get("response") == want

    async def handle(self, request: Request) -> Response:
        path = "/" + request.path_params["path"]
        body = await request.json()
        assert body.get("version") == "1.0" and "data" in body
        if path == "/API/Web/Login":
            auth = request.headers.get("authorization")
            if not auth:
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": f'Digest realm="{REALM}", qop="auth", nonce="{self.nonce}", '
                        'userhash="true"'
                    },
                )
            if not self._digest_ok(auth, "POST", path):
                return Response(status_code=401, headers={"WWW-Authenticate": f'Digest realm="{REALM}", nonce="{self.nonce}", userhash="true"'})
            self.logins += 1
            sid, csrf = secrets.token_hex(8), secrets.token_hex(8)
            self.sessions[sid] = csrf
            return JSONResponse(
                {"result": "success", "data": {}},
                headers={"Set-Cookie": f"session={sid}; Path=/; HttpOnly", "X-csrftoken": csrf},
            )
        cookie = request.headers.get("cookie", "")
        m = re.search(r"session=([^;]+)", cookie)
        sid = m.group(1) if m else None
        if sid not in self.sessions or request.headers.get("x-csrftoken") != self.sessions[sid]:
            return Response(status_code=401)
        self.calls.append((path, body))
        if path == "/API/Login/Heartbeat":
            self.heartbeat_seen = True
        if path.startswith("/API/Playback") and self.require_heartbeat_for_playback and not self.heartbeat_seen:
            return JSONResponse({"result": "no_heartbeat"})
        if path == "/API/ChannelConfig/OSD/Get":
            return JSONResponse({"result": "success", "data": self.osd})
        if path == "/API/ChannelConfig/OSD/Range":
            return JSONResponse({"result": "success", "data": self.osd_range})
        if path == "/API/ChannelConfig/OSD/Set":
            data = body["data"]
            allowed = set(self.osd_range["channel_info"]["items"])
            if set(data.get("channel_info", {})) - allowed:
                return Response(status_code=400)  # what the real DVR does with IP_CH1 & co
            self.osd_sets.append(data)
            if self.osd_set_result != "success":
                return JSONResponse({"result": self.osd_set_result})
            if self.osd_ignore_set:  # claims success but stores nothing
                return JSONResponse({"result": "success"})
            for k, v in data["channel_info"].items():
                self.osd["channel_info"][k] = v
                if self.rename_changes_channel_info:  # the real DVR's channel_name follows the OSD text
                    for it in self.responses["/API/Login/ChannelInfo/Get"]["data"]["channel_param"]["items"]:
                        if it["channel"] == k:
                            it["channel_name"] = v["name"]["text"]
            return JSONResponse({"result": "success"})
        if path not in self.responses:
            return JSONResponse({"result": "not_support"})
        return JSONResponse(self.responses[path])


@pytest.fixture
def fake_dvr():
    return FakeDvr()


@pytest.fixture
def make_client(fake_dvr):
    from dvrbridge.dvr import DvrClient

    clients = []

    def _make(**kw):
        c = DvrClient("dvr.test", kw.pop("user", USER), kw.pop("password", PASSWORD),
                      transport=fake_dvr.transport(), heartbeat_interval=kw.pop("heartbeat_interval", 3600),
                      min_relogin_interval=kw.pop("min_relogin_interval", 0.0), **kw)
        clients.append(c)
        return c

    yield _make

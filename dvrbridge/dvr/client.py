"""Async client for the RaySharp/Swann DVR HTTP API (port 85).

Login is HTTP Digest with ``userhash=true`` (MD5, qop=auth). The reply carries a
``session`` cookie and an ``X-csrftoken`` header which every later call sends.
A background heartbeat keeps the session alive (Playback calls fail with
``no_heartbeat`` otherwise). One login session is shared by every caller.

Safety: only an allow-list of endpoints can be called. It is read-only except
for exactly one write, ``/API/ChannelConfig/OSD/Set``, used to rename a channel
(read-modify-write of the OSD config, see ``DvrClient.set_channel_name``).
Anything else that could change the device (other Set, Format, Reset, Control,
Upgrade...) is refused here, before a request is built.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import os
import re
import time
from datetime import date
from typing import Any

import httpx

from .models import (
    DVR_DATE_FMT,
    BaseInfo,
    ChannelInfo,
    Clip,
    DeviceInfo,
    DiskInfo,
    EventState,
    ChannelRecordState,
    parse_record_state,
    parse_search,
)

log = logging.getLogger("dvrbridge.dvr")

LOGIN_PATH = "/API/Web/Login"
HEARTBEAT_PATH = "/API/Login/Heartbeat"

OSD_GET = "/API/ChannelConfig/OSD/Get"
OSD_RANGE = "/API/ChannelConfig/OSD/Range"
OSD_SET = "/API/ChannelConfig/OSD/Set"  # the only write endpoint DVR Bridge can call

ALLOWED_PATHS = frozenset(
    {
        LOGIN_PATH,
        HEARTBEAT_PATH,
        "/API/Login/DeviceInfo/Get",
        "/API/SystemInfo/Base/Get",
        "/API/Login/ChannelInfo/Get",
        "/API/StorageConfig/Disk/Get",
        "/API/SystemInfo/Record/Get",
        "/API/Playback/SearchRecord/Search",
        "/API/Event/Check",
        OSD_GET,
        OSD_RANGE,
        OSD_SET,
    }
)

MAX_RESPONSE = 8 * 1024 * 1024  # a day's search for 16+ channels is well under 1 MB
DEFAULT_NAME_MAX_LEN = 31  # what the real DVR's OSD/Range reports; used only if Range omits it


class DvrError(Exception):
    """Base class; messages never contain credentials."""


class DvrUnavailable(DvrError):
    """Network-level failure talking to the DVR."""


class DvrAuthError(DvrError):
    """Login refused."""


class DvrApiError(DvrError):
    def __init__(self, path: str, result: str):
        super().__init__(f"{path}: {result}")
        self.path = path
        self.result = result


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()  # noqa: S324 - the DVR mandates MD5


_PARAM_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s,]+))')


def parse_challenge(header: str) -> dict[str, str]:
    if not header.lower().startswith("digest"):
        raise DvrAuthError("DVR did not offer Digest authentication")
    return {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _PARAM_RE.finditer(header)}


def build_digest_header(
    method: str, uri: str, user: str, password: str, challenge: dict[str, str], *, nc: int = 1, cnonce: str | None = None,
    hash_username: bool = False
) -> str:
    realm = challenge.get("realm", "")
    nonce = challenge["nonce"]
    qop = "auth"
    cnonce = cnonce or os.urandom(8).hex()
    ncv = f"{nc:08x}"
    ha1 = _md5(f"{user}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    response = _md5(f"{ha1}:{nonce}:{ncv}:{cnonce}:{qop}:{ha2}")
    # The DVR advertises userhash="true" but answers 400 to a hashed username;
    # it wants the plain one (verified against the real device, 2026-11-23).
    # Hashing stays available for a firmware that really needs it.
    username = _md5(f"{user}:{realm}") if hash_username else user
    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'cnonce="{cnonce}"',
        f"nc={ncv}",
        f'response="{response}"',
        f'qop="{qop}"',
    ]
    if "opaque" in challenge:
        parts.append(f'opaque="{challenge["opaque"]}"')
    if hash_username:
        parts.append("userhash=true")
    return "Digest " + ",".join(parts)


class DvrClient:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        port: int = 85,
        heartbeat_interval: float = 20.0,
        timeout: float = 10.0,
        min_relogin_interval: float = 30.0,
        auth_lockout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._user = user
        self._password = password
        self._base = f"http://{host}:{port}"
        self._http = httpx.AsyncClient(base_url=self._base, timeout=timeout, transport=transport)
        self._lock = asyncio.Lock()
        self._session: str | None = None
        self._csrf: str | None = None
        self._last_login_attempt = 0.0
        self._auth_failed_at: float | None = None
        self._min_relogin = min_relogin_interval
        self._auth_lockout = auth_lockout
        self._hb_interval = heartbeat_interval
        self._hb_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()  # serialises read-modify-write renames
        self.last_success: float | None = None  # time.monotonic()
        self.login_count = 0

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if self._hb_task is None:
            self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="dvr-heartbeat")

    async def close(self) -> None:
        if self._hb_task:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except (asyncio.CancelledError, Exception):
                pass
            self._hb_task = None
        await self._http.aclose()

    @property
    def last_success_age(self) -> float | None:
        return None if self.last_success is None else time.monotonic() - self.last_success

    # ---- transport -------------------------------------------------------
    async def _post_raw(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
        if path not in ALLOWED_PATHS:
            raise DvrError(f"endpoint not allowed: {path}")
        try:
            # Read with a cap: whatever answers at the DVR's address must not be able to fill our memory.
            async with self._http.stream("POST", path, json=body, headers=headers or {}) as r:
                content = bytearray()
                async for chunk in r.aiter_bytes():
                    content += chunk
                    if len(content) > MAX_RESPONSE:
                        raise DvrUnavailable("oversized reply from the DVR")
                return httpx.Response(r.status_code, headers=r.headers, content=bytes(content))
        except httpx.HTTPError as e:
            raise DvrUnavailable(f"{type(e).__name__} talking to DVR") from None

    async def _login(self) -> None:
        now = time.monotonic()
        if self._auth_failed_at is not None and now - self._auth_failed_at < self._auth_lockout:
            raise DvrAuthError("login refused earlier; waiting before retrying")
        if now - self._last_login_attempt < self._min_relogin and self._last_login_attempt:
            raise DvrUnavailable("login rate limit; waiting before a new login")
        self._last_login_attempt = now
        body = {"version": "1.0", "data": {}}
        r1 = await self._post_raw(LOGIN_PATH, body)
        if r1.status_code != 401:
            raise DvrAuthError(f"unexpected login challenge status {r1.status_code}")
        challenge = parse_challenge(r1.headers.get("www-authenticate", ""))
        auth = build_digest_header("POST", LOGIN_PATH, self._user, self._password, challenge)
        r2 = await self._post_raw(LOGIN_PATH, body, {"Authorization": auth})
        if r2.status_code == 401:
            self._auth_failed_at = time.monotonic()
            raise DvrAuthError("DVR refused the username/password")
        if r2.status_code != 200:
            raise DvrUnavailable(f"login status {r2.status_code}")
        m = re.search(r"(?:^|[;,\s])session=([^;,\s]+)", "; ".join(r2.headers.get_list("set-cookie")))
        csrf = r2.headers.get("x-csrftoken")
        if not m or not csrf:
            raise DvrAuthError("login reply had no session/csrf token")
        self._session, self._csrf = m.group(1), csrf
        self._auth_failed_at = None
        self.login_count += 1
        self.last_success = time.monotonic()
        log.info("DVR login ok (login #%d)", self.login_count)

    def _auth_headers(self) -> dict[str, str]:
        return {"Cookie": f"session={self._session}", "X-csrftoken": self._csrf or ""}

    async def _call_locked(self, path: str, data: dict[str, Any] | None) -> dict[str, Any]:
        body = {"version": "1.0", "data": data or {}}
        for attempt in (1, 2):
            if self._session is None:
                await self._login()
            r = await self._post_raw(path, body, self._auth_headers())
            if r.status_code == 401:
                self._session = None
                if attempt == 1:
                    continue
                raise DvrAuthError("session rejected after re-login")
            if r.status_code != 200:
                raise DvrUnavailable(f"{path}: HTTP {r.status_code}")
            try:
                doc = r.json()
            except ValueError:
                raise DvrApiError(path, "invalid_json") from None
            if not isinstance(doc, dict):
                raise DvrApiError(path, "invalid_json")
            result = str(doc.get("result", ""))
            if result == "no_heartbeat" and attempt == 1 and path != HEARTBEAT_PATH:
                await self._call_locked(HEARTBEAT_PATH, None)
                continue
            if result != "success":
                raise DvrApiError(path, result or "no_result")
            self.last_success = time.monotonic()
            data = doc.get("data") or {}
            if not isinstance(data, dict):
                raise DvrApiError(path, "invalid_json")
            return data
        raise DvrApiError(path, "retry_exhausted")  # pragma: no cover

    async def call(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if path not in ALLOWED_PATHS:
            raise DvrError(f"endpoint not allowed: {path}")
        async with self._lock:
            return await self._call_locked(path, data)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._hb_interval)
            if self._session is None:
                continue
            try:
                await self.call(HEARTBEAT_PATH)
            except DvrError as e:
                log.warning("heartbeat failed: %s", e)

    # ---- typed calls -----------------------------------------------------
    async def system_info(self) -> DeviceInfo:
        return DeviceInfo.parse(await self.call("/API/Login/DeviceInfo/Get"))

    async def base_info(self) -> BaseInfo:
        """Model, firmware and hardware versions (shown by the connection test)."""
        return BaseInfo.parse(await self.call("/API/SystemInfo/Base/Get"))

    async def channel_info(self) -> list[ChannelInfo]:
        return ChannelInfo.parse_list(await self.call("/API/Login/ChannelInfo/Get"))

    async def disk_info(self) -> list[DiskInfo]:
        return DiskInfo.parse_list(await self.call("/API/StorageConfig/Disk/Get"))

    async def record_state(self) -> list[ChannelRecordState]:
        return parse_record_state(await self.call("/API/SystemInfo/Record/Get"))

    async def event_check(self) -> EventState:
        return EventState.parse(await self.call("/API/Event/Check"))

    async def search_records(
        self, channels: list[int], day: date, *, stream: str = "Mainstream"
    ) -> list[Clip]:
        d = day.strftime(DVR_DATE_FMT)
        data = await self.call(
            "/API/Playback/SearchRecord/Search",
            {
                "channel": [f"CH{c}" for c in channels],
                "start_date": d,
                "end_date": d,
                "start_time": "00:00:00",
                "end_time": "23:59:59",
                "stream_mode": stream,
                "record_type": 4294967295,
            },
        )
        return parse_search(data)

    # ---- channel names (OSD) -----------------------------------------------
    async def osd_name_max_len(self, channel: int, rng: dict[str, Any] | None = None) -> int:
        """Longest name the DVR accepts for this channel, from OSD/Range."""
        rng = rng if rng is not None else await self.call(OSD_RANGE)
        try:
            n = rng["channel_info"]["items"][f"CH{channel}"]["items"]["name"]["items"]["text"]["max_len"]
            return int(n)
        except (KeyError, TypeError, ValueError):
            return DEFAULT_NAME_MAX_LEN

    async def osd_names(self) -> dict[int, str]:
        """Channel number -> the OSD name text currently stored on the DVR."""
        return osd_names(await self.call(OSD_GET))

    async def osd_overview(self) -> tuple[dict[int, str], dict[int, int]]:
        """(names, max name length per channel) for the settings page: one OSD/Range and one OSD/Get."""
        rng = await self.call(OSD_RANGE)
        names = osd_names(await self.call(OSD_GET))
        return names, {n: await self.osd_name_max_len(n, rng) for n in names}

    async def set_channel_name(self, channel: int, name: str) -> str:
        """Rename one channel on the DVR (stored on the device, so it also shows in the OSD text
        and on the DVR's own screen).

        Read-modify-write: fetch the current OSD config, change ONLY
        ``channel_info.CH<n>.name.text`` and send the rest back as it was, then re-read to confirm.
        What is left out of the object is what OSD/Range does not describe as writable: Get also
        returns placeholder entries (``IP_CH1: {"reason": "Not configured"}``) that Range does not list, and
        an offline channel carries a read-only ``status``. Sending the whole Get object back was answered with
        HTTP 400 by the real DVR (2026-11-23); the same data without the unlisted entries was accepted.
        Returns the name the DVR now reports. Raises DvrApiError if the DVR refuses (``.result``
        carries its reason) or the readback does not match.
        """
        key = f"CH{channel}"
        async with self._write_lock:
            rng = await self.call(OSD_RANGE)
            current = await self.call(OSD_GET)
            entry = (current.get("channel_info") or {}).get(key)
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), dict) or "text" not in entry["name"]:
                raise DvrApiError(OSD_GET, "unknown_channel")
            new = writable_osd(current, rng)
            new["channel_info"][key]["name"]["text"] = name
            await self.call(OSD_SET, new)
            after = await self.call(OSD_GET)
        confirmed = osd_names(after).get(channel)
        if confirmed != name:
            raise DvrApiError(OSD_SET, "verify_failed")
        # Everything but the one field must be untouched; report (do not fail) if the DVR normalised something.
        if new != writable_osd(after, rng):
            log.warning("OSD readback differs from what was sent beyond the renamed field")
        return confirmed


def writable_osd(osd: dict[str, Any], rng: dict[str, Any]) -> dict[str, Any]:
    """Deep copy of an OSD/Get result reduced to what OSD/Range declares writable: only the channel
    entries Range lists, minus fields it marks ``mode: "r"``. Every value is kept exactly as read."""
    items = ((rng.get("channel_info") or {}).get("items")) or {}
    out = copy.deepcopy(osd)
    chans = out.get("channel_info")
    if not isinstance(chans, dict):
        return out
    for k in list(chans):
        schema = (items.get(k) or {}).get("items") if isinstance(items.get(k), dict) else None
        if schema is None:
            del chans[k]
            continue
        if isinstance(chans[k], dict):
            for f in list(chans[k]):
                if isinstance(schema.get(f), dict) and schema[f].get("mode") == "r":
                    del chans[k][f]
    return out


def osd_names(osd: dict[str, Any]) -> dict[int, str]:
    out: dict[int, str] = {}
    for key, v in (osd.get("channel_info") or {}).items():
        if not isinstance(v, dict) or not isinstance(v.get("name"), dict) or "text" not in v["name"]:
            continue  # e.g. IP_CH1: {"reason": "Not configured"}
        if re.fullmatch(r"CH\d+", key):
            out[int(key[2:])] = str(v["name"]["text"])
    return out

"""Optional: detections from a Frigate instance, shown on the recordings timeline.

DVR Bridge stays read-only towards Frigate: it lists events (``/api/events``) and fetches event
snapshots, nothing else. Frigate's times are epoch seconds; the DVR's are local wall-clock times in
``dvr.timezone``, so events are converted to that zone to line up with the recordings.

Use Frigate's internal port (5000) from inside the Docker network: it is unauthenticated there,
and the browser never talks to Frigate - snapshots are proxied through DVR Bridge's own login.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import parse_camera_map

log = logging.getLogger("dvrbridge.frigate")

EVENT_ID = re.compile(r"^\d{9,11}\.\d{1,6}-[a-z0-9]{1,12}$")
MAX_EVENTS = 2000
MAX_JSON = 8 * 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024


class FrigateError(Exception):
    pass


@dataclass(frozen=True)
class Detection:
    id: str
    channel: int
    label: str
    score: float
    start: datetime          # DVR-local, naive
    end: datetime | None     # None while the object is still being tracked
    has_snapshot: bool

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "score": round(self.score, 2),
                "start": self.start.isoformat(timespec="seconds"),
                "end": self.end.isoformat(timespec="seconds") if self.end else None,
                "has_snapshot": self.has_snapshot}


def normalise(name: str) -> str:
    """'Front door' -> 'front_door' (how Frigate camera names are usually written)."""
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def camera_map(explicit: str, channel_names: dict[int, str], frigate_cameras: list[str]) -> dict[int, str]:
    """Channel -> Frigate camera: the explicit map wins; otherwise a channel whose name, normalised,
    equals a Frigate camera's name is matched. A channel in the explicit map is never matched by
    name, even when its camera is unknown to Frigate (then it simply has no detections)."""
    known = set(frigate_cameras)
    explicit_map = parse_camera_map(explicit)
    out = {ch: cam for ch, cam in explicit_map.items() if cam in known}
    taken = set(out.values())
    for ch, name in channel_names.items():
        cam = normalise(name)
        if ch not in explicit_map and cam in known and cam not in taken:
            out[ch] = cam
            taken.add(cam)
    return out


class FrigateClient:
    def __init__(self, base_url: str, tz: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.tz = ZoneInfo(tz)
        self._http = httpx.AsyncClient(base_url=self.base, transport=transport, timeout=httpx.Timeout(10.0),
                                       follow_redirects=False, headers={"User-Agent": "dvr-bridge"})
        self._cameras: tuple[float, list[str]] | None = None
        self.up: bool | None = None

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict | None = None, limit: int = MAX_JSON) -> httpx.Response:
        try:
            async with self._http.stream("GET", path, params=params) as r:
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > limit:
                        raise FrigateError("Frigate sent an oversized response")
                resp = httpx.Response(r.status_code, headers=r.headers, content=bytes(body))
        except httpx.HTTPError as e:
            self.up = False
            raise FrigateError(f"Frigate did not answer ({type(e).__name__})") from None
        self.up = resp.status_code < 500
        if resp.status_code >= 400:
            raise FrigateError(f"Frigate answered HTTP {resp.status_code}")
        return resp

    async def cameras(self) -> list[str]:
        """Frigate's camera names (cached for 5 minutes)."""
        if self._cameras and time.monotonic() - self._cameras[0] < 300:
            return self._cameras[1]
        try:
            cfg = (await self._get("/api/config")).json()
        except ValueError:
            raise FrigateError("Frigate's config was not JSON") from None
        cams = cfg.get("cameras") if isinstance(cfg, dict) else None
        if not isinstance(cams, dict):
            raise FrigateError("unexpected config response")
        names = sorted(str(k) for k in cams)
        self._cameras = (time.monotonic(), names)
        return names

    def _local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.tz).replace(tzinfo=None)

    async def detections(self, day: date, mapping: dict[int, str]) -> tuple[list[Detection], bool]:
        """Events that overlap ``day`` (DVR-local) on the mapped cameras; (detections, truncated)."""
        if not mapping:
            return [], False
        start = datetime.combine(day, datetime.min.time(), self.tz)
        params = {"after": int((start - timedelta(hours=1)).timestamp()),
                  "before": int((start + timedelta(days=1)).timestamp()),
                  "cameras": ",".join(sorted(set(mapping.values()))), "limit": MAX_EVENTS}
        try:
            events = (await self._get("/api/events", params)).json()
        except ValueError:
            raise FrigateError("Frigate's events were not JSON") from None
        if not isinstance(events, list):
            raise FrigateError("unexpected events response")
        by_cam = {cam: ch for ch, cam in mapping.items()}
        out: list[Detection] = []
        for ev in events:
            try:
                ch = by_cam.get(ev["camera"])
                if ch is None or ev.get("false_positive"):
                    continue
                st = self._local(float(ev["start_time"]))
                en = self._local(float(ev["end_time"])) if ev.get("end_time") else None
                if (en or st).date() < day or st.date() > day:
                    continue
                data = ev.get("data") or {}
                score = data.get("top_score") or ev.get("top_score") or data.get("score") or 0
                out.append(Detection(str(ev["id"]), ch, str(ev["label"]), float(score), st, en, bool(ev.get("has_snapshot"))))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        out.sort(key=lambda d: d.start)
        return out, len(events) >= MAX_EVENTS

    async def snapshot(self, event_id: str, allowed_cameras: set[str]) -> bytes:
        """The event's snapshot (JPEG, with its box drawn), if the event belongs to a mapped camera."""
        if not EVENT_ID.fullmatch(event_id or ""):
            raise FrigateError("bad event id")
        try:
            ev = (await self._get(f"/api/events/{event_id}")).json()
        except ValueError:
            raise FrigateError("Frigate's event was not JSON") from None
        if not isinstance(ev, dict) or ev.get("camera") not in allowed_cameras or not ev.get("has_snapshot"):
            raise FrigateError("no snapshot for that event")
        r = await self._get(f"/api/events/{event_id}/snapshot.jpg", {"bounding_box": 1, "height": 360}, MAX_IMAGE)
        if not r.headers.get("content-type", "").startswith("image/jpeg"):
            raise FrigateError("Frigate did not send a JPEG")
        return r.content

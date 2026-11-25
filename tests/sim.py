"""A stand-in DVR (HTTP API on :85, from the test fixtures) and Frigate (:5000) for trying the UI
without hardware. Live view and playback are not simulated; recordings search, camera names, status
and Frigate detections (for today, in SIM_TZ) are.

    docker build --target test -t dvr-bridge:test .
    docker network create dvrb-sim
    docker run -d --name sim --network dvrb-sim dvr-bridge:test python -m tests.sim
    docker run -d --name dvr-bridge-sim --network dvrb-sim -p 127.0.0.1:8099:8099 \
      -e DVRBRIDGE_DVR_HOST=sim -e DVRBRIDGE_DVR_USERNAME=tester -e DVRBRIDGE_DVR_PASSWORD=s3cret-pw \
      -e DVRBRIDGE_DETECTION_FRIGATE_URL=http://sim:5000 -e DVRBRIDGE_AUTH_MODE=none dvr-bridge:local
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import date, datetime
from zoneinfo import ZoneInfo

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.conftest import FakeDvr

TZ = ZoneInfo(os.environ.get("SIM_TZ", "UTC"))


def snapshot_jpeg() -> bytes:
    """A plain grey 640x360 JPEG made by the bundled ffmpeg (no image library needed)."""
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x445566:s=640x360",
                          "-frames:v", "1", "-f", "mjpeg", "pipe:1"], capture_output=True, check=True)
    return out.stdout


def today_events() -> list[dict]:
    """Detections placed against the fixture recordings: two on recorded clips, two where the DVR
    recorded nothing (one with a snapshot, one without)."""
    d = datetime.now(TZ).date()

    def at(hms: str, day: date = d) -> float:
        h, m, s = map(int, hms.split(":"))
        return datetime(day.year, day.month, day.day, h, m, s, tzinfo=TZ).timestamp()

    def ev(n, cam, label, start, end, snap=True, score=0.9):
        return {"id": f"{int(at('00:00:00'))}.{n}-sim{n:03d}", "camera": cam, "label": label, "start_time": at(start),
                "end_time": at(end), "has_snapshot": snap, "false_positive": False, "data": {"top_score": score}}

    return [
        ev(1, "channel_1", "person", "14:05:00", "14:05:30", score=0.96),   # inside the 14:04:47 clip
        ev(2, "channel_1", "car", "15:00:00", "15:00:20", score=0.88),      # no recording then
        ev(3, "channel_3", "dog", "23:59:50", "23:59:58", score=0.81),      # inside the clip across midnight
        ev(4, "channel_2", "cat", "09:00:00", "09:00:12", snap=False),      # no recording, no snapshot
    ]


def frigate_app() -> Starlette:
    jpeg = snapshot_jpeg()

    async def config(request: Request):
        return JSONResponse({"cameras": {f"channel_{n}": {} for n in range(1, 9)}})

    async def events(request: Request):
        cams = set(request.query_params.get("cameras", "").split(","))
        return JSONResponse([e for e in today_events() if e["camera"] in cams])

    async def one(request: Request):
        for e in today_events():
            if e["id"] == request.path_params["eid"]:
                return JSONResponse(e)
        return JSONResponse({"message": "Event not found"}, status_code=404)

    async def snap(request: Request):
        for e in today_events():
            if e["id"] == request.path_params["eid"] and e["has_snapshot"]:
                return Response(jpeg, media_type="image/jpeg")
        return JSONResponse({"message": "Event not found"}, status_code=404)

    return Starlette(routes=[Route("/api/config", config), Route("/api/events", events),
                             Route("/api/events/{eid}", one), Route("/api/events/{eid}/snapshot.jpg", snap)])


async def main() -> None:
    dvr = FakeDvr()
    servers = [uvicorn.Server(uvicorn.Config(dvr.app, host="0.0.0.0", port=85, log_level="warning")),
               uvicorn.Server(uvicorn.Config(frigate_app(), host="0.0.0.0", port=5000, log_level="warning"))]
    print("simulated DVR on :85 (tester / s3cret-pw), simulated Frigate on :5000", flush=True)
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())

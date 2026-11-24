"""FastAPI app: live view proxy, recordings browser, healthz, metrics."""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from .config import Settings
from .channels import InvalidName, csrf_problem, validate_name
from .dvr import DvrApiError, DvrAuthError, DvrClient, DvrError
from .dvr.client import DEFAULT_NAME_MAX_LEN
from .go2rtc import MAX_CHANNELS, STREAM_KINDS, Go2rtc, stream_name
from .dvr9000 import (
    Dvr9000AuthError,
    Dvr9000Error,
    Dvr9000Playback,
    MainSession,
    PlaybackBusy,
    PlaybackRefused,
)
from .playback import NoFootage, PlaybackNotImplemented, PlaybackProvider, PlaybackStats, prime, remux_to_fmp4
from .state import Monitor, render_metrics
from .auth.gate import AuthGate
from .auth.routes import build_router
from .auth.service import AuthService
from .auth.store import Store

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("dvrbridge")


def create_app(
    settings: Settings | None = None,
    *,
    dvr: DvrClient | None = None,
    go2rtc: Go2rtc | None = None,
    playback: PlaybackProvider | None = None,
    media: MainSession | None = None,
    auth: AuthService | None = None,
) -> FastAPI:
    s = settings or Settings.from_env()
    if auth is None:
        # With auth.mode none there are no accounts to keep; the audit log lives in memory.
        auth = AuthService(s, store=Store(":memory:") if s.auth_mode == "none" else None)
    dvr = dvr or DvrClient(s.dvr_host, s.dvr_user, s.dvr_password, port=s.dvr_port)
    monitor = Monitor(dvr, interval=s.poll_interval)

    def channel_count() -> int:
        """The configured count, or (channels: 0) however many the DVR reports."""
        return s.channels or len(monitor.channels)

    g2r = go2rtc if go2rtc is not None else Go2rtc(s)
    if playback is None:
        media = media or MainSession(s.dvr_host, s.dvr_media_port, s.dvr_user, s.dvr_password)
        playback = Dvr9000Playback(media, max_concurrent=s.max_playbacks, stats=PlaybackStats())
    provider = playback
    tz = ZoneInfo(s.tz)
    search_cache: dict[date, tuple[float, dict]] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        auth.startup()
        await dvr.start()
        await monitor.start()
        await g2r.start()
        try:
            yield
        finally:
            await g2r.stop()
            await monitor.stop()
            await dvr.close()
            if media is not None:
                await media.close()
            await auth.close()

    app = FastAPI(title="dvr-bridge", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.add_middleware(AuthGate, service=auth)
    app.include_router(build_router(auth))

    def audit(request: Request, action: str, detail: str) -> None:
        user = request.state.user
        auth.store.audit(action, user.username if user else None, detail, request.state.client_ip)

    @app.middleware("http")
    async def static_revalidate(request, call_next):
        # StaticFiles sends no Cache-Control, so browsers heuristically reuse old JS/CSS and a
        # deploy (e.g. the Settings nav link) stays invisible. no-cache = always revalidate;
        # unchanged files still come back as a cheap 304 via ETag.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # ---- health / metrics ------------------------------------------------
    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz() -> str:
        return "ok\n"

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics(request: Request) -> PlainTextResponse:
        if not s.metrics_enabled:
            return PlainTextResponse("metrics are disabled\n", status_code=404)
        if s.metrics_token:
            sent = request.headers.get("authorization", "")
            if not hmac.compare_digest(sent.encode(), f"Bearer {s.metrics_token}".encode()):
                return PlainTextResponse("bearer token required\n", status_code=401,
                                         headers={"WWW-Authenticate": "Bearer"})
        return PlainTextResponse(
            render_metrics(
                monitor.snapshot(
                    go2rtc_up=g2r.running,
                    playback=provider.stats,
                    media_session_up=media.up if media is not None else None,
                )
            ),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ---- JSON API --------------------------------------------------------
    @app.get("/api/channels")
    async def channels() -> dict:
        names = monitor.channel_names(channel_count())
        return {
            "dvr_up": monitor.up,
            "channels": [{"channel": n, "name": names[n]} for n in sorted(names)],
            "today": datetime.now(tz).date().isoformat(),
        }

    @app.get("/api/recordings")
    async def recordings(day: str = Query(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$")):
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return JSONResponse({"error": "bad_date"}, status_code=400)
        ttl = 30 if d >= datetime.now(tz).date() else 300
        hit = search_cache.get(d)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            clips = await dvr.search_records(list(range(1, channel_count() + 1)), d)
        except DvrError as e:
            log.warning("recordings search failed: %s", e)
            return JSONResponse({"error": "dvr_unavailable"}, status_code=502)
        names = monitor.channel_names(channel_count())
        per: dict[int, list[dict]] = {n: [] for n in names}
        for c in clips:
            per.setdefault(c.channel, []).append(
                {
                    "start": c.start.isoformat(timespec="seconds"),
                    "end": c.end.isoformat(timespec="seconds"),
                    "duration_s": c.duration_s,
                    "size": c.size,
                    "record_id": c.record_id,
                    "disk_event_id": c.disk_event_id,
                }
            )
        doc = {
            "date": d.isoformat(),
            "channels": [{"channel": n, "name": names.get(n, f"Channel {n}"), "clips": per[n]} for n in sorted(per)],
        }
        if len(search_cache) > 64:
            search_cache.clear()
        search_cache[d] = (time.monotonic(), doc)
        return doc

    # ---- settings: rename cameras (stored on the DVR) ----------------------
    def dvr_failure(e: DvrError, what: str) -> JSONResponse:
        if isinstance(e, DvrApiError):
            return JSONResponse({"error": "dvr_refused", "detail": f"The DVR refused {what} ({e.result})."},
                                status_code=502)
        if isinstance(e, DvrAuthError):
            return JSONResponse({"error": "dvr_login_refused", "detail": "The DVR refused the gateway's login."},
                                status_code=503)
        return JSONResponse({"error": "dvr_unavailable", "detail": "The DVR did not answer."}, status_code=502)

    @app.get("/api/settings/channels")
    async def settings_channels():
        try:
            names, limits = await dvr.osd_overview()
        except DvrError as e:
            log.warning("settings read failed: %s", e)
            return dvr_failure(e, "the request")
        nums = [n for n in range(1, channel_count() + 1) if n in names]
        return {
            "max_length": min((limits[n] for n in nums), default=DEFAULT_NAME_MAX_LEN),
            "channels": [{"channel": n, "name": names[n], "max_length": limits[n]} for n in nums],
        }

    @app.post("/api/settings/channels/{channel}")
    async def settings_rename(channel: int, request: Request):
        problem = csrf_problem(request.headers)
        if problem:
            return JSONResponse({"error": "forbidden", "detail": problem}, status_code=403)
        if not 1 <= channel <= channel_count():
            return JSONResponse({"error": "no_such_channel", "detail": f"channel must be 1-{channel_count()}"},
                                status_code=404)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad_request", "detail": 'send JSON like {"name": "Front door"}'},
                                status_code=400)
        try:
            limit = await dvr.osd_name_max_len(channel)
            name = validate_name(body.get("name"), limit)
        except InvalidName as e:
            return JSONResponse({"error": e.code, "detail": e.detail}, status_code=400)
        except DvrError as e:
            log.warning("rename: could not read the name limit: %s", e)
            return dvr_failure(e, "the request")
        try:
            confirmed = await dvr.set_channel_name(channel, name)
        except DvrError as e:
            log.warning("rename of channel %d failed: %s", channel, e)
            return dvr_failure(e, "the new name")
        log.info("channel %d renamed on the DVR to %r", channel, confirmed)
        audit(request, "camera_renamed", f"channel {channel} -> {confirmed}")
        await monitor.refresh_channels((channel, confirmed))
        search_cache.clear()  # cached day searches carry the old names
        return {"channel": channel, "name": confirmed}

    # ---- recorded playback / export (DVR port 9000) ------------------------
    MAX_RANGE = timedelta(hours=s.max_range_hours)

    def local_naive(dt: datetime) -> datetime:
        return dt.astimezone(tz).replace(tzinfo=None) if dt.tzinfo else dt

    async def clip_response(request: Request, channel: int, start: datetime, end: datetime, stream: str,
                            filename: str | None):
        start, end = local_naive(start).replace(microsecond=0), local_naive(end).replace(microsecond=0)
        if end <= start or end - start > MAX_RANGE:
            return JSONResponse({"error": "bad_range",
                                 "detail": f"end must be after start, at most {s.max_range_hours} hours."},
                                status_code=400)
        sess = None
        try:
            sess = await provider.open_session(channel, start, end, stream)
            fps, source = await prime(sess)
        except PlaybackNotImplemented as e:
            return JSONResponse({"error": "not_implemented", "detail": str(e)}, status_code=501)
        except PlaybackBusy:
            return JSONResponse({"error": "busy", "detail": "Too many clips are playing; try again shortly."},
                                status_code=429, headers={"Retry-After": "5"})
        except PlaybackRefused as e:
            return JSONResponse({"error": "dvr_refused", "detail": f"The DVR refused the request (code {e.ack})."},
                                status_code=502)
        except NoFootage:
            return JSONResponse({"error": "no_footage", "detail": "The DVR has no video for that range."},
                                status_code=404)
        except Dvr9000AuthError:
            return JSONResponse({"error": "dvr_login_refused",
                                 "detail": "The DVR refused the gateway's media login."}, status_code=503)
        except Dvr9000Error as e:
            log.warning("playback open failed: %s", e)
            return JSONResponse({"error": "dvr_unavailable", "detail": "The DVR media port did not answer."},
                                status_code=502)

        audit(request, "export" if filename else "playback",
              f"channel {channel} {start:%Y-%m-%d %H:%M:%S}-{end:%H:%M:%S} {stream}")

        async def body():
            try:
                async for chunk in remux_to_fmp4(source, fps):
                    provider.stats.bytes_streamed += len(chunk)
                    yield chunk
            finally:
                await source.aclose()
                await sess.aclose()

        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if filename:
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return StreamingResponse(body(), media_type="video/mp4", headers=headers,
                                 background=BackgroundTask(sess.aclose))

    clip_params = dict(
        channel=Query(..., ge=1, le=MAX_CHANNELS),
        start=Query(...),
        end=Query(...),
        stream=Query("Mainstream", pattern="^(Mainstream|Substream)$"),
    )

    @app.get("/api/playback")
    async def playback_endpoint(request: Request, channel: int = clip_params["channel"],
                                start: datetime = clip_params["start"], end: datetime = clip_params["end"],
                                stream: str = clip_params["stream"]):
        return await clip_response(request, channel, start, end, stream, None)

    @app.get("/api/export")
    async def export_endpoint(request: Request, channel: int = clip_params["channel"],
                              start: datetime = clip_params["start"], end: datetime = clip_params["end"],
                              stream: str = clip_params["stream"]):
        name = re.sub(r"[^A-Za-z0-9_-]+", "", monitor.channel_names(channel_count()).get(channel, "").replace(" ", "-"))
        if name == f"Channel-{channel}":
            name = ""
        a, b = local_naive(start), local_naive(end)
        fn = f"ch{channel}" + (f"-{name}" if name else "") + f"-{a:%Y%m%d-%H%M%S}-{b:%H%M%S}.mp4"
        return await clip_response(request, channel, start, end, stream, fn)

    @app.get("/api/playback/status")
    async def playback_status() -> dict:
        st = provider.stats
        return {"active": st.active, "max": getattr(provider, "max_concurrent", None), "last_error": st.last_error}


    # ---- live view: proxy go2rtc's MSE websocket -------------------------
    @app.websocket("/ws/live/{channel}/{kind}")
    async def live(ws: WebSocket, channel: int, kind: str) -> None:
        origin = ws.headers.get("origin")
        host = ws.headers.get("host")
        if origin and host and origin.split("://", 1)[-1] != host:
            await ws.close(code=1008)
            return
        if kind not in STREAM_KINDS or not 1 <= channel <= channel_count():
            await ws.close(code=1008)
            return
        await ws.accept()
        url = f"ws://127.0.0.1:{s.go2rtc_port}/api/ws?src={stream_name(channel, kind)}"
        try:
            async with websockets.connect(url, max_size=None, open_timeout=5) as up:

                async def down() -> None:
                    async for msg in up:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)

                async def upstream() -> None:
                    while True:
                        m = await ws.receive()
                        if m["type"] == "websocket.disconnect":
                            return
                        if m.get("text") is not None:
                            await up.send(m["text"])
                        elif m.get("bytes") is not None:
                            await up.send(m["bytes"])

                tasks = [asyncio.create_task(down()), asyncio.create_task(upstream())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except (OSError, websockets.WebSocketException, WebSocketDisconnect):
            pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    # ---- pages -----------------------------------------------------------
    def page(name: str):
        async def handler() -> FileResponse:
            return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})

        return handler

    app.add_api_route("/", page("index.html"), include_in_schema=False)
    app.add_api_route("/camera/{channel}", page("camera.html"), include_in_schema=False)
    app.add_api_route("/recordings", page("recordings.html"), include_in_schema=False)
    app.add_api_route("/settings", page("settings.html"), include_in_schema=False)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app

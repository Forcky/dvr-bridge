"""FastAPI app: live view proxy, recordings browser, healthz, metrics."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from typing import Callable, Mapping
from contextlib import asynccontextmanager
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from . import REVISION, __version__
from .config import BY_ATTR, ConfigError, Settings, config_path, schema, valid_host
from .config import save as save_settings
from .connection_test import Target
from .connection_test import run as run_connection_test
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
from .frigate import FrigateClient, FrigateError, camera_map
from .auth.gate import AuthGate
from .auth.netutil import is_trusted, parse_networks
from .auth.routes import build_router
from .auth.service import AuthService
from .auth.store import Store

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("dvrbridge")
_CODECS_RE = re.compile(r"^[A-Za-z0-9.,]{0,200}$")


def mse_request(text: str | None) -> str | None:
    """The one message a browser may send to go2rtc through the live-view proxy, re-encoded; else None."""
    if not text or len(text) > 400:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("type") != "mse":
        return None
    value = doc.get("value", "")
    if not isinstance(value, str) or not _CODECS_RE.match(value):
        return None
    return json.dumps({"type": "mse", "value": value})


def public_go2rtc_text(text: str) -> str:
    """go2rtc's text messages for the browser. Its error text names internal addresses (the DVR's IP and
    port), which viewers have no need to see."""
    try:
        doc = json.loads(text)
    except ValueError:
        return json.dumps({"type": "error", "value": "stream error"})
    if isinstance(doc, dict) and doc.get("type") == "mse" and isinstance(doc.get("value"), str):
        return text
    return json.dumps({"type": "error", "value": "stream error"})


def create_app(
    settings: Settings | None = None,
    *,
    dvr: DvrClient | None = None,
    go2rtc: Go2rtc | None = None,
    playback: PlaybackProvider | None = None,
    media: MainSession | None = None,
    auth: AuthService | None = None,
    env: Mapping[str, str] | None = None,
    request_reload: Callable[[], None] | None = None,
    frigate: FrigateClient | None = None,
) -> FastAPI:
    """The app for one configuration. ``request_reload`` (from dvrbridge.server) rebuilds it after the
    settings page saves a change; ``env`` is where settings are read from and saved to."""
    s = settings or Settings.from_env(env)
    env = dict(os.environ if env is None else env)
    env.setdefault("DVRBRIDGE_DATA_DIR", s.data_dir)
    configured = s.dvr_configured
    if auth is None:
        # With auth.mode none there are no accounts to keep; the audit log lives in memory.
        auth = AuthService(s, store=Store(":memory:") if s.auth_mode == "none" else None)
    # Not configured yet: a placeholder client that is never started or called.
    dvr = dvr or DvrClient(s.dvr_host or "unconfigured.invalid", s.dvr_user, s.dvr_password, port=s.dvr_port)
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
    if frigate is None and s.frigate_url:
        frigate = FrigateClient(s.frigate_url, s.tz)
    detection_cache: dict[date, tuple[float, dict]] = {}
    metrics_networks = parse_networks(s.metrics_allow)
    live_sockets: dict[int, int] = {}     # user id -> open live-view websockets

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        auth.startup()
        if configured:
            await dvr.start()
            await monitor.start()
            await g2r.start()
        else:
            log.warning("No DVR configured yet: an admin can set it up on the Settings page")
        try:
            yield
        finally:
            if configured:
                await g2r.stop()
                await monitor.stop()
            await dvr.close()
            if media is not None:
                await media.close()
            if frigate is not None:
                await frigate.close()
            await auth.close()

    app = FastAPI(title="dvr-bridge", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.add_middleware(AuthGate, service=auth)
    app.include_router(build_router(auth))

    def audit(request: Request, action: str, detail: str) -> None:
        user = request.state.user
        auth.store.audit(action, user.username if user else None, detail, request.state.client_ip)

    def not_configured() -> JSONResponse:
        return JSONResponse({"error": "not_configured", "detail": "No DVR is set up yet. An admin can add it under Settings."},
                            status_code=503)

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
        # Camera names and what is (not) recording are not for strangers: the token, an address listed
        # in monitoring.metrics_allow, or a signed-in admin.
        sent = request.headers.get("authorization", "")
        user = request.state.user
        allowed = ((s.metrics_token and hmac.compare_digest(sent.encode(), f"Bearer {s.metrics_token}".encode()))
                   or is_trusted(request.client.host if request.client else "", metrics_networks)
                   # auth.mode none has no logins: there the token alone decides, as before.
                   or (user is not None and user.is_admin and (s.auth_mode != "none" or not s.metrics_token)))
        if not allowed:
            return PlainTextResponse("metrics need the bearer token (monitoring.metrics_token), an address in "
                                     "monitoring.metrics_allow, or an admin login\n", status_code=401,
                                     headers={"WWW-Authenticate": "Bearer"})
        build = (f'# HELP dvrbridge_build_info Version and revision of this DVR Bridge; value is always 1.\n'
                 f'# TYPE dvrbridge_build_info gauge\n'
                 f'dvrbridge_build_info{{version="{__version__}",revision="{REVISION[:40]}"}} 1\n')
        return PlainTextResponse(
            build + render_metrics(
                monitor.snapshot(
                    go2rtc_up=g2r.running,
                    playback=provider.stats,
                    media_session_up=media.up if media is not None else None,
                    frigate_up=frigate.up if frigate is not None else None,   # absent until Frigate was asked
                )
            ),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ---- JSON API --------------------------------------------------------
    @app.get("/api/channels")
    async def channels() -> dict:
        names = monitor.channel_names(channel_count())
        return {
            "configured": configured,
            "dvr_up": monitor.up,
            "channels": [{"channel": n, "name": names[n]} for n in sorted(names)],
            "today": datetime.now(tz).date().isoformat(),
        }

    @app.get("/api/recordings")
    async def recordings(day: str = Query(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$")):
        if not configured:
            return not_configured()
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

    # ---- detections from Frigate (optional) --------------------------------
    async def frigate_mapping() -> dict[int, str]:
        return camera_map(s.frigate_cameras, monitor.channel_names(channel_count()), await frigate.cameras())

    @app.get("/api/detections")
    async def detections(day: str = Query(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$")):
        if frigate is None:
            return {"enabled": False}
        if not configured:
            return not_configured()
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return JSONResponse({"error": "bad_date"}, status_code=400)
        ttl = 20 if d >= datetime.now(tz).date() else 300
        hit = detection_cache.get(d)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            mapping = await frigate_mapping()
            found, truncated = await frigate.detections(d, mapping)
        except FrigateError as e:
            log.warning("detections from Frigate failed: %s", e)
            return JSONResponse({"enabled": True, "error": "frigate_unavailable", "detail": str(e)}, status_code=502)
        per: dict[int, list[dict]] = {}
        for det in found:
            per.setdefault(det.channel, []).append(det.public())
        doc = {"enabled": True, "date": d.isoformat(), "truncated": truncated,
               "cameras": {str(ch): cam for ch, cam in sorted(mapping.items())},
               "channels": {str(ch): items for ch, items in sorted(per.items())}}
        if len(detection_cache) > 32:
            detection_cache.clear()
        detection_cache[d] = (time.monotonic(), doc)
        return doc

    @app.get("/api/detections/{event_id}/snapshot.jpg")
    async def detection_snapshot(event_id: str):
        if frigate is None:
            return JSONResponse({"error": "not_enabled"}, status_code=404)
        try:
            image = await frigate.snapshot(event_id, set((await frigate_mapping()).values()))
        except FrigateError as e:
            status = 400 if "bad event id" in str(e) else 502 if "did not answer" in str(e) else 404
            return JSONResponse({"error": "no_snapshot", "detail": str(e)}, status_code=status)
        return Response(image, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})

    # ---- settings: rename cameras (stored on the DVR) ----------------------
    def dvr_failure(e: DvrError, what: str) -> JSONResponse:
        if isinstance(e, DvrApiError):
            return JSONResponse({"error": "dvr_refused", "detail": f"The DVR refused {what} ({e.result})."},
                                status_code=502)
        if isinstance(e, DvrAuthError):
            return JSONResponse({"error": "dvr_login_refused", "detail": "The DVR refused DVR Bridge's login (check the DVR username and password in Settings)."},
                                status_code=503)
        return JSONResponse({"error": "dvr_unavailable", "detail": "The DVR did not answer."}, status_code=502)

    @app.get("/api/settings/channels")
    async def settings_channels():
        if not configured:
            return not_configured()
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
        if not configured:
            return not_configured()
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
        if not configured:
            return not_configured()
        if channel > channel_count():
            return JSONResponse({"error": "no_such_channel", "detail": f"channel must be 1-{channel_count()}"},
                                status_code=404)
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
                                 "detail": "The DVR refused DVR Bridge's media login (check the DVR username and password in Settings)."}, status_code=503)
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
        if not configured or kind not in STREAM_KINDS or not 1 <= channel <= channel_count():
            await ws.close(code=1008)
            return
        # A handful of tabs per person is normal; hundreds of sockets from one account is not.
        user = ws.scope.get("state", {}).get("user")
        uid = user.id if user is not None else 0
        if uid and live_sockets.get(uid, 0) >= max(8, 4 * channel_count()):
            await ws.close(code=1013)
            return
        await ws.accept()
        live_sockets[uid] = live_sockets.get(uid, 0) + 1
        url = f"ws://127.0.0.1:{s.go2rtc_port}/api/ws?src={stream_name(channel, kind)}"
        try:
            async with websockets.connect(url, max_size=None, open_timeout=5) as up:

                async def down() -> None:
                    async for msg in up:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(public_go2rtc_text(msg))

                async def upstream() -> None:
                    while True:
                        m = await ws.receive()
                        if m["type"] == "websocket.disconnect":
                            return
                        # The page only ever asks for MSE; nothing else of go2rtc's websocket API
                        # (WebRTC offers and candidates, HLS, MP4...) is passed on.
                        request = mse_request(m.get("text"))
                        if request is not None:
                            await up.send(request)

                tasks = [asyncio.create_task(down()), asyncio.create_task(upstream())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except (OSError, websockets.WebSocketException, WebSocketDisconnect):
            pass
        finally:
            left = live_sockets.get(uid, 1) - 1
            if left > 0:
                live_sockets[uid] = left
            else:
                live_sockets.pop(uid, None)
            try:
                await ws.close()
            except Exception:
                pass

    # ---- system settings (admins; /api/settings/* is admin-only in the auth gate) ----
    test_lock = asyncio.Lock()
    last_test = [0.0]

    @app.get("/api/settings/system")
    async def system_settings():
        return {"configured": configured, "options": schema(s), "config_file": str(config_path(env)),
                "warnings": auth.warnings()}

    @app.put("/api/settings/system")
    async def save_system_settings(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        updates = body.get("updates") if isinstance(body, dict) else None
        if not isinstance(updates, dict) or not updates:
            return JSONResponse({"error": "bad_request", "detail": 'send {"updates": {"section.key": value}}'},
                                status_code=400)
        try:
            proposed = save_settings(updates, env, dry_run=True)
        except ConfigError as e:
            return JSONResponse({"error": "invalid", "detail": str(e), "problems": str(e).split("; ")},
                                status_code=400)
        user = request.state.user
        if proposed.auth_mode == "none" and s.auth_mode != "none":
            return JSONResponse({"error": "refused", "detail": "auth.mode none turns logins off for everyone; it "
                                 "can only be set in the config file or environment."}, status_code=400)
        if proposed.auth_mode in ("oidc", "header") and not proposed.local_login and user.source == "local":
            return JSONResponse({"error": "refused", "detail": f"Keep password logins on until you have logged in "
                                 f"as an admin through {proposed.auth_mode} once - otherwise a mistake in its "
                                 f"settings would lock everyone out."}, status_code=400)
        if (proposed.auth_mode == "local" and s.auth_mode != "local" and auth.store.count_users()
                and not auth.store.count_local_admins()):
            return JSONResponse({"error": "refused", "detail": "Nobody could log in after this change: there is no "
                                 "local admin account with a password. Turn on password logins (auth.local_login), "
                                 "add a local admin on the Users page, then switch."}, status_code=400)
        # The saved DVR password is only ever sent to the DVR it was saved for: pointing the bridge at
        # another address (or other ports) needs the password typed in again.
        moved = [BY_ATTR[a].key for a in ("dvr_host", "dvr_port", "dvr_media_port", "rtsp_port")
                 if getattr(proposed, a) != getattr(s, a)]
        if moved and s.dvr_password and proposed.dvr_host and not updates.get("dvr.password"):
            return JSONResponse({"error": "refused", "detail": f"Enter the DVR password again to change the DVR "
                                 f"{' and '.join(moved)}: the saved password is not sent to a different address."},
                                status_code=400)
        save_settings(updates, env)
        audit(request, "settings_changed", ", ".join(sorted(updates)))
        log.info("settings changed by %s: %s", user.username, ", ".join(sorted(updates)))
        if request_reload:
            request_reload()
        return {"ok": True, "reloading": bool(request_reload),
                "detail": "Saved. Applying now; live video reconnects in a few seconds."
                if request_reload else "Saved. Restart DVR Bridge to apply."}

    @app.post("/api/settings/test-dvr")
    async def test_dvr(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}

        def num(key: str, default: int) -> int:
            try:
                v = int(body.get(key) or default)
            except (TypeError, ValueError):
                v = 0
            return v if 1 <= v <= 65535 else 0

        host = str(body.get("host") or s.dvr_host or "").strip()
        ports = (num("http_port", s.dvr_port), num("media_port", s.dvr_media_port), num("rtsp_port", s.rtsp_port))
        # A blank password means "the one already saved" (which the page never sees) - but only for the
        # DVR it was saved for. Any other address or port must come with its own password, or the test
        # would hand the saved one to whatever answers there.
        same_dvr = bool(s.dvr_host) and (host.lower(), *ports) == (s.dvr_host.lower(), s.dvr_port, s.dvr_media_port, s.rtsp_port)
        password = str(body.get("password") or (s.dvr_password if same_dvr else ""))
        if host and not password and s.dvr_password and 0 not in ports:
            return JSONResponse({"error": "bad_request", "detail": "Enter the DVR password to test a different "
                                 "address or port: the saved password is only used for the saved DVR."}, status_code=400)
        target = Target(host=host, http_port=ports[0], media_port=ports[1], rtsp_port=ports[2],
                        username=str(body.get("username") or s.dvr_user), password=password)
        if not host or not target.password or 0 in (target.http_port, target.media_port, target.rtsp_port):
            return JSONResponse({"error": "bad_request", "detail": "Enter the DVR address, ports and password first."},
                                status_code=400)
        if not valid_host(host):
            return JSONResponse({"error": "bad_request", "detail": "That is not a valid address."}, status_code=400)
        if test_lock.locked() or time.monotonic() - last_test[0] < 3:
            return JSONResponse({"error": "busy", "detail": "A test is already running; try again in a moment."},
                                status_code=429)
        async with test_lock:
            last_test[0] = time.monotonic()
            result = await run_connection_test(target)
        audit(request, "dvr_test", f"{host}: {'ok' if result['ok'] else 'failed'}")
        return result

    # ---- pages -----------------------------------------------------------
    def page(name: str, needs_dvr: bool = False):
        async def handler(request: Request):
            if needs_dvr and not configured:
                user = request.state.user
                if user and user.is_admin:
                    return RedirectResponse("/settings/system?wizard=1", status_code=302)
                return HTMLResponse('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                                    '<link rel="stylesheet" href="/static/style.css"><main><div class="card"><h2>Not set up yet'
                                    '</h2><p>An admin needs to connect DVR Bridge to the DVR first.</p></div></main>',
                                    status_code=503)
            return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})

        return handler

    app.add_api_route("/", page("index.html", True), include_in_schema=False)
    app.add_api_route("/camera/{channel}", page("camera.html", True), include_in_schema=False)
    app.add_api_route("/recordings", page("recordings.html", True), include_in_schema=False)
    app.add_api_route("/settings", page("settings.html", True), include_in_schema=False)
    app.add_api_route("/settings/system", page("system.html"), include_in_schema=False)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app

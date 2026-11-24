"""The ASGI entry point: the app for the current settings, rebuilt in place when they change.

The settings page saves to config.yaml and asks for a reload. The old app is shut down first (go2rtc's
localhost port cannot be held twice), then the new one is started from freshly loaded settings; requests
in that window - normally a second or two - get 503 with Retry-After. If the new settings fail to start,
the previous ones are started again, so a bad change cannot leave the server down.

Run with: uvicorn --factory dvrbridge.server:create_server
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Mapping

from .config import Settings, load
from .main import create_app

log = logging.getLogger("dvrbridge.server")


class ReloadingServer:
    def __init__(self, env: Mapping[str, str] | None = None, factory=create_app) -> None:
        self.env = dict(os.environ if env is None else env)
        self.factory = factory
        self.app = None
        self._ctx = None
        self._settings: Settings | None = None
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task | None = None
        self.reloads = 0

    # ---- ASGI ----------------------------------------------------------------------------
    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self._lifespan(receive, send)
        app = self.app
        if app is None:
            return await self._unavailable(scope, receive, send)
        return await app(scope, receive, send)

    async def _lifespan(self, receive, send):
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self._start(load(self.env))
                except Exception as e:  # noqa: BLE001 - report any start-up failure to the server
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self._pending:
                    self._pending.cancel()
                async with self._lock:
                    await self._stop()
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _unavailable(scope, receive, send):
        if scope["type"] == "websocket":
            if (await receive())["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": 1013})  # try again later
            return
        body = b'{"error":"restarting","detail":"DVR Bridge is applying new settings; try again in a moment."}'
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"application/json"), (b"retry-after", b"2"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    # ---- lifecycle -----------------------------------------------------------------------
    async def _start(self, settings: Settings) -> None:
        app = self.factory(settings, env=self.env, request_reload=self.request_reload)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        self.app, self._ctx, self._settings = app, ctx, settings

    async def _stop(self) -> None:
        ctx, self._ctx, self.app = self._ctx, None, None
        if ctx is not None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.exception("error while stopping the previous app")

    def request_reload(self) -> None:
        """Called from a request handler: reload shortly after the response has gone out."""
        if self._pending and not self._pending.done():
            return

        async def later():
            await asyncio.sleep(0.3)
            await self.reload()

        self._pending = asyncio.get_running_loop().create_task(later(), name="settings-reload")

    async def reload(self) -> bool:
        async with self._lock:
            previous = self._settings
            try:
                new = load(self.env)
            except Exception as e:  # noqa: BLE001
                log.error("new settings do not load, keeping the current ones: %s", e)
                return False
            log.info("applying new settings")
            await self._stop()
            try:
                await self._start(new)
            except Exception:  # noqa: BLE001
                log.exception("new settings failed to start; going back to the previous ones")
                if previous is not None:
                    await self._start(previous)
                return False
            self.reloads += 1
            return True


def create_server() -> ReloadingServer:
    return ReloadingServer()

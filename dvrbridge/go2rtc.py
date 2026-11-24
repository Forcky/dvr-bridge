"""go2rtc: config generation and a small supervisor.

go2rtc is bound to 127.0.0.1 only and every other listener (RTSP server,
WebRTC) is switched off. The app proxies just the MSE websocket the page needs.
The generated config holds the RTSP credentials, so it is written 0600 to the
tmpfs and go2rtc's log output is scrubbed before it reaches our logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import quote

from .config import Settings

log = logging.getLogger("dvrbridge.go2rtc")

STREAM_KINDS = {"main": "0", "sub": "1"}
MAX_CHANNELS = 64


def stream_name(channel: int, kind: str) -> str:
    return f"ch{channel}_{kind}"


def rtsp_url(s: Settings, channel: int, kind: str) -> str:
    u = quote(s.dvr_user, safe="")
    p = quote(s.dvr_password, safe="")
    # '#media=video' drops the audio track (MSE cannot play G.711 anyway).
    return f"rtsp://{u}:{p}@{s.dvr_host}:{s.rtsp_port}/ch{channel:02d}/{STREAM_KINDS[kind]}#media=video"


def build_config(s: Settings) -> str:
    """YAML text (JSON strings are valid YAML scalars, so quoting is safe)."""
    lines = [
        "api:",
        f'  listen: "127.0.0.1:{s.go2rtc_port}"',
        "rtsp:",
        '  listen: ""',
        "webrtc:",
        '  listen: ""',
        "srtp:",
        '  listen: ""',
        "log:",
        "  level: warn",
        "  format: text",
        "streams:",
    ]
    # With channels: 0 (ask the DVR) the count is not known yet; go2rtc connects to a
    # stream only when it is requested, and the app refuses channels the DVR lacks.
    for ch in range(1, (s.channels or MAX_CHANNELS) + 1):
        for kind in STREAM_KINDS:
            lines.append(f"  {stream_name(ch, kind)}: {json.dumps(rtsp_url(s, ch, kind))}")
    return "\n".join(lines) + "\n"


def make_scrubber(s: Settings):
    secrets = {x for x in (s.dvr_password, quote(s.dvr_password, safe="")) if x}
    url_re = re.compile(r"rtsp://[^\s\"'@/]*@")

    def scrub(text: str) -> str:
        text = url_re.sub("rtsp://***@", text)
        for sec in secrets:
            text = text.replace(sec, "***")
        return text

    return scrub


def write_config(s: Settings) -> None:
    fd = os.open(s.go2rtc_config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(build_config(s))


class Go2rtc:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._scrub = make_scrubber(s)
        self.restarts = 0

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        write_config(self.s)
        self._task = asyncio.create_task(self._supervise(), name="go2rtc")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except asyncio.TimeoutError:
                self._proc.kill()

    async def _pump(self, stream: asyncio.StreamReader) -> None:
        async for raw in stream:
            log.info("go2rtc: %s", self._scrub(raw.decode("utf-8", "replace").rstrip()))

    async def _supervise(self) -> None:
        delay = 1.0
        while True:
            self._proc = await asyncio.create_subprocess_exec(
                self.s.go2rtc_bin,
                "-config",
                self.s.go2rtc_config_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            log.info("go2rtc started (pid %s)", self._proc.pid)
            pump = asyncio.create_task(self._pump(self._proc.stdout))
            rc = await self._proc.wait()
            await asyncio.gather(pump, return_exceptions=True)
            self.restarts += 1
            log.warning("go2rtc exited rc=%s; restarting in %.0fs", rc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

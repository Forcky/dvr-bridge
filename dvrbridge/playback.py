"""Recorded-video playback: provider seam, fps estimation and the ffmpeg remux.

A `PlaybackProvider` supplies H.264 (Annex B) frames for one channel and time range.
`open_session` returns a session whose `frames()` yields `VideoFrame(ts_us, data)` and whose
`aclose()` releases the DVR connection; `open_stream` is the plain bytes view of the same thing.

Raw H.264 carries no timing, and the DVR records at about 15 fps (not ffmpeg's default 25), so
the remux is told the frame rate, estimated from the DVR's own frame timestamps. ffmpeg stream
copies into fragmented MP4. Nothing is written to disk: ffmpeg reads and writes pipes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Protocol, Sequence, runtime_checkable

FFMPEG = "ffmpeg"
DEFAULT_FPS = 15.0
FPS_SAMPLE_FRAMES = 60


class PlaybackNotImplemented(Exception):
    """Raised by a provider that cannot serve recorded video."""


class NoFootage(Exception):
    """The DVR accepted the request but sent no video."""


@dataclass(frozen=True)
class VideoFrame:
    ts_us: int
    data: bytes


@dataclass
class PlaybackStats:
    active: int = 0
    started: int = 0
    failed: int = 0
    rejected: int = 0
    bytes_streamed: int = 0
    last_error: str = ""


class PlaybackSession(Protocol):
    def frames(self) -> AsyncIterator[VideoFrame]: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class PlaybackProvider(Protocol):
    stats: PlaybackStats

    async def open_session(
        self, channel: int, start: datetime, end: datetime, stream: str = "Mainstream"
    ) -> PlaybackSession:
        """Open a playback. `start`/`end` are DVR-local wall-clock times; `channel` is 1-based."""
        ...

    def open_stream(
        self, channel: int, start: datetime, end: datetime, stream: str = "Mainstream"
    ) -> AsyncIterator[bytes]:
        """Raw H.264 (Annex B) bytes for the range."""
        ...


class NotImplementedPlayback:
    def __init__(self) -> None:
        self.stats = PlaybackStats()

    async def open_session(self, channel, start, end, stream="Mainstream"):
        raise PlaybackNotImplemented("Recorded-video playback is not implemented.")

    def open_stream(self, channel, start, end, stream="Mainstream"):
        raise PlaybackNotImplemented("Recorded-video playback is not implemented.")


def estimate_fps(timestamps_us: Sequence[int]) -> float | None:
    """Mean frame rate from DVR timestamps (microseconds); None when it cannot be trusted."""
    if len(timestamps_us) < 2:
        return None
    span = timestamps_us[-1] - timestamps_us[0]
    if span <= 0:
        return None
    fps = (len(timestamps_us) - 1) * 1_000_000 / span
    if not 1.0 <= fps <= 60.0:
        return None
    return round(fps, 2)


async def prime(session: PlaybackSession, n: int = FPS_SAMPLE_FRAMES):
    """Read the first frames of a session. Returns (fps, async iterator of H.264 bytes).

    Raises NoFootage if the DVR sends no video. The returned iterator replays the buffered
    frames, then the rest, and closes the session when it finishes or is closed.
    """
    it = session.frames()
    head: list[VideoFrame] = []
    try:
        async for f in it:
            head.append(f)
            if len(head) >= n:
                break
        if not head:
            raise NoFootage("the DVR returned no video for that range")
    except BaseException:
        await it.aclose()
        await session.aclose()
        raise
    fps = estimate_fps([f.ts_us for f in head]) or DEFAULT_FPS

    async def chain() -> AsyncIterator[bytes]:
        try:
            for f in head:
                yield f.data
            async for f in it:
                yield f.data
        finally:
            await it.aclose()
            await session.aclose()

    return fps, chain()


def ffmpeg_remux_args(fps: float | None = None) -> list[str]:
    fps = fps or DEFAULT_FPS
    # -framerate alone is not enough: on the DVR's stream ffmpeg's raw-H.264 timing collapses after the
    # first ~50 frames (timestamps advance by 1 tick, so a 36 s clip played as 3 s). The setts filter
    # stamps every packet at N/fps, which is correct for the DVR's steady frame rate.
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "h264", "-framerate", f"{fps:g}"]
    args += [
        "-i", "pipe:0",
        "-c", "copy",
        "-bsf:v", f"setts=ts=N/({fps:g}*TB)",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    return args


async def remux_to_fmp4(source: AsyncIterator[bytes], fps: float | None = None) -> AsyncIterator[bytes]:
    """Stream-copy raw H.264 into fragmented MP4 through ffmpeg pipes."""
    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_remux_args(fps),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def feed() -> None:
        try:
            async for chunk in source:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    feeder = asyncio.create_task(feed())
    try:
        while True:
            data = await proc.stdout.read(65536)
            if not data:
                break
            yield data
    finally:
        feeder.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        await asyncio.gather(feeder, return_exceptions=True)
        aclose = getattr(source, "aclose", None)
        if aclose:
            try:
                await aclose()
            except Exception:
                pass

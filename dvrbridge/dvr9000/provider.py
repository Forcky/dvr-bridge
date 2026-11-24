"""PlaybackProvider backed by the DVR's port-9000 protocol."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import AsyncIterator

from ..playback import PlaybackStats, VideoFrame
from .client import (
    Dvr9000Error,
    MainSession,
    PlaybackBusy,
    PlaybackRefused,
    PlaybackStalled,
    PlaybackStream,
)

log = logging.getLogger("dvrbridge.playback")


class Dvr9000Session:
    """A open playback: frames() until the end marker, aclose() always releases everything."""

    def __init__(self, provider: "Dvr9000Playback", stream: PlaybackStream) -> None:
        self._provider = provider
        self._stream = stream
        self._closed = False

    async def frames(self) -> AsyncIterator[VideoFrame]:
        try:
            async for f in self._stream.frames():
                yield f
        except Dvr9000Error as e:
            self._provider.stats.failed += 1
            self._provider.stats.last_error = _error_code(e)
            log.warning("playback stream failed: %s", e)
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stream.aclose()


def _error_code(e: Exception) -> str:
    if isinstance(e, PlaybackBusy):
        return "busy"
    if isinstance(e, PlaybackRefused):
        return "dvr_refused"
    if isinstance(e, PlaybackStalled):
        return "dvr_stalled"
    return "dvr_unavailable"


class Dvr9000Playback:
    def __init__(self, session: MainSession, *, max_concurrent: int = 2, stats: PlaybackStats | None = None,
                 timeout: float = 8.0, **stream_kw) -> None:
        self.session = session
        self.max_concurrent = max_concurrent
        self.stats = stats or PlaybackStats()
        self.timeout = timeout
        self._stream_kw = stream_kw

    async def open_session(self, channel: int, start: datetime, end: datetime,
                           stream: str = "Mainstream") -> Dvr9000Session:
        """`channel` is 1-based (as everywhere in DVR Bridge); the DVR wants 0-based."""
        st = self.stats
        if st.active >= self.max_concurrent:
            st.rejected += 1
            st.last_error = "busy"
            raise PlaybackBusy("too many concurrent playbacks")
        st.active += 1
        leased = False
        try:
            sid = await self.session.acquire()
            leased = True
            ps = await PlaybackStream.open(
                self.session.host, self.session.port, sid, channel - 1, start, end,
                self._release, timeout=self.timeout, **self._stream_kw)
        except Dvr9000Error as e:
            st.active -= 1
            if leased:
                self.session.release()
            st.failed += 1
            st.last_error = _error_code(e)
            raise
        except BaseException:
            st.active -= 1
            if leased:
                self.session.release()
            raise
        st.started += 1
        st.last_error = ""
        return Dvr9000Session(self, ps)

    def _release(self, _stream: PlaybackStream) -> None:
        self.stats.active -= 1
        self.session.release()

    async def open_stream(self, channel: int, start: datetime, end: datetime,
                          stream: str = "Mainstream") -> AsyncIterator[bytes]:
        """The seam's raw H.264 (Annex B) iterator. No timing: prefer open_session + remux."""
        sess = await self.open_session(channel, start, end, stream)
        try:
            async for f in sess.frames():
                yield f.data
        finally:
            await sess.aclose()

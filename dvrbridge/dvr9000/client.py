"""Asyncio client for the Swann/RaySharp legacy media protocol (TCP 9000).

See the protocol notes in the README. In short:

* A *main* connection logs in (mode-2 obfuscated 80-byte body), is kept alive with an empty
  type 0x20 message every few seconds, and answers day searches. It yields a session id.
* Each recorded playback uses its own *playback* connection: a type 0x79 open carrying the
  session id, a type 0x7a/0x79 data request, then plain 12-byte-header data packets that
  reassemble into "RSFm" frames (40-byte header + H.264 Annex B).

Toward the DVR this module only ever sends login, keepalive, day search, playback open and
data request. Credentials and the session id are never logged or put in exception text.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import AsyncIterator

from . import codec
from ..playback import VideoFrame

log = logging.getLogger("dvrbridge.dvr9000")

MODE = 2
MSG_LOGIN = 0x1F
MSG_KEEPALIVE = 0x20
MSG_SEARCH = 0x6F
SUB_SEARCH_DAY = 0x65
MSG_PLAYBACK = 0x79
MSG_DATA_REQUEST = 0x7A
SUB_DATA_REQUEST = 0x79

RSFM_HEADER = 40
MAX_CONTROL_PAYLOAD = 0x1000000  # the vendor client caps at 16 MB too
MAX_FRAME = 0x1000000

ACK_TEXT = {3: "admin already logged in", 10: "user already logged in", 11: "no such user",
            12: "bad password", 13: "ip filtered", 14: "user disabled", 15: "must change password",
            18: "bad password (encrypted login required)", 50: "no right", 66: "too many users"}


class Dvr9000Error(Exception):
    """Base class; messages never contain credentials or the session id."""


class Dvr9000Unavailable(Dvr9000Error):
    """Network trouble, timeout or a malformed reply."""


class Dvr9000AuthError(Dvr9000Error):
    """The DVR refused the login. Logins are suspended afterwards so the account cannot lock."""


class PlaybackRefused(Dvr9000Error):
    def __init__(self, ack: int):
        super().__init__(f"playback open refused (ack {ack})")
        self.ack = ack


class PlaybackBusy(Dvr9000Error):
    """Too many concurrent playbacks."""


class PlaybackStalled(Dvr9000Error):
    """The DVR stopped sending data and did not recover after repeated data requests."""


def pack_time(dt: datetime) -> bytes:
    return struct.pack("<HBBBBBB", dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, 0)


def unpack_time(b: bytes) -> datetime | None:
    y, mo, d, h, mi, s, _ = struct.unpack("<HBBBBBB", b[:8])
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


@dataclass
class Frame:
    mode: int
    ack: int
    type: int
    sub: int
    size: int
    seq: int
    body: bytes


class Conn:
    """One TCP connection speaking the 16-byte-header message framing."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.seq = 0

    @classmethod
    async def connect(cls, host: str, port: int, timeout: float = 8.0) -> "Conn":
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise Dvr9000Unavailable(f"cannot connect to the DVR media port: {type(e).__name__}") from None
        return cls(reader, writer)

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        self.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 2.0)
        except Exception:
            pass

    async def send(self, mtype: int, sub: int = 0, body: bytes = b"", ack: int = 0) -> int:
        self.seq += 1
        payload = codec.encode(body) if len(body) > 1 else body
        hdr = struct.pack("<BBHHHII", MODE & 3, 0, ack, mtype, sub, len(payload), self.seq)
        hdr = hdr[:2] + codec.encode(hdr[2:16])
        try:
            self.writer.write(hdr + payload)
            await self.writer.drain()
        except (OSError, ConnectionError) as e:
            raise Dvr9000Unavailable(f"send failed: {type(e).__name__}") from None
        return self.seq

    async def recv(self) -> Frame:
        try:
            raw = await self.reader.readexactly(16)
            mode = raw[0] & 3
            if mode == 2:
                raw = raw[:2] + codec.decode(raw[2:16])
            b0, _b1, ack, mtype, sub, size, seq = struct.unpack("<BBHHHII", raw)
            ext = (b0 >> 2) * 4
            if ext:
                await self.reader.readexactly(ext)
            if size > MAX_CONTROL_PAYLOAD:
                raise Dvr9000Unavailable("implausible message size")
            body = await self.reader.readexactly(size) if size else b""
        except (asyncio.IncompleteReadError, OSError, ConnectionError) as e:
            raise Dvr9000Unavailable(f"connection closed: {type(e).__name__}") from None
        if mode == 2 and len(body) > 1:
            body = codec.decode(body)
        return Frame(mode, ack, mtype, sub, size, seq, body)


def login_body(user: str, password: str, lang: int = 2) -> bytes:
    return (user.encode().ljust(36, b"\0")[:36] + password.encode().ljust(36, b"\0")[:36]
            + struct.pack("<II", 0, (lang << 24) | 1))


class MainSession:
    """The single shared main connection: lazy login, keepalive, re-login after a drop.

    `acquire()` returns the session id and takes a lease; while any lease is held the session is
    never idle-closed. `release()` gives the lease back. Re-login attempts are rate limited, and
    after the DVR refuses a login no further attempt is made for `auth_lockout` seconds.
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        *,
        keepalive: float = 3.0,
        idle_close: float = 300.0,
        min_login_interval: float = 5.0,
        auth_lockout: float = 900.0,
        timeout: float = 8.0,
    ) -> None:
        self.host, self.port = host, port
        self._user, self._password = user, password
        self.keepalive = keepalive
        self.idle_close = idle_close
        self.min_login_interval = min_login_interval
        self.auth_lockout = auth_lockout
        self.timeout = timeout
        self._conn: Conn | None = None
        self._session_id = 0
        self._pending: dict[tuple[int, int], asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._req_lock = asyncio.Lock()
        self._leases = 0
        self._last_use = time.monotonic()
        self._last_rx = time.monotonic()
        self._last_login_attempt = -1e9
        self._locked_until = 0.0
        self.logins = 0

    # -- state -----------------------------------------------------------------------------
    @property
    def up(self) -> bool:
        return self._conn is not None

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn:
            conn.close()
        for t in self._tasks:
            if t is not asyncio.current_task():
                t.cancel()
        self._tasks = []
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(Dvr9000Unavailable("main session closed"))
        self._pending.clear()

    async def close(self) -> None:
        self._drop()
        await asyncio.sleep(0)

    # -- login -----------------------------------------------------------------------------
    async def _login(self) -> None:
        now = time.monotonic()
        if now < self._locked_until:
            raise Dvr9000AuthError("DVR media login suspended after a refusal")
        if now - self._last_login_attempt < self.min_login_interval:
            raise Dvr9000Unavailable("DVR media login rate limited, retry shortly")
        self._last_login_attempt = now
        conn = await Conn.connect(self.host, self.port, self.timeout)
        try:
            seq = await conn.send(MSG_LOGIN, 0, login_body(self._user, self._password))
            reply = None
            for _ in range(50):
                f = await asyncio.wait_for(conn.recv(), self.timeout)
                if f.type == MSG_LOGIN and f.seq == seq:
                    reply = f
                    break
            if reply is None:
                raise Dvr9000Unavailable("no login reply")
        except (asyncio.TimeoutError, Dvr9000Unavailable) as e:
            await conn.aclose()
            if isinstance(e, asyncio.TimeoutError):
                raise Dvr9000Unavailable("login timed out") from None
            raise
        except BaseException:
            conn.close()
            raise
        if reply.ack != 0:
            await conn.aclose()
            self._locked_until = time.monotonic() + (60.0 if reply.ack == 66 else self.auth_lockout)
            raise Dvr9000AuthError(f"login refused: ack {reply.ack} ({ACK_TEXT.get(reply.ack, '?')})")
        if len(reply.body) < 0xA4:
            await conn.aclose()
            raise Dvr9000Unavailable("login reply too short")
        self._session_id = struct.unpack_from("<I", reply.body, 0xA0)[0]
        self._conn = conn
        self._last_rx = time.monotonic()
        self.logins += 1
        self._tasks = [asyncio.create_task(self._reader(conn), name="dvr9000-reader"),
                       asyncio.create_task(self._keepalive(conn), name="dvr9000-keepalive")]
        log.info("DVR media session established")

    async def _reader(self, conn: Conn) -> None:
        try:
            while True:
                f = await conn.recv()
                self._last_rx = time.monotonic()
                fut = self._pending.pop((f.type, f.seq), None)
                if fut and not fut.done():
                    fut.set_result(f)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._conn is conn:
                log.warning("DVR media session lost: %s", e)
                self._drop()

    async def _keepalive(self, conn: Conn) -> None:
        try:
            while True:
                await asyncio.sleep(self.keepalive)
                if self._conn is not conn:
                    return
                now = time.monotonic()
                if self._leases == 0 and now - self._last_use > self.idle_close:
                    log.info("DVR media session idle, closing")
                    self._drop()
                    return
                if now - self._last_rx > max(5 * self.keepalive, 15.0):
                    log.warning("DVR media session silent, dropping")
                    self._drop()
                    return
                await conn.send(MSG_KEEPALIVE)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self._conn is conn:
                log.warning("DVR media keepalive failed: %s", e)
                self._drop()

    async def acquire(self) -> int:
        """Ensure a live session and take a lease. Returns the session id."""
        self._leases += 1
        self._last_use = time.monotonic()
        try:
            async with self._lock:
                if self._conn is None:
                    await self._login()
                return self._session_id
        except BaseException:
            self._leases -= 1
            raise

    def release(self) -> None:
        self._leases = max(0, self._leases - 1)
        self._last_use = time.monotonic()

    # -- requests --------------------------------------------------------------------------
    async def request(self, mtype: int, sub: int, body: bytes = b"") -> Frame:
        await self.acquire()
        try:
            conn = self._conn
            if conn is None:
                raise Dvr9000Unavailable("main session closed")
            async with self._req_lock:
                fut = asyncio.get_running_loop().create_future()
                seq = conn.seq + 1
                self._pending[(mtype, seq)] = fut
                try:
                    await conn.send(mtype, sub, body)
                    return await asyncio.wait_for(fut, self.timeout)
                except asyncio.TimeoutError:
                    raise Dvr9000Unavailable("request timed out") from None
                finally:
                    self._pending.pop((mtype, seq), None)
        finally:
            self.release()

    async def search_day(self, channel: int, day: date, rec_type: int = 0xFFFFFFFF) -> list[dict]:
        """Day search on the 0-based channel. Returns [{start, end, size}] (DVR local times)."""
        begin = datetime(day.year, day.month, day.day, 0, 0, 0)
        end = datetime(day.year, day.month, day.day, 23, 59, 59)
        body = (struct.pack("<I", channel) + pack_time(begin) + pack_time(end)
                + struct.pack("<I", rec_type)).ljust(136, b"\0")
        f = await self.request(MSG_SEARCH, SUB_SEARCH_DAY, body)
        out: list[dict] = []
        off = 0
        while off + 4 <= len(f.body):
            (rlen,) = struct.unpack_from("<I", f.body, off)
            if rlen < 28 or off + rlen > len(f.body):
                break
            r = f.body[off:off + rlen]
            out.append({"start": unpack_time(r[4:12]), "end": unpack_time(r[12:20]),
                        "size": struct.unpack_from("<I", r, 24)[0]})
            off += rlen
        return out


def is_annexb(data: bytes) -> bool:
    return data[:4] == b"\0\0\0\1" or data[:3] == b"\0\0\1"


def parse_rsfm(frame: bytes) -> VideoFrame | None:
    """A reassembled RSFm frame -> video frame, or None for anything that is not H.264 video."""
    if frame[:4] != b"RSFm" or len(frame) <= RSFM_HEADER:
        return None
    data = frame[RSFM_HEADER:]
    if not is_annexb(data):
        return None
    return VideoFrame(struct.unpack_from("<Q", frame, 0x10)[0], data)


class PlaybackStream:
    """One recorded playback on its own socket. Always `aclose()` it (the provider does)."""

    def __init__(self, conn: Conn, on_close, *, stall_timeout: float = 2.0, max_stalls: int = 4) -> None:
        self.conn = conn
        self._on_close = on_close
        self.stall_timeout = stall_timeout
        self.max_stalls = max_stalls
        self.closed = False
        self.ended = False  # saw the end-of-clip marker
        self.frames_seen = 0

    @classmethod
    async def open(cls, host: str, port: int, session_id: int, channel: int, start: datetime,
                   end: datetime, on_close, *, timeout: float = 8.0, **kw) -> "PlaybackStream":
        conn = await Conn.connect(host, port, timeout)
        self = cls(conn, on_close, **kw)
        try:
            # 0xFFFFFFFF record-type mask: with 0 the DVR accepts the open but streams nothing.
            body = (struct.pack("<III", session_id, channel, 0xFFFFFFFF) + pack_time(start) + pack_time(end)
                    ).ljust(0x44, b"\0")
            await conn.send(MSG_PLAYBACK, 0, body)
            try:
                reply = await asyncio.wait_for(conn.recv(), timeout)
            except asyncio.TimeoutError:
                raise Dvr9000Unavailable("playback open timed out") from None
            if reply.ack != 0:
                raise PlaybackRefused(reply.ack)
            await conn.send(MSG_DATA_REQUEST, SUB_DATA_REQUEST)
        except BaseException:
            self.closed = True  # never opened: the caller releases its own reservations
            await conn.aclose()
            raise
        return self

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.conn.close()  # synchronous part first: safe even if we are being cancelled
        try:
            self._on_close(self)
        finally:
            try:
                await asyncio.wait_for(self.conn.writer.wait_closed(), 2.0)
            except BaseException:
                pass

    async def _read_packet(self):
        """-> (flags, count, fsize, idx, payload) of the next data packet, or None on end of clip."""
        r = self.conn.reader
        stalls = 0
        while True:
            try:
                hdr = await asyncio.wait_for(r.readexactly(12), self.stall_timeout)
            except asyncio.TimeoutError:
                stalls += 1
                if stalls > self.max_stalls:
                    raise PlaybackStalled("DVR stopped sending data") from None
                await self.conn.send(MSG_DATA_REQUEST, SUB_DATA_REQUEST)
                continue
            except (asyncio.IncompleteReadError, OSError, ConnectionError):
                if self.frames_seen:
                    return None  # DVR closed the socket after the data: treat as the end
                raise Dvr9000Unavailable("playback connection closed") from None
            stalls = 0
            b0, flags, count, fsize, idx, plen = struct.unpack("<BBHIHH", hdr)
            if (b0 & 3) == 2 and count == 0:
                # a 16-byte control message interleaved on the data socket: skip its last 4 bytes
                await r.readexactly(4)
                continue
            try:
                if b0 & 0xFC:
                    await r.readexactly(b0 & 0xFC)
                payload = await r.readexactly(plen) if plen else b""
            except (asyncio.IncompleteReadError, OSError, ConnectionError):
                raise Dvr9000Unavailable("playback connection closed mid-packet") from None
            if count == 0 and flags == 0x02:
                self.ended = True
                return None
            if count == 0:
                continue
            return flags, count, fsize, idx, payload

    async def frames(self) -> AsyncIterator[VideoFrame]:
        """Video frames until the end-of-clip marker. Non-video frames are skipped."""
        cur: tuple[int, int, dict[int, bytes]] | None = None
        held = 0  # bytes buffered for the frame being reassembled
        while True:
            pkt = await self._read_packet()
            if pkt is None:
                return
            _flags, count, fsize, idx, payload = pkt
            if fsize > MAX_FRAME:
                raise Dvr9000Unavailable("implausible frame size")
            if idx >= count:
                continue  # not a fragment of this frame; it could never complete one
            if cur is None or cur[0] != fsize or cur[1] != count or (idx == 0 and cur[2]):
                cur, held = (fsize, count, {}), 0
            held += len(payload) - len(cur[2].get(idx, b""))
            # Whatever the header claims, never buffer more than one maximal frame.
            if held > MAX_FRAME:
                raise Dvr9000Unavailable("implausible frame size")
            cur[2][idx] = payload
            if len(cur[2]) == count:
                try:
                    whole = b"".join(cur[2][i] for i in range(count))
                except KeyError:
                    cur = None
                    continue
                cur = None
                self.frames_seen += 1
                vf = parse_rsfm(whole)
                if vf is not None:
                    yield vf

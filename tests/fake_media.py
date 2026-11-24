"""A stand-in for the DVR's port-9000 media protocol (login, keepalive, search, playback)."""
from __future__ import annotations

import asyncio
import struct

from dvrbridge.dvr9000 import codec

USER, PASSWORD = "tester", "s3cret-pw"
SESSION_ID = 0x1234ABCD


async def read_msg(reader: asyncio.StreamReader):
    raw = await reader.readexactly(16)
    if raw[0] & 3 == 2:
        raw = raw[:2] + codec.decode(raw[2:16])
    b0, _b1, ack, mtype, sub, size, seq = struct.unpack("<BBHHHII", raw)
    body = await reader.readexactly(size) if size else b""
    if b0 & 3 == 2 and len(body) > 1:
        body = codec.decode(body)
    return mtype, sub, ack, seq, body


def msg(mtype: int, sub: int, seq: int, body: bytes = b"", ack: int = 0, mode: int = 2) -> bytes:
    payload = codec.encode(body) if mode == 2 and len(body) > 1 else body
    hdr = struct.pack("<BBHHHII", mode, 0, ack, mtype, sub, len(payload), seq)
    if mode == 2:
        hdr = hdr[:2] + codec.encode(hdr[2:16])
    return hdr + payload


def rsfm(ts_us: int, payload: bytes, w=1920, h=1080, typ=0x149) -> bytes:
    hdr = bytearray(40)
    hdr[0:4] = b"RSFm"
    struct.pack_into("<II", hdr, 4, typ, 40 + len(payload))
    struct.pack_into("<Q", hdr, 0x10, ts_us)
    struct.pack_into("<HH", hdr, 0x20, w, h)
    return bytes(hdr) + payload


def packets(frame: bytes, mtu: int = 1400) -> list[bytes]:
    chunks = [frame[i:i + mtu] for i in range(0, len(frame), mtu)] or [b""]
    return [struct.pack("<BBHIHH", 0, 0, len(chunks), len(frame), i, len(c)) + c for i, c in enumerate(chunks)]


def end_marker() -> bytes:
    return struct.pack("<BBHIHH", 0, 0x02, 0, 0, 0, 0)


class FakeMediaDvr:
    """Records what the gateway does so tests can assert on it."""

    def __init__(self, h264_units: list[bytes] | None = None, fps: float = 15.0, nframes: int = 12,
                 open_ack: int = 0, hold_after: int | None = None, drop_data_requests: int = 0,
                 login_ack: int = 0):
        self.units = h264_units or [b"\0\0\0\1" + bytes([0x65 if i == 0 else 0x41]) + bytes(range(256)) * 12
                                    for i in range(nframes)]
        self.fps = fps
        self.open_ack = open_ack
        self.login_ack = login_ack
        self.hold_after = hold_after  # stop sending after N frames, never send the end marker
        self.drop_data_requests = drop_data_requests  # ignore this many 0x7a requests (simulated stall)
        self.logins = 0
        self.keepalives = 0
        self.opens: list[dict] = []
        self.data_requests = 0
        self.playback_open = 0
        self.playback_closed = 0
        self.bad_logins = 0
        self.server: asyncio.Server | None = None
        self.port = 0
        self.t0 = 1_763_820_287_000_000

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            mtype, sub, ack, seq, body = await read_msg(reader)
            if mtype == 0x1F:
                await self._main(reader, writer, seq, body)
            elif mtype == 0x79:
                await self._playback(reader, writer, seq, body)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _main(self, reader, writer, seq, body):
        user = body[:36].rstrip(b"\0").decode()
        pw = body[36:72].rstrip(b"\0").decode()
        flags = struct.unpack_from("<I", body, 76)[0]
        if self.login_ack or user != USER or pw != PASSWORD or flags != 0x02000001 or len(body) != 80:
            self.bad_logins += 1
            writer.write(msg(0x1F, 0, seq, b"", ack=self.login_ack or 11))
            await writer.drain()
            return
        self.logins += 1
        reply = bytearray(1232)
        reply[6:8] = b"SR"
        struct.pack_into("<I", reply, 0xA0, SESSION_ID)
        writer.write(msg(0x1F, 0, seq, bytes(reply)))
        await writer.drain()
        while True:
            mtype, sub, ack, seq, body = await read_msg(reader)
            if mtype == 0x20:
                self.keepalives += 1
                writer.write(msg(0x20, 0, seq))
            elif mtype == 0x6F and sub == 0x65:
                rec = struct.pack("<I", 28) + body[4:12] + body[12:20] + struct.pack("<HHI", 0, 1, 999)
                writer.write(msg(0x6F, 0x65, seq, rec * 2))
            await writer.drain()

    async def _playback(self, reader, writer, seq, body):
        self.playback_open += 1
        sender = None
        try:
            sid, ch, mask = struct.unpack_from("<III", body, 0)
            self.opens.append({"session": sid, "channel": ch, "mask": mask, "begin": body[12:20],
                               "end": body[20:28], "len": len(body)})
            ok = self.open_ack == 0 and sid == SESSION_ID and mask != 0
            writer.write(msg(0x79, 0, seq, b"", ack=0 if ok else (self.open_ack or 1)))
            await writer.drain()
            if not ok:
                await reader.read()
                return
            async def send_all():
                for i, u in enumerate(self.units):
                    if self.hold_after is not None and i >= self.hold_after:
                        await asyncio.sleep(3600)
                    ts = self.t0 + int(i * 1_000_000 / self.fps)
                    if i == 3:  # an audio-ish frame the gateway must skip
                        for p in packets(rsfm(ts, b"\x01\x02\x03\x04" * 50, typ=0x2)):
                            writer.write(p)
                    for p in packets(rsfm(ts, u)):
                        writer.write(p)
                    await writer.drain()
                    await asyncio.sleep(0)
                for _ in range(5):
                    writer.write(end_marker())
                await writer.drain()

            while True:
                mtype, sub, ack, seq, body = await read_msg(reader)
                if mtype == 0x7A:
                    self.data_requests += 1
                    if self.drop_data_requests > 0:
                        self.drop_data_requests -= 1
                        continue
                    if sender is None:
                        sender = asyncio.create_task(send_all())
        finally:
            if sender is not None:
                sender.cancel()
            self.playback_closed += 1

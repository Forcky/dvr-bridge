"""Cached DVR status (polled lightly in the background) and /metrics rendering.

/metrics and the UI read this cache; scraping never causes DVR traffic."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace

from .dvr import ChannelInfo, DiskInfo, DvrClient, DvrError, EventState
from .dvr.models import ChannelRecordState
from .playback import PlaybackStats

log = logging.getLogger("dvrbridge.state")


@dataclass
class Snapshot:
    up: bool = False
    last_success_age: float | None = None
    channels: list[ChannelInfo] = field(default_factory=list)
    disks: list[DiskInfo] = field(default_factory=list)
    records: list[ChannelRecordState] = field(default_factory=list)
    events: EventState | None = None
    go2rtc_up: bool = False
    playback: PlaybackStats | None = None
    media_session_up: bool | None = None


class Monitor:
    def __init__(self, dvr: DvrClient, interval: float = 60.0, channel_refresh: float = 600.0) -> None:
        self.dvr = dvr
        self.interval = interval
        self.channel_refresh = channel_refresh
        self.up = False
        self.channels: list[ChannelInfo] = []
        self.disks: list[DiskInfo] = []
        self.records: list[ChannelRecordState] = []
        self.events: EventState | None = None
        self._channels_at = 0.0
        self._task: asyncio.Task | None = None

    async def refresh(self) -> None:
        try:
            now = time.monotonic()
            if not self.channels or now - self._channels_at > self.channel_refresh:
                self.channels = await self.dvr.channel_info()
                self._channels_at = now
            self.records = await self.dvr.record_state()
            self.events = await self.dvr.event_check()
            self.disks = await self.dvr.disk_info()
            self.up = True
        except DvrError as e:
            self.up = False
            log.warning("DVR poll failed: %s", e)

    async def refresh_channels(self, renamed: tuple[int, str] | None = None) -> None:
        """Re-read the channel list now (after a rename) instead of waiting for the 10-minute refresh.
        ``renamed`` is applied to the cache first so the new name shows even if the re-read fails."""
        if renamed is not None:
            n, name = renamed
            self.channels = [replace(c, name=name) if c.number == n else c for c in self.channels]
        try:
            self.channels = await self.dvr.channel_info()
            self._channels_at = time.monotonic()
        except DvrError as e:
            log.warning("channel list refresh failed: %s", e)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="dvr-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.interval if self.up else min(self.interval, 30.0))

    def snapshot(self, go2rtc_up: bool = False, playback: PlaybackStats | None = None,
                 media_session_up: bool | None = None) -> Snapshot:
        return Snapshot(
            up=self.up,
            last_success_age=self.dvr.last_success_age,
            channels=list(self.channels),
            disks=list(self.disks),
            records=list(self.records),
            events=self.events,
            go2rtc_up=go2rtc_up,
            playback=playback,
            media_session_up=media_session_up,
        )

    def channel_names(self, count: int) -> dict[int, str]:
        names = {c.number: c.name for c in self.channels}
        return {n: names.get(n, f"Channel {n}") for n in range(1, count + 1)}


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(s: Snapshot) -> str:
    out: list[str] = []

    def metric(name: str, help_: str, typ: str = "gauge") -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {typ}")

    def sample(metric_name: str, value: float | int, /, **labels: str) -> None:
        lab = ",".join(f'{k}="{_esc(str(v))}"' for k, v in labels.items())
        out.append(f"{metric_name}{{{lab}}} {value}" if lab else f"{metric_name} {value}")

    metric("dvr_up", "1 if the last poll of the DVR API succeeded.")
    sample("dvr_up", int(s.up))
    metric("dvr_last_success_age_seconds", "Seconds since the last successful DVR API call (-1 if never).")
    sample("dvr_last_success_age_seconds", -1 if s.last_success_age is None else round(s.last_success_age, 1))

    metric("dvr_disk_info", "Disk status as a label; value is always 1.")
    for d in s.disks:
        sample("dvr_disk_info", 1, disk=d.id, status=d.status)
    metric("dvr_disk_total_bytes", "Disk capacity in bytes.")
    for d in s.disks:
        sample("dvr_disk_total_bytes", d.total_mb * 1024 * 1024, disk=d.id)
    metric("dvr_disk_free_bytes", "Free disk space in bytes (0 while the DVR is overwriting a full disk).")
    for d in s.disks:
        sample("dvr_disk_free_bytes", d.free_mb * 1024 * 1024, disk=d.id)
    metric("dvr_hdd_alarm", "1 if the DVR reports a disk alarm other than Ok.")
    if s.events:
        sample("dvr_hdd_alarm", int(s.events.hdd_alarm not in ("Ok", "Unknown")))

    metric("dvr_channel_record_switch", "1 if recording is enabled for the channel.")
    for r in s.records:
        sample("dvr_channel_record_switch", int(r.switch_enabled), channel=r.number)
    metric("dvr_channel_recording", "1 if the channel is recording right now.")
    for r in s.records:
        sample("dvr_channel_recording", int(r.recording), channel=r.number)
    metric("dvr_channel_videoloss", "1 if the channel reports video loss.")
    if s.events:
        for c in s.events.channels:
            sample("dvr_channel_videoloss", int(c.videoloss), channel=c.number)
    metric("dvr_channel_online", "1 if the camera is connected to the DVR.")
    for c in s.channels:
        sample("dvr_channel_online", int(c.online), channel=c.number)
    # Info metric: keeps the camera name off the other series (a rename would split their
    # history); alerts join it with `* on(channel) group_left(name) dvr_channel_info`.
    metric("dvr_channel_info", "Camera name for a channel; value is always 1.")
    for c in s.channels:
        sample("dvr_channel_info", 1, channel=c.number, name=c.name)

    metric("dvrbridge_go2rtc_up", "1 if the bundled go2rtc process is running.")
    sample("dvrbridge_go2rtc_up", int(s.go2rtc_up))
    if s.playback is not None:
        p = s.playback
        metric("dvrbridge_playback_active", "Recorded playbacks and exports streaming right now.")
        sample("dvrbridge_playback_active", p.active)
        metric("dvrbridge_playback_started_total", "Recorded playbacks the DVR accepted.", "counter")
        sample("dvrbridge_playback_started_total", p.started)
        metric("dvrbridge_playback_failed_total", "Recorded playbacks that failed (refused, unreachable or stalled).", "counter")
        sample("dvrbridge_playback_failed_total", p.failed)
        metric("dvrbridge_playback_rejected_total", "Playback requests refused locally because of the concurrency limit.", "counter")
        sample("dvrbridge_playback_rejected_total", p.rejected)
        metric("dvrbridge_playback_bytes_total", "Bytes of recorded video sent to clients.", "counter")
        sample("dvrbridge_playback_bytes_total", p.bytes_streamed)
    if s.media_session_up is not None:
        metric("dvrbridge_dvr_media_session_up", "1 if the shared port-9000 media session is logged in.")
        sample("dvrbridge_dvr_media_session_up", int(s.media_session_up))
    return "\n".join(out) + "\n"

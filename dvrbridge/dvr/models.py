"""Typed views of the DVR's JSON responses. Parsing is tolerant: missing keys
become safe defaults so one odd field cannot take the whole page down."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

DVR_DATE_FMT = "%m/%d/%Y"
DVR_TIME_FMT = "%H:%M:%S"


def channel_number(name: str) -> int:
    """'CH3' -> 3."""
    digits = "".join(c for c in str(name) if c.isdigit())
    return int(digits) if digits else 0


@dataclass(frozen=True)
class DeviceInfo:
    device_type: str
    channel_count: int
    mac: str

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "DeviceInfo":
        return cls(
            device_type=str(data.get("device_type", "")),
            channel_count=int(data.get("channel_num", 0) or 0),
            mac=str(data.get("mac_addr", "")),
        )


@dataclass(frozen=True)
class ChannelInfo:
    number: int
    name: str
    online: bool
    videoloss: bool

    @classmethod
    def parse_list(cls, data: dict[str, Any]) -> list["ChannelInfo"]:
        items = (data.get("channel_param") or {}).get("items") or []
        out = []
        for it in items:
            num = channel_number(it.get("channel", ""))
            # channel_name follows the OSD name (verified on the real DVR: renaming via OSD/Set changes it);
            # channel_alias stays empty there, so it is only a fallback.
            name = (it.get("channel_name") or it.get("channel_alias") or f"Channel {num}").strip()
            out.append(
                cls(
                    number=num,
                    name=name,
                    online=str(it.get("connect_status", "")).lower() == "online",
                    videoloss=bool(it.get("videoloss", False)),
                )
            )
        return sorted(out, key=lambda c: c.number)


@dataclass(frozen=True)
class DiskInfo:
    id: int
    status: str
    total_mb: int
    free_mb: int

    @classmethod
    def parse_list(cls, data: dict[str, Any]) -> list["DiskInfo"]:
        return [
            cls(
                id=int(d.get("id", 0) or 0),
                status=str(d.get("status", "Unknown")),
                total_mb=int(d.get("total_size", 0) or 0),
                free_mb=int(d.get("free_size", 0) or 0),
            )
            for d in data.get("disk_info") or []
        ]


@dataclass(frozen=True)
class ChannelRecordState:
    number: int
    recording: bool
    switch_enabled: bool


def parse_record_state(data: dict[str, Any]) -> list[ChannelRecordState]:
    out = []
    for name, v in (data.get("channel_info") or {}).items():
        out.append(
            ChannelRecordState(
                number=channel_number(name),
                recording=str(v.get("record_state", "")).lower() == "on",
                switch_enabled=str(v.get("record_switch", "")).lower() == "enable",
            )
        )
    return sorted(out, key=lambda r: r.number)


@dataclass(frozen=True)
class ChannelEvent:
    number: int
    motion: bool
    videoloss: bool


@dataclass(frozen=True)
class EventState:
    hdd_alarm: str
    channels: list[ChannelEvent] = field(default_factory=list)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "EventState":
        alarms = data.get("alarm_list") or []
        if not alarms:
            return cls(hdd_alarm="Unknown")
        a = alarms[0]
        hdd = "Unknown"
        for sa in a.get("system_alarm") or []:
            hdd = str((sa.get("hdd") or {}).get("hdd_alarm_type", hdd))
        chans = [
            ChannelEvent(
                number=channel_number(c.get("channel", "")),
                motion=bool(c.get("motion_alarm", False)),
                videoloss=bool(c.get("videoloss", False)),
            )
            for c in a.get("channel_alarm") or []
        ]
        return cls(hdd_alarm=hdd, channels=sorted(chans, key=lambda c: c.number))


@dataclass(frozen=True)
class Clip:
    channel: int
    start: datetime  # DVR-local wall clock, naive
    end: datetime
    size: int
    record_id: int
    disk_event_id: int
    stream: str

    @property
    def duration_s(self) -> int:
        return max(0, int((self.end - self.start).total_seconds()))

    @classmethod
    def parse(cls, d: dict[str, Any]) -> "Clip":
        def dt(date_key: str, time_key: str) -> datetime:
            return datetime.strptime(f"{d[date_key]} {d[time_key]}", f"{DVR_DATE_FMT} {DVR_TIME_FMT}")

        return cls(
            channel=channel_number(d.get("channel", "")),
            start=dt("start_date", "start_time"),
            end=dt("end_date", "end_time"),
            size=int(d.get("size", 0) or 0),
            record_id=int(d.get("record_id", 0) or 0),
            disk_event_id=int(d.get("disk_event_id", 0) or 0),
            stream=str(d.get("stream_mode", "Mainstream")),
        )


def parse_search(data: dict[str, Any]) -> list[Clip]:
    """The DVR returns data.record as a list of per-channel lists. Be lenient
    about nesting and skip malformed entries rather than failing the day."""
    clips: list[Clip] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict) and "start_time" in node:
            try:
                clips.append(Clip.parse(node))
            except (KeyError, ValueError):
                pass

    walk(data.get("record"))
    clips.sort(key=lambda c: (c.channel, c.start))
    return clips

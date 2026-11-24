from dvrbridge.dvr.models import ChannelInfo, DiskInfo, EventState
from dvrbridge.state import Monitor, Snapshot, render_metrics
from tests.conftest import fixture


async def test_metrics_from_fixtures(make_client):
    c = make_client()
    mon = Monitor(c)
    await mon.refresh()
    text = render_metrics(mon.snapshot(go2rtc_up=True))
    assert "dvr_up 1\n" in text
    assert 'dvr_disk_info{disk="1",status="Full"} 1' in text
    assert 'dvr_disk_total_bytes{disk="1"} 1000255 ' not in text  # sanity: value is bytes
    assert f'dvr_disk_total_bytes{{disk="1"}} {953869 * 1024 * 1024}' in text
    assert 'dvr_disk_free_bytes{disk="1"} 0' in text
    for n in range(1, 9):
        assert f'dvr_channel_record_switch{{channel="{n}"}} 1' in text
        assert f'dvr_channel_videoloss{{channel="{n}"}} 0' in text
        assert f'dvr_channel_recording{{channel="{n}"}} 0' in text
    assert "dvr_last_success_age_seconds " in text and "dvr_last_success_age_seconds -1" not in text
    assert "dvrbridge_go2rtc_up 1" in text
    assert "dvr_hdd_alarm 0" in text
    await c.close()


def test_metrics_when_never_connected():
    text = render_metrics(Snapshot())
    assert "dvr_up 0\n" in text
    assert "dvr_last_success_age_seconds -1\n" in text
    assert "dvr_channel_videoloss{" not in text


def test_metrics_videoloss_and_alarm():
    ev = EventState.parse(fixture("event_check.json")["data"])
    ev.channels[2] = type(ev.channels[2])(ev.channels[2].number, False, True)
    s = Snapshot(up=True, events=EventState("Error", ev.channels),
                 disks=[DiskInfo(1, "Normal", 100, 50)], channels=[ChannelInfo(1, "x", False, False)])
    text = render_metrics(s)
    assert 'dvr_channel_videoloss{channel="3"} 1' in text
    assert "dvr_hdd_alarm 1" in text and 'dvr_channel_online{channel="1"} 0' in text
    # every sample line is valid exposition format
    for line in text.splitlines():
        assert line.startswith("#") or len(line.rsplit(" ", 1)) == 2


def test_metrics_channel_info_carries_escaped_name():
    s = Snapshot(up=True, channels=[ChannelInfo(1, 'Front "door"', True, False), ChannelInfo(2, "Drive", True, False)])
    text = render_metrics(s)
    assert 'dvr_channel_info{channel="1",name="Front \\"door\\""} 1' in text
    assert 'dvr_channel_info{channel="2",name="Drive"} 1' in text

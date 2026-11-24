import yaml

from dvrbridge.config import Settings
from dvrbridge.go2rtc import build_config, make_scrubber, rtsp_url


def settings(**kw):
    base = dict(dvr_host="192.0.2.10", dvr_user="admin", dvr_password="p@ss/w:rd", secret_key="k" * 32, channels=8)
    base.update(kw)
    return Settings(**base)


def test_config_has_16_streams_and_valid_yaml():
    cfg = yaml.safe_load(build_config(settings()))
    assert len(cfg["streams"]) == 16
    assert cfg["streams"]["ch1_main"].startswith("rtsp://admin:p%40ss%2Fw%3Ard@192.0.2.10:554/ch01/0")
    assert "/ch08/1" in cfg["streams"]["ch8_sub"]
    assert cfg["streams"]["ch3_sub"].endswith("#media=video")


def test_only_localhost_api_and_other_listeners_off():
    cfg = yaml.safe_load(build_config(settings()))
    assert cfg["api"]["listen"] == "127.0.0.1:1984"
    assert cfg["rtsp"]["listen"] == "" and cfg["webrtc"]["listen"] == "" and cfg["srtp"]["listen"] == ""


def test_channel_count_and_host_from_settings():
    cfg = yaml.safe_load(build_config(settings(channels=4, dvr_host="10.0.0.5", rtsp_port=8554)))
    assert len(cfg["streams"]) == 8
    assert "@10.0.0.5:8554/ch04/0" in cfg["streams"]["ch4_main"]


def test_log_scrubber_hides_credentials():
    s = settings()
    scrub = make_scrubber(s)
    line = f'src={rtsp_url(s, 1, "main")} failed; pw p@ss/w:rd and p%40ss%2Fw%3Ard'
    out = scrub(line)
    assert "admin" not in out and "p@ss" not in out and "p%40ss" not in out
    assert "rtsp://***@" in out

def test_channels_zero_lists_every_possible_channel():
    from dvrbridge.go2rtc import MAX_CHANNELS

    cfg = yaml.safe_load(build_config(settings(channels=0)))
    assert len(cfg["streams"]) == 2 * MAX_CHANNELS

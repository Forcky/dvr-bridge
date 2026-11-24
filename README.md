# DVR Bridge

A modern browser interface for older RaySharp-based DVRs - the hardware sold under brands such as
Swann - whose own web UI needs a long-dead browser plugin (ActiveX/NPAPI).

DVR Bridge runs as one small Docker container next to your DVR and gives you, in any current browser
on desktop or phone:

- **Live view**: a grid of every camera, click one for its full-resolution stream. No transcoding: the
  DVR's H.264 is passed straight through (go2rtc + Media Source Extensions over a WebSocket).
- **Recordings**: pick a day, see every camera's recordings on one zoomable timeline, play a clip in the
  page or **download it as MP4**.
- **Camera names**: rename cameras from a settings page; the names are stored on the DVR itself, so its
  on-screen overlay and its own monitor output follow.
- **Monitoring**: `/healthz` and Prometheus `/metrics` (DVR reachable, disk status, per-camera
  recording/video-loss state, playback counters).

The DVR stays the recorder. **DVR Bridge stores no footage**: clips stream from the DVR through the
container to your browser, and nothing is written to disk.

> Status: early. Tested on one model so far (see Compatibility). Built-in login (local accounts, OIDC,
> trusted reverse-proxy header) is in progress - until it lands, **do not expose DVR Bridge without an
> authenticating reverse proxy in front of it.**

Not affiliated with or endorsed by Swann, RaySharp or any DVR manufacturer. Brand names are used only to
describe compatibility.

## Compatibility

| Model | Firmware | Live | Recordings list | Playback / export | Rename |
| --- | --- | --- | --- | --- | --- |
| Swann DVR8-4580V (hardware DM-307) | V8.1.0-20220301 | yes | yes | yes | yes |

Other RaySharp-family DVRs that serve the JSON web API on their HTTP port (`/API/Web/Login`) and the
client protocol on port 9000 are likely to work. If you try one, please open an issue with the model,
firmware and what worked - that table is how others find out.

## Quick start

```yaml
# compose.yaml
services:
  dvr-bridge:
    image: ghcr.io/forcky/dvr-bridge:latest   # or build: .
    environment:
      DVRBRIDGE_DVR_HOST: 192.168.1.50
      DVRBRIDGE_DVR_USERNAME: admin
      DVRBRIDGE_DVR_PASSWORD_FILE: /run/secrets/dvr_password
      DVRBRIDGE_DVR_TIMEZONE: Europe/London     # the time zone the DVR's clock is set to
    secrets: [dvr_password]
    volumes:
      - dvr-bridge-data:/data
    ports:
      - 127.0.0.1:8099:8099                   # put your reverse proxy in front
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    restart: unless-stopped

secrets:
  dvr_password:
    file: ./dvr_password.txt

volumes:
  dvr-bridge-data:
```

Then open `http://127.0.0.1:8099/` (or your proxy's address). See `compose.example.yaml` and
`config.example.yaml` for complete examples.

### Network

DVR Bridge needs to reach the DVR on TCP **85** (web API), **554** (RTSP, live view) and **9000** (client
protocol, recorded playback; without it live view and the recordings list still work). Nothing else.

These DVRs run old firmware with vendor cloud ("P2P") services that cannot always be switched off. The
setup we recommend: put the DVR on its own VLAN with no internet access, and allow only the DVR Bridge
host to reach those three ports. Never port-forward the DVR itself.

## Configuration

Settings come from, in order of precedence: environment variables, then `/data/config.yaml`, then the
defaults below. Every setting `section.key` has the environment variable `DVRBRIDGE_<SECTION>_<KEY>`,
and any of them can be read from a file by adding `_FILE` (for Docker secrets).

| Setting | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `dvr.host` | `DVRBRIDGE_DVR_HOST` | - (required) | DVR address |
| `dvr.username` | `DVRBRIDGE_DVR_USERNAME` | `admin` | DVR account the bridge logs in with |
| `dvr.password` | `DVRBRIDGE_DVR_PASSWORD` | - (required) | password for that account |
| `dvr.http_port` | `DVRBRIDGE_DVR_HTTP_PORT` | `85` | DVR web/API port |
| `dvr.media_port` | `DVRBRIDGE_DVR_MEDIA_PORT` | `9000` | DVR client port (recorded playback) |
| `dvr.rtsp_port` | `DVRBRIDGE_DVR_RTSP_PORT` | `554` | DVR RTSP port (live view) |
| `dvr.channels` | `DVRBRIDGE_DVR_CHANNELS` | `0` | number of cameras; `0` asks the DVR |
| `dvr.timezone` | `DVRBRIDGE_DVR_TIMEZONE` | `$TZ` or `UTC` | time zone the DVR's clock is set to |
| `playback.max_concurrent` | `DVRBRIDGE_PLAYBACK_MAX_CONCURRENT` | `2` | clips streamed at once (more get HTTP 429) |
| `playback.max_range_hours` | `DVRBRIDGE_PLAYBACK_MAX_RANGE_HOURS` | `3` | longest clip that can be played or exported |
| `monitoring.poll_interval` | `DVRBRIDGE_MONITORING_POLL_INTERVAL` | `60` | seconds between DVR status polls |
| `monitoring.metrics_enabled` | `DVRBRIDGE_MONITORING_METRICS_ENABLED` | `true` | serve `/metrics` |
| `monitoring.metrics_token` | `DVRBRIDGE_MONITORING_METRICS_TOKEN` | empty | if set, `/metrics` needs `Authorization: Bearer <token>` |
| `server.public_url` | `DVRBRIDGE_SERVER_PUBLIC_URL` | empty | external URL, e.g. `https://cameras.example.com` |
| `server.secret_key` | `DVRBRIDGE_SERVER_SECRET_KEY` | generated | signs sessions; generated into `/data/secret.key` if unset |

Also: `DVRBRIDGE_DATA_DIR` (default `/data`) and `DVRBRIDGE_CONFIG` (default `/data/config.yaml`).
A bad or unknown setting stops the container at start-up with a message naming it.

**Use a dedicated DVR account if your DVR allows it.** DVR Bridge only ever reads from the DVR, with one
exception: renaming a camera writes that camera's name (and nothing else) to the DVR.

## Security notes

- The DVR's credentials are never logged or sent to the browser; go2rtc's output is scrubbed.
- DVR Bridge talks to the DVR through a fixed allow-list of API calls: read-only except the camera-name
  write. Firmware upgrade, reset, format, reboot and every other setting are refused inside the client.
- Login attempts to the DVR are rate-limited and suspended after a refusal, so a wrong password cannot
  lock the DVR's account.
- State-changing requests require a same-origin check (CSRF protection).
- Report vulnerabilities privately: see `SECURITY.md`.

## How it works

- **API client** (`dvrbridge/dvr/`): HTTP Digest login to the DVR's JSON API, session cookie + CSRF
  token, heartbeat, and a background poller whose cached state serves `/metrics` (scraping adds no DVR
  traffic).
- **Live** (`dvrbridge/go2rtc.py`): a bundled go2rtc pulls each camera's RTSP stream on demand, bound to
  localhost only; the app proxies its MSE WebSocket.
- **Recorded playback** (`dvrbridge/dvr9000/`): the DVR serves recordings only over its port-9000 client
  protocol, implemented here from observed network traffic and black-box behaviour. Frames are
  reassembled and stream-copied by ffmpeg into fragmented MP4 - no transcoding, a few MB of memory per
  playback, nothing on disk.

## Development

Tests run in the image build, so you need only Docker:

```
docker build --target test .
```

The tests use fake DVRs (HTTP API and port-9000 protocol) built from scrubbed real responses, so no
hardware is needed.

## License

GNU Affero General Public License v3.0 - see `LICENSE`. If you run a modified version for others over a
network, you must offer them its source. Third-party components and their licenses: see `NOTICE`.

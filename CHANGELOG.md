# Changelog

All notable changes to DVR Bridge. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-25

First public release.

### Added
- Live view of every camera in the browser (go2rtc, Media Source Extensions; no transcoding).
- Recordings: per-day timeline across all cameras with zoom and pan, in-page playback and MP4 download
  over the DVR's port-9000 client protocol. No footage is stored.
- Camera renames, stored on the DVR.
- Logins: local accounts (scrypt, optional or required TOTP, first-run setup code), OpenID Connect
  single sign-on, and trusted reverse-proxy headers; admin and viewer roles; an activity log.
- Settings page with validation, environment-locked values and in-place reloads; a first-run wizard with
  a DVR connection test.
- Optional Frigate integration: detections as timeline markers, including moments the DVR did not
  record (shown with Frigate's snapshot).
- Prometheus metrics and a health endpoint.
- Configuration via `/data/config.yaml` and `DVRBRIDGE_*` environment variables (with `_FILE` secrets).
- Multi-arch images (linux/amd64, linux/arm64) with SBOM and provenance.

### Compatibility
- Tested on Swann DVR8-4580V (hardware DM-307), firmware V8.1.0-20220301.

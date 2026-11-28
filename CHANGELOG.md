# Changelog

All notable changes to DVR Bridge. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- Users page with proxy or single sign-on logins: it now says where people are added (and which group
  makes someone an admin) instead of silently hiding the add form; the role of an account whose role comes
  from an admin group can no longer be changed here, because the next login put it back; proxy users show
  a last-login time (their last visit, updated at most every 10 minutes).

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

### Security
Hardening from an independent review before release:
- Password hashing is bounded (a few at a time, per address and overall) and request bodies are capped at
  64 KB, so a burst of anonymous requests cannot exhaust the container's memory.
- Wrong two-factor codes are limited per account and a repeated password no longer resets the count.
  Two-factor secrets are generated and kept by the server until confirmed; setting up, replacing or
  removing two-factor needs the account's password.
- `/metrics` needs the bearer token, an address in the new `monitoring.metrics_allow`, or an admin login.
- OIDC accounts are tied to the provider's subject (`sub`) rather than the username; an unverified e-mail
  address is not used as a username. (Database schema version 2; upgraded automatically.)
- Header mode without `auth.header_secret` accepts only single proxy addresses in `auth.trusted_proxies`.
- Behind an untrusted or unlisted proxy one visitor can no longer lock everyone out of logging in;
  a missing `auth.trusted_proxies` is reported in the log and on the Settings page. Login throttling counts
  IPv6 clients per /64.
- The saved DVR password is only used for the saved DVR address and ports: changing them, or testing
  another address, needs the password again.
- The live-view websocket passes only the MSE request to go2rtc, and go2rtc's error text (which names the
  DVR's address) is no longer shown to viewers. Live sockets per account are capped.
- Content-Security-Policy and related headers on every response; no inline scripts; API responses are
  `no-store`. The session cookie is `Secure` with the `__Host-` prefix over HTTPS, including behind a
  trusted TLS-terminating proxy.
- Replies from the DVR are size-limited and a malformed one no longer stops the status poller; playback
  frame reassembly is bounded.
- The activity log is capped, and anonymous OIDC callbacks no longer write to it. Password checks on the
  account page are throttled.
- The settings page refuses a switch to password logins while no local admin exists.
- Python dependencies are installed with hash checking; data files are created owner-only.

### Compatibility
- Tested on Swann DVR8-4580V (hardware DM-307), firmware V8.1.0-20220301.

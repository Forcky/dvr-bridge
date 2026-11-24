# Security policy

DVR Bridge guards access to home CCTV, so security reports are very welcome.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through GitHub: the repository's
**Security** tab, then **Report a vulnerability**. Include what you found, how to reproduce it, and the
version or commit you tested.

You can expect an acknowledgement within a few days. Once a fix is ready we will publish it and credit
you, unless you prefer not to be named.

## Supported versions

Fixes go into the latest release only. Please update before reporting, and keep your installation up
to date.

## Scope

In scope: DVR Bridge itself - logins and sessions, roles, the web pages and API, the configuration
handling, and anything that could expose camera video, DVR credentials or the DVR to someone who should
not have it.

Out of scope: vulnerabilities in the DVRs themselves (report those to the manufacturer), and setups
that ignore the documented guidance - for example `auth.mode: none` without an authenticating proxy,
or a DVR exposed directly to the internet.

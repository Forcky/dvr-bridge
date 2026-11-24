"""Rules for the camera-rename endpoint: name validation and the CSRF guard.

The site sits behind cookie auth (its own login or a reverse proxy's), so a state-changing request must prove it came from our own
pages. A cross-site page can neither send ``Content-Type: application/json`` without a CORS preflight (which
this app never approves) nor forge a same-origin ``Origin`` / ``Sec-Fetch-Site``.
"""
from __future__ import annotations

import re
from typing import Mapping

# Letters, digits, space and - _ . ' ( ) &  (ASCII only: the DVR counts bytes and its screen font is limited).
NAME_RE = re.compile(r"^[A-Za-z0-9 \-_.'()&]+$")


class InvalidName(ValueError):
    """Invalid camera name; ``code`` is the machine-readable reason."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def validate_name(raw: object, max_len: int) -> str:
    if not isinstance(raw, str):
        raise InvalidName("bad_name", "name must be a string")
    name = raw.strip()
    if not name:
        raise InvalidName("bad_name", "name must not be empty")
    if len(name) > max_len:
        raise InvalidName("bad_name", f"name must be at most {max_len} characters")
    if not NAME_RE.fullmatch(name):
        raise InvalidName("bad_name", "name may only contain letters, digits, spaces and - _ . ' ( ) &")
    return name


def csrf_problem(headers: Mapping[str, str]) -> str | None:
    """None if the request looks same-origin JSON, else a short reason (to return with HTTP 403).

    ``Sec-Fetch-Site`` is set by the browser from the URL it actually requested, so when it is present it decides
    (same-origin passes, anything else fails) and keeps working if a proxy rewrites ``Host``. Without it (older
    browsers) the ``Origin`` header must name this host.
    """
    ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return "Content-Type must be application/json"
    site = headers.get("sec-fetch-site")
    if site is not None:
        return None if site == "same-origin" else "cross-site request refused"
    origin = headers.get("origin")
    if origin is None:
        return "request must carry a same-origin Origin header"
    host = headers.get("host", "")
    if not host or origin.split("://", 1)[-1] != host:
        return "cross-origin request refused"
    return None

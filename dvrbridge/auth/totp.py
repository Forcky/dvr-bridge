"""Time-based one-time passwords (RFC 6238: SHA-1, 6 digits, 30-second steps), as used by every
common authenticator app."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP = 30
DIGITS = 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def verify(secret: str, code: str, last_used_step: int | None = None, now: float | None = None,
           window: int = 1) -> int | None:
    """The time step the code belongs to, or None. A step at or before ``last_used_step`` is refused,
    so a code cannot be replayed."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // STEP)
    for step in range(current - window, current + window + 1):
        if last_used_step is not None and step <= last_used_step:
            continue
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, username: str, issuer: str = "DVR Bridge") -> str:
    label = quote(f"{issuer}:{username}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret, "issuer": issuer,
                                                   "digits": DIGITS, "period": STEP})

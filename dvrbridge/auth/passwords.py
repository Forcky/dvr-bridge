"""Password hashing with scrypt (Python's standard library - no native dependency).

Stored form: ``scrypt$<log2 N>$<r>$<p>$<salt b64>$<hash b64>``. Parameters are stored with each
hash, so they can be raised later without invalidating existing passwords.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

LOG2_N, R, P = 15, 8, 1  # ~32 MB and tens of milliseconds per hash
DKLEN = 32
MIN_LENGTH, MAX_LENGTH = 10, 256


def _scrypt(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    n = 1 << log2_n
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=DKLEN,
                          maxmem=128 * r * (n + p + 2) + 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = _scrypt(password, salt, LOG2_N, R, P)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"scrypt${LOG2_N}${R}${P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. A missing or malformed hash is still hashed against, so the time taken
    does not reveal whether an account exists."""
    try:
        kind, log2_n, r, p, salt, dk = (stored or "").split("$")
        if kind != "scrypt":
            raise ValueError
        params = int(log2_n), int(r), int(p)
        if not (10 <= params[0] <= 20 and 1 <= params[1] <= 32 and 1 <= params[2] <= 4):
            raise ValueError
        salt_b, dk_b = base64.b64decode(salt), base64.b64decode(dk)
    except (ValueError, TypeError):
        _scrypt(password, b"\0" * 16, LOG2_N, R, P)
        return False
    return hmac.compare_digest(_scrypt(password, salt_b, *params), dk_b)


def password_problem(password: str, username: str = "") -> str | None:
    """Why a new password is not acceptable, or None. Length over complexity rules (NIST 800-63B)."""
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        return f"Passwords must be at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Passwords can be at most {MAX_LENGTH} characters."
    if username and password.strip().lower() == username.strip().lower():
        return "The password cannot be the username."
    if len(set(password)) < 4:
        return "That password is too repetitive."
    return None

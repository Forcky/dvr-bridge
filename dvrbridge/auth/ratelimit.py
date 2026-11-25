"""Failed-login throttling, in memory (a restart clears it; that is acceptable for a brute-force brake).

Two keys per attempt: the client address and the username. An address that keeps failing is
blocked for a while; a username is blocked only after many more failures, so that someone who
merely knows the admin's name cannot keep them locked out from one address. Wrong two-factor
codes are counted per account on a third, much stricter key that a correct password never clears.

IPv6 clients are counted per /64 (one customer's range), and the tables are bounded.
"""
from __future__ import annotations

import ipaddress
import time
from collections import deque

WINDOW = 15 * 60
IP_LIMIT = 10
# A "shared" address is a reverse proxy we were not told to trust: every visitor arrives from it, so
# blocking it at IP_LIMIT would let one visitor lock out everybody. The account limits still apply.
SHARED_IP_LIMIT = 100
USER_LIMIT = 30
MFA_LIMIT = 5
ACCOUNT_LIMIT = 5
BLOCK = 15 * 60
MAX_KEYS = 20_000
IP_INFLIGHT = 4


def ip_key(ip: str) -> str:
    try:
        a = ipaddress.ip_address((ip or "").strip().strip("[]"))
    except ValueError:
        return f"ip:{ip}"
    if a.version == 6:
        if a.ipv4_mapped:
            return f"ip:{a.ipv4_mapped}"
        return f"ip:{ipaddress.ip_network((a, 64), strict=False).network_address}/64"
    return f"ip:{a}"


class LoginThrottle:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._fails: dict[str, deque[float]] = {}
        self._blocked: dict[str, float] = {}
        self._inflight: dict[str, int] = {}

    # ---- generic keys --------------------------------------------------------------------------
    def _prune(self, now: float) -> None:
        for k in [k for k, until in self._blocked.items() if until <= now]:
            del self._blocked[k]
        if len(self._fails) > MAX_KEYS:
            for k in [k for k, q in self._fails.items() if not q or now - q[-1] > WINDOW]:
                del self._fails[k]
            while len(self._fails) > MAX_KEYS:      # still full: forget the oldest keys
                del self._fails[next(iter(self._fails))]
        while len(self._blocked) > MAX_KEYS:
            del self._blocked[next(iter(self._blocked))]

    def blocked(self, *keys: str) -> int:
        """Seconds until the longest block on any of these keys ends (0 = none)."""
        now = self.clock()
        waits = [until - now for k in keys if (until := self._blocked.get(k)) and until > now]
        return int(max(waits)) + 1 if waits else 0

    def hit(self, key: str, limit: int) -> None:
        now = self.clock()
        self._prune(now)
        q = self._fails.setdefault(key, deque())
        while q and now - q[0] > WINDOW:
            q.popleft()
        q.append(now)
        if len(q) >= limit:
            self._blocked[key] = now + BLOCK
            self._fails.pop(key, None)

    def clear(self, *keys: str) -> None:
        for k in keys:
            self._fails.pop(k, None)

    # ---- logins --------------------------------------------------------------------------------
    @staticmethod
    def _user(username: str) -> str:
        return f"user:{username.lower()}"

    @staticmethod
    def _mfa(username: str) -> str:
        return f"mfa:{username.lower()}"

    def retry_after(self, ip: str, username: str, *, mfa: bool = False) -> int:
        """Seconds until another attempt is allowed (0 = go ahead)."""
        keys = [ip_key(ip), self._user(username)] + ([self._mfa(username)] if mfa else [])
        return self.blocked(*keys)

    def failure(self, ip: str, username: str, *, shared: bool = False, mfa: bool = False) -> None:
        self.hit(ip_key(ip), SHARED_IP_LIMIT if shared else IP_LIMIT)
        self.hit(self._user(username), USER_LIMIT)
        if mfa:
            self.hit(self._mfa(username), MFA_LIMIT)

    def success(self, ip: str, username: str) -> None:
        """A completed login (password and, where enabled, the second factor)."""
        self.clear(ip_key(ip), self._user(username), self._mfa(username))

    # ---- attempts in progress (counted before the expensive password hash) -----------------------
    def enter(self, ip: str, *, shared: bool = False) -> bool:
        """Take a slot for a password check from this address; False if it already holds too many."""
        if shared:
            return True
        k = ip_key(ip)
        if self._inflight.get(k, 0) >= IP_INFLIGHT:
            return False
        self._inflight[k] = self._inflight.get(k, 0) + 1
        return True

    def leave(self, ip: str, *, shared: bool = False) -> None:
        if shared:
            return
        k = ip_key(ip)
        n = self._inflight.get(k, 0) - 1
        if n > 0:
            self._inflight[k] = n
        else:
            self._inflight.pop(k, None)

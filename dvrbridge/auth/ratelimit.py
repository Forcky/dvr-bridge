"""Failed-login throttling, in memory (a restart clears it; that is acceptable for a brute-force brake).

Two keys per attempt: the client address and the username. An address that keeps failing is
blocked for a while; a username is blocked only after many more failures, so that someone who
merely knows the admin's name cannot keep them locked out from one address.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

WINDOW = 15 * 60
IP_LIMIT = 10
USER_LIMIT = 30
BLOCK = 15 * 60


class LoginThrottle:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._blocked: dict[str, float] = {}

    def _prune(self, key: str, now: float) -> None:
        q = self._fails[key]
        while q and now - q[0] > WINDOW:
            q.popleft()
        if not q:
            self._fails.pop(key, None)

    def retry_after(self, ip: str, username: str) -> int:
        """Seconds until another attempt is allowed (0 = go ahead)."""
        now = self.clock()
        waits = [until - now for k in (f"ip:{ip}", f"user:{username.lower()}")
                 if (until := self._blocked.get(k)) and until > now]
        return int(max(waits)) + 1 if waits else 0

    def failure(self, ip: str, username: str) -> None:
        now = self.clock()
        for key, limit in ((f"ip:{ip}", IP_LIMIT), (f"user:{username.lower()}", USER_LIMIT)):
            self._prune(key, now)
            self._fails[key].append(now)
            if len(self._fails[key]) >= limit:
                self._blocked[key] = now + BLOCK
                self._fails.pop(key, None)

    def success(self, ip: str, username: str) -> None:
        self._fails.pop(f"ip:{ip}", None)
        self._fails.pop(f"user:{username.lower()}", None)

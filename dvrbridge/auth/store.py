"""Users, sessions and the audit log, in SQLite under the data directory.

Session tokens are stored only as SHA-256 hashes, so a copy of the database cannot be used to log
in. Calls are short and synchronous behind one lock; the app makes a handful per request at most.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROLES = ("admin", "viewer")
SOURCES = ("local", "oidc", "header")

SCHEMA = [
    # version 1
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT,
        role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
        source TEXT NOT NULL CHECK (source IN ('local', 'oidc', 'header')),
        totp_secret TEXT,
        totp_last_step INTEGER,
        disabled INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        last_login_at REAL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        mfa_pending INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        ip TEXT,
        user_agent TEXT
    );
    CREATE INDEX sessions_user ON sessions(user_id);
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        username TEXT,
        action TEXT NOT NULL,
        detail TEXT,
        ip TEXT
    );
    CREATE INDEX audit_ts ON audit(ts);
    """,
    # version 2: who an external (OIDC) account is at its identity provider - "<issuer> <sub>". The username
    # there can change or be reused; the subject cannot. NULL for local and proxy accounts, and for OIDC
    # accounts created before this version until their next login.
    """
    ALTER TABLE users ADD COLUMN subject TEXT;
    CREATE UNIQUE INDEX users_subject ON users(subject) WHERE subject IS NOT NULL;
    """,
]
MAX_AUDIT_ROWS = 50_000
AUDIT_PRUNE_EVERY = 200


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    source: str
    totp_enabled: bool
    disabled: bool
    created_at: float
    last_login_at: float | None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role, "source": self.source,
                "totp_enabled": self.totp_enabled, "disabled": self.disabled,
                "created_at": self.created_at, "last_login_at": self.last_login_at}


@dataclass(frozen=True)
class Session:
    user: User
    mfa_pending: bool
    expires_at: float


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
            self._migrate()
        self._audit_inserts = 0
        self._restrict_files()

    def _restrict_files(self) -> None:
        """The database holds password hashes and two-factor secrets: owner-only, whatever the umask was."""
        if self.path == ":memory:":
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self.path + suffix, 0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(SCHEMA[version:], start=version + 1):
            self._db.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # ---- users -----------------------------------------------------------------------------
    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(row["id"], row["username"], row["role"], row["source"], bool(row["totp_secret"]),
                    bool(row["disabled"]), row["created_at"], row["last_login_at"])

    def count_users(self) -> int:
        return self._q("SELECT COUNT(*) FROM users")[0][0]

    def count_active_admins(self) -> int:
        return self._q("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0")[0][0]

    def users(self) -> list[User]:
        return [self._user(r) for r in self._q("SELECT * FROM users ORDER BY username COLLATE NOCASE")]

    def get_user(self, user_id: int) -> User | None:
        rows = self._q("SELECT * FROM users WHERE id = ?", (user_id,))
        return self._user(rows[0]) if rows else None

    def find_user(self, username: str) -> User | None:
        rows = self._q("SELECT * FROM users WHERE username = ?", (username,))
        return self._user(rows[0]) if rows else None

    def find_subject(self, subject: str) -> User | None:
        rows = self._q("SELECT * FROM users WHERE subject = ?", (subject,))
        return self._user(rows[0]) if rows else None

    def subject(self, user_id: int) -> str | None:
        rows = self._q("SELECT subject FROM users WHERE id = ?", (user_id,))
        return rows[0][0] if rows else None

    def set_subject(self, user_id: int, subject: str) -> None:
        self._x("UPDATE users SET subject = ? WHERE id = ?", (subject, user_id))

    def count_local_admins(self) -> int:
        """Enabled admins who can log in with a password."""
        return self._q("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND source = 'local' "
                       "AND password_hash IS NOT NULL")[0][0]

    def password_hash(self, user_id: int) -> str | None:
        rows = self._q("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        return rows[0][0] if rows else None

    def create_user(self, username: str, role: str, source: str, password_hash: str | None = None,
                    subject: str | None = None) -> User:
        if role not in ROLES or source not in SOURCES:
            raise ValueError("bad role or source")
        cur = self._x("INSERT INTO users (username, password_hash, role, source, created_at, subject) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (username, password_hash, role, source, time.time(), subject))
        return self.get_user(cur.lastrowid)

    def update_user(self, user_id: int, **fields) -> User | None:
        allowed = {"role", "disabled", "password_hash", "username"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
        if "role" in fields and fields["role"] not in ROLES:
            raise ValueError("bad role")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._x(f"UPDATE users SET {sets} WHERE id = ?", (*[int(v) if isinstance(v, bool) else v
                                                              for v in fields.values()], user_id))
        return self.get_user(user_id)

    def delete_user(self, user_id: int) -> None:
        self._x("DELETE FROM users WHERE id = ?", (user_id,))

    def touch_login(self, user_id: int) -> None:
        self._x("UPDATE users SET last_login_at = ? WHERE id = ?", (time.time(), user_id))

    # ---- two-factor --------------------------------------------------------------------------
    def totp(self, user_id: int) -> tuple[str | None, int | None]:
        rows = self._q("SELECT totp_secret, totp_last_step FROM users WHERE id = ?", (user_id,))
        return (rows[0][0], rows[0][1]) if rows else (None, None)

    def set_totp(self, user_id: int, secret: str | None, last_step: int | None = None) -> None:
        self._x("UPDATE users SET totp_secret = ?, totp_last_step = ? WHERE id = ?", (secret, last_step, user_id))

    def use_totp_step(self, user_id: int, step: int) -> bool:
        """Record a used code's step; False if it (or a later one) was already used."""
        cur = self._x("UPDATE users SET totp_last_step = ? WHERE id = ? AND (totp_last_step IS NULL OR totp_last_step < ?)",
                      (step, user_id, step))
        return cur.rowcount == 1

    # ---- sessions ----------------------------------------------------------------------------
    def create_session(self, user_id: int, hours: float, *, mfa_pending: bool = False, ip: str = "",
                       user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        ttl = 300 if mfa_pending else hours * 3600
        self._x("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (token_hash(token), user_id, int(mfa_pending), now, now + ttl, now, ip[:64], user_agent[:256]))
        return token

    def session(self, token: str) -> Session | None:
        if not token or len(token) > 128:
            return None
        rows = self._q("SELECT s.mfa_pending, s.expires_at, s.last_seen_at, u.* FROM sessions s "
                       "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (token_hash(token),))
        if not rows:
            return None
        r = rows[0]
        now = time.time()
        if r["expires_at"] < now or r["disabled"]:
            self.revoke(token)
            return None
        if now - r["last_seen_at"] > 60:
            self._x("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash(token)))
        return Session(self._user(r), bool(r["mfa_pending"]), r["expires_at"])

    def revoke(self, token: str) -> None:
        self._x("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def revoke_user_sessions(self, user_id: int, keep: str | None = None) -> None:
        if keep:
            self._x("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, token_hash(keep)))
        else:
            self._x("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def purge_expired(self) -> None:
        self._x("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        self._x("DELETE FROM audit WHERE ts < ?", (time.time() - 365 * 86400,))
        self.trim_audit()

    # ---- audit -------------------------------------------------------------------------------
    def audit(self, action: str, username: str | None = None, detail: str = "", ip: str = "") -> None:
        self._x("INSERT INTO audit (ts, username, action, detail, ip) VALUES (?, ?, ?, ?, ?)",
                (time.time(), username, action, detail[:500], ip[:64]))
        self._audit_inserts += 1
        if self._audit_inserts % AUDIT_PRUNE_EVERY == 0:
            self.trim_audit()

    def trim_audit(self, keep: int = MAX_AUDIT_ROWS) -> None:
        """Keep only the newest rows, so nobody can grow the log without bound."""
        self._x("DELETE FROM audit WHERE id <= (SELECT id FROM audit ORDER BY id DESC LIMIT 1 OFFSET ?)", (keep,))

    def audit_log(self, limit: int = 200) -> list[dict]:
        rows = self._q("SELECT ts, username, action, detail, ip FROM audit ORDER BY id DESC LIMIT ?",
                       (max(1, min(limit, 1000)),))
        return [dict(r) for r in rows]

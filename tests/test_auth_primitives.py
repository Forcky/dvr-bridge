import time

import pytest

from dvrbridge.auth import passwords, totp
from dvrbridge.auth.store import Store

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890" (RFC 6238 SHA-1 key)


# ---- passwords ---------------------------------------------------------------------------------
def test_hash_and_verify():
    h = passwords.hash_password("correct horse battery")
    assert h.startswith("scrypt$15$8$1$") and "correct" not in h
    assert passwords.verify_password("correct horse battery", h)
    assert not passwords.verify_password("correct horse batterY", h)
    assert passwords.hash_password("correct horse battery") != h  # salted


@pytest.mark.parametrize("stored", [None, "", "plain", "md5$x", "scrypt$99$8$1$AA==$AA==", "scrypt$15$8$1$!!$!!"])
def test_malformed_hashes_never_verify(stored):
    assert passwords.verify_password("anything", stored) is False


@pytest.mark.parametrize("pw, ok", [("short", False), ("aaaaaaaaaaaa", False), ("alice-admin", True),
                                    ("x" * 257, False), ("Alice12345", False)])
def test_password_rules(pw, ok):
    assert (passwords.password_problem(pw, username="alice12345") is None) is ok


# ---- TOTP ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("t, code", [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"),
                                     (2000000000, "279037")])
def test_rfc6238_vectors(t, code):
    assert totp.code_at(RFC_SECRET, t // 30) == code
    assert totp.verify(RFC_SECRET, code, now=t) == t // 30


def test_window_and_replay():
    now = 1_700_000_000
    prev = totp.code_at(RFC_SECRET, now // 30 - 1)
    assert totp.verify(RFC_SECRET, prev, now=now) == now // 30 - 1           # one step of clock skew
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, now // 30 - 3), now=now) is None
    step = totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, now // 30), now=now)
    assert totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, now // 30), last_used_step=step, now=now) is None


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", None])
def test_malformed_codes(bad):
    assert totp.verify(RFC_SECRET, bad) is None


def test_new_secret_and_uri():
    s = totp.new_secret()
    assert len(s) == 32 and totp.verify(s, totp.code_at(s, int(time.time() // 30))) is not None
    uri = totp.provisioning_uri(s, "alice")
    assert uri.startswith("otpauth://totp/DVR%20Bridge%3Aalice?") and f"secret={s}" in uri


# ---- store --------------------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    yield s
    s.close()


def test_users_and_case_insensitive_names(store):
    a = store.create_user("Alice", "admin", "local", "hash")
    assert store.find_user("alice").id == a.id and store.count_active_admins() == 1
    with pytest.raises(Exception):
        store.create_user("ALICE", "viewer", "local")
    with pytest.raises(ValueError):
        store.create_user("bob", "root", "local")
    store.update_user(a.id, disabled=True)
    assert store.get_user(a.id).disabled and store.count_active_admins() == 0
    with pytest.raises(ValueError):
        store.update_user(a.id, totp_secret="x")


def test_sessions_are_stored_hashed_and_expire(store, tmp_path):
    u = store.create_user("alice", "viewer", "local")
    token = store.create_session(u.id, hours=1, ip="10.0.0.1")
    assert store.session(token).user.username == "alice"
    raw = (tmp_path / "db.sqlite").read_bytes() + (tmp_path / "db.sqlite-wal").read_bytes()
    assert token.encode() not in raw
    store._x("UPDATE sessions SET expires_at = 0")
    assert store.session(token) is None


def test_disabled_user_loses_sessions_and_revoke(store):
    u = store.create_user("alice", "viewer", "local")
    t1, t2 = store.create_session(u.id, 1), store.create_session(u.id, 1)
    store.revoke_user_sessions(u.id, keep=t1)
    assert store.session(t1) and store.session(t2) is None
    store.update_user(u.id, disabled=True)
    assert store.session(t1) is None


def test_mfa_pending_sessions_are_short(store):
    u = store.create_user("alice", "viewer", "local")
    s = store.session(store.create_session(u.id, hours=12, mfa_pending=True))
    assert s.mfa_pending and s.expires_at - time.time() <= 300


def test_totp_step_cannot_be_reused(store):
    u = store.create_user("alice", "viewer", "local")
    store.set_totp(u.id, "SECRET")
    assert store.use_totp_step(u.id, 100) and not store.use_totp_step(u.id, 100)
    assert not store.use_totp_step(u.id, 99) and store.use_totp_step(u.id, 101)


def test_deleting_a_user_removes_sessions(store):
    u = store.create_user("alice", "viewer", "local")
    t = store.create_session(u.id, 1)
    store.delete_user(u.id)
    assert store.session(t) is None


def test_audit_log(store):
    store.audit("login", "alice", "password", "10.0.0.1")
    store.audit("export", "alice", "ch1 10:00-10:05")
    log = store.audit_log()
    assert [e["action"] for e in log] == ["export", "login"] and log[1]["ip"] == "10.0.0.1"


def test_schema_survives_reopen(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.create_user("alice", "admin", "local")
    s.close()
    s2 = Store(tmp_path / "db.sqlite")
    assert s2.find_user("alice") is not None
    s2.close()

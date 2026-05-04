"""Unit tests for hashing helpers + reset_password CLI."""
import os
import subprocess
import sys

import pytest

from backend.auth import hash_password, verify_password
from backend.auth_storage import read_auth, write_auth


@pytest.fixture
def tmp_auth_file(tmp_path, monkeypatch):
    p = tmp_path / "auth.json"
    monkeypatch.setenv("KOTAK_AUTH_FILE", str(p))
    return str(p)


def test_hash_password_returns_pbkdf2_string():
    h = hash_password("hello123")
    assert h.startswith("pbkdf2:")
    assert h != "hello123"


def test_verify_password_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password(h, "correct horse battery staple") is True
    assert verify_password(h, "wrong") is False


def test_verify_password_with_none_hash_returns_false():
    # Important: uninitialized auth file has password_hash=None.
    # verify_password(None, "anything") must return False, not crash.
    assert verify_password(None, "any") is False


def test_reset_script_writes_hash_and_bumps_version(tmp_auth_file):
    # Pre-condition: file has version=7, some hash.
    write_auth(tmp_auth_file, password_hash="oldhash", session_version=7)

    env = os.environ.copy()
    env["KOTAK_AUTH_FILE"] = tmp_auth_file
    result = subprocess.run(
        [sys.executable, "-m", "backend.auth_reset_password", "--non-interactive"],
        input="newpw1234\nnewpw1234\n",
        text=True,
        capture_output=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr

    state = read_auth(tmp_auth_file)
    assert state["session_version"] == 8  # bumped
    assert state["password_hash"] != "oldhash"
    assert verify_password(state["password_hash"], "newpw1234")


def test_reset_script_rejects_short_password(tmp_auth_file):
    env = os.environ.copy()
    env["KOTAK_AUTH_FILE"] = tmp_auth_file
    result = subprocess.run(
        [sys.executable, "-m", "backend.auth_reset_password", "--non-interactive"],
        input="abc\nabc\n",
        text=True,
        capture_output=True,
        env=env,
    )
    assert result.returncode != 0
    assert "at least 8" in result.stderr.lower()


def test_reset_script_rejects_mismatch(tmp_auth_file):
    env = os.environ.copy()
    env["KOTAK_AUTH_FILE"] = tmp_auth_file
    result = subprocess.run(
        [sys.executable, "-m", "backend.auth_reset_password", "--non-interactive"],
        input="newpw1234\ndifferent\n",
        text=True,
        capture_output=True,
        env=env,
    )
    assert result.returncode != 0
    assert "match" in result.stderr.lower()


# ----- Task 3: brute-force lockout -----

from backend.auth import (
    LOCKOUT_THRESHOLD,
    LOCKOUT_WINDOW_SECS,
    _LOCKOUT_STATE,
    is_locked_out,
    record_failed_login,
)


def setup_function():
    # Each test starts with a clean lockout dict so they don't interfere.
    _LOCKOUT_STATE.clear()


def test_lockout_triggers_after_threshold():
    for _ in range(LOCKOUT_THRESHOLD):
        record_failed_login("1.2.3.4")
    assert is_locked_out("1.2.3.4")


def test_lockout_does_not_affect_other_ips():
    for _ in range(LOCKOUT_THRESHOLD):
        record_failed_login("1.2.3.4")
    assert not is_locked_out("5.6.7.8")


def test_lockout_clears_after_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("backend.auth.time.time", lambda: now[0])
    for _ in range(LOCKOUT_THRESHOLD):
        record_failed_login("1.2.3.4")
    assert is_locked_out("1.2.3.4")
    now[0] += LOCKOUT_WINDOW_SECS + 1
    assert not is_locked_out("1.2.3.4")


def test_lockout_threshold_is_5_window_60s():
    # Concrete numbers per spec.
    assert LOCKOUT_THRESHOLD == 5
    assert LOCKOUT_WINDOW_SECS == 60

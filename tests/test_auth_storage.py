"""Unit tests for backend.auth_storage."""
import threading

import pytest

from backend.auth_storage import (
    AUTH_VERSION_UNINITIALIZED,
    bump_session_version,
    default_auth_path,
    read_auth,
    write_auth,
)


@pytest.fixture
def tmp_auth_file(tmp_path, monkeypatch):
    p = tmp_path / "auth.json"
    monkeypatch.setenv("KOTAK_AUTH_FILE", str(p))
    return str(p)


def test_default_auth_path_uses_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("KOTAK_AUTH_FILE", str(tmp_path / "x.json"))
    assert default_auth_path() == str(tmp_path / "x.json")


def test_default_auth_path_fallback(monkeypatch):
    monkeypatch.delenv("KOTAK_AUTH_FILE", raising=False)
    assert default_auth_path() == "/home/kotak/shared/auth.json"


def test_read_missing_file_returns_uninitialized(tmp_auth_file):
    state = read_auth(tmp_auth_file)
    assert state["password_hash"] is None
    assert state["session_version"] == AUTH_VERSION_UNINITIALIZED


def test_write_then_read_roundtrip(tmp_auth_file):
    write_auth(tmp_auth_file, password_hash="hashedpw", session_version=3)
    state = read_auth(tmp_auth_file)
    assert state["password_hash"] == "hashedpw"
    assert state["session_version"] == 3
    assert "updated_at" in state


def test_concurrent_writes_dont_corrupt(tmp_auth_file):
    # 10 threads each do 5 read-modify-write cycles bumping version.
    # Final version should equal 50 if locking works.
    write_auth(tmp_auth_file, password_hash="x", session_version=0)
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        for _ in range(5):
            bump_session_version(tmp_auth_file)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert read_auth(tmp_auth_file)["session_version"] == 50


def test_corrupt_json_returns_uninitialized(tmp_auth_file):
    with open(tmp_auth_file, "w") as f:
        f.write("{not valid json")
    state = read_auth(tmp_auth_file)
    assert state["session_version"] == AUTH_VERSION_UNINITIALIZED

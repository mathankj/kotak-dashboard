"""Read/write the shared auth.json file under fcntl.flock.

This module is deliberately Flask-free — it's pure I/O so it can be
tested without spinning up the app, and reused from the
backend.auth_reset_password CLI.

Cross-process safety: both kotak.service (port 5000) and
kotak-reverse.service (port 5001) read+write this same file. We use
fcntl.flock with LOCK_SH for reads and LOCK_EX for writes so the two
processes can't tear-read each other.
"""
import fcntl
import json
import os
from datetime import datetime

# Sentinel: a freshly-installed system with no password set.
# Any cookie carrying session_version=0 is rejected (no one ever
# logged in before initial setup, so no legit cookie has sid=0).
AUTH_VERSION_UNINITIALIZED = 0

DEFAULT_PATH = "/home/kotak/shared/auth.json"


def default_auth_path():
    """Return the auth file path: env var override, else default."""
    return os.environ.get("KOTAK_AUTH_FILE", DEFAULT_PATH)


def _empty_state():
    return {
        "password_hash": None,
        "session_version": AUTH_VERSION_UNINITIALIZED,
        "updated_at": None,
    }


def read_auth(path=None):
    """Read auth.json under LOCK_SH. Returns sentinel state on missing/corrupt.

    Holds a shared lock on the sidecar lock file (same one writers use
    exclusively) so we cannot race a write-in-progress.
    """
    path = path or default_auth_path()
    if not os.path.exists(path):
        return _empty_state()
    try:
        # LOCK_SH on the sidecar; multiple readers fine, blocks only against writers.
        with open(_lock_path(path), "a+") as lockf:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_SH)
            try:
                with open(path, "r") as f:
                    data = json.load(f)
            finally:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
        # Defensive: only return well-formed records.
        if not isinstance(data, dict) or "session_version" not in data:
            return _empty_state()
        return {
            "password_hash": data.get("password_hash"),
            "session_version": int(data.get("session_version", 0)),
            "updated_at": data.get("updated_at"),
        }
    except (json.JSONDecodeError, OSError, ValueError):
        return _empty_state()


def _atomic_write_locked(path, payload):
    """Internal: caller must already hold LOCK_EX on `path`. Writes via tmp+replace."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _lock_path(path):
    """Return the sidecar lock path. Kept separate from the data file so the
    flock-bearing fd stays valid across tmp+rename of the data file."""
    return path + ".lock"


def write_auth(path=None, *, password_hash, session_version):
    """Write auth.json under LOCK_EX. Caller responsible for incrementing version."""
    path = path or default_auth_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    payload = {
        "password_hash": password_hash,
        "session_version": int(session_version),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
    }
    # Lock on a sidecar file that is NEVER renamed. Locking the data file
    # itself is unsafe because os.replace(tmp, path) swaps the inode, and
    # any concurrent thread holding an fd to the old inode would be flock'ing
    # an orphaned inode (no mutual exclusion against the renamer).
    with open(_lock_path(path), "a+") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            _atomic_write_locked(path, payload)
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def bump_session_version(path=None):
    """Atomic read-modify-write under LOCK_EX: version += 1, hash unchanged."""
    path = path or default_auth_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(_lock_path(path), "a+") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            try:
                with open(path, "r") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = _empty_state()
            data["session_version"] = int(data.get("session_version", 0)) + 1
            data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
            _atomic_write_locked(path, data)
        finally:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

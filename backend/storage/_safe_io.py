"""Atomic JSON I/O + per-path locks.

Two race conditions we need to prevent:
1. Reader sees a half-written file (e.g. process killed mid-write, or another
   thread reads while writer is partway through).
2. Two writers race and one's update is lost (read-modify-write on the same
   file from two threads at once).

Fix:
- atomic_write_json writes to <path>.tmp then os.replace() — POSIX atomic on
  the same filesystem; on Windows os.replace also overwrites atomically.
- file_lock(path) gives each storage path its own threading.Lock so the
  read-modify-write sequence inside append_history/append_order is serialised.
  Locks are keyed by the absolute path so different files don't contend.

Used by storage/{trades,orders,history}.py. Not exported outside storage.
"""
import json
import os
import threading


_locks = {}
_locks_guard = threading.Lock()


def file_lock(path):
    """Return a threading.Lock unique to this file path."""
    key = os.path.abspath(path)
    lock = _locks.get(key)
    if lock is None:
        with _locks_guard:
            lock = _locks.get(key)
            if lock is None:
                lock = threading.Lock()
                _locks[key] = lock
    return lock


def atomic_write_json(path, data):
    """Write JSON to `path` atomically.

    Writes to <path>.tmp then os.replace() — readers either see the old
    file or the new one, never a partial write.
    """
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path, default):
    """Read JSON from `path`, returning `default` if missing or unreadable."""
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return default

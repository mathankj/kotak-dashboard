"""Orders log: every place_order attempt is appended for audit.

Atomic writes + per-file lock so concurrent place_order requests can't
corrupt the file or lose entries to a read-modify-write race.
"""
import os

from backend.storage._safe_io import atomic_write_json, file_lock, read_json

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ORDERS_FILE = os.path.join(_REPO_ROOT, "data", "orders_log.json")


def append_order(entry):
    """Append one order attempt to orders_log.json (newest first, max 200)."""
    try:
        with file_lock(ORDERS_FILE):
            existing = read_json(ORDERS_FILE, [])
            existing.insert(0, entry)
            existing = existing[:200]
            atomic_write_json(ORDERS_FILE, existing)
    except Exception:
        pass


def read_orders():
    return read_json(ORDERS_FILE, [])

"""Login attempt history (login_history.json), shown on /history page.

Atomic writes + per-file lock to keep history consistent if a login attempt
and a /history read race each other.
"""
import os

from backend.storage._safe_io import atomic_write_json, file_lock, read_json
from backend.utils import now_ist

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HISTORY_FILE = os.path.join(_REPO_ROOT, "data", "login_history.json")


def append_history(status, detail):
    """Append a login attempt to history (newest first, max 30)."""
    entry = {
        "timestamp": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "status": status,  # "success" or "failed"
        "detail": detail,
    }
    try:
        with file_lock(HISTORY_FILE):
            existing = read_json(HISTORY_FILE, [])
            existing.insert(0, entry)
            existing = existing[:30]
            atomic_write_json(HISTORY_FILE, existing)
    except Exception:
        pass  # never let history I/O break login


def read_history():
    return read_json(HISTORY_FILE, [])

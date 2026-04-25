"""Login attempt history (login_history.json), shown on /history page.

Moved out of kotak/client.py so storage concerns live together. client.py now
imports append_history/read_history from here.
"""
import json
import os

from backend.utils import now_ist

HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "login_history.json",
)


def append_history(status, detail):
    """Append a login attempt to history. Newest first, max 30."""
    entry = {
        "timestamp": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "status": status,  # "success" or "failed"
        "detail": detail,
    }
    try:
        existing = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                existing = json.load(f)
        existing.insert(0, entry)
        existing = existing[:30]
        with open(HISTORY_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass  # never let history I/O break login


def read_history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []

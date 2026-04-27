"""Paper trade ledger JSON store.

data/paper_ledger.json. Mirror of trades.py — atomic writes, per-path
file lock — for the parallel paper book added in Phase 2 of the
trailing-paper-l5 spec.

Note: deliberately uses a NEW filename (`paper_ledger.json`) to avoid
collision with the legacy `paper_trades.json` that trades.py migrates
into the live ledger.
"""
import os

from backend.storage._safe_io import atomic_write_json, file_lock, read_json


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
LEDGER_FILE = os.path.join(_REPO_ROOT, "data", "paper_ledger.json")

os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)


def read_paper_ledger():
    return read_json(LEDGER_FILE, [])


def write_paper_ledger(trades):
    try:
        with file_lock(LEDGER_FILE):
            atomic_write_json(LEDGER_FILE, trades)
    except Exception:
        pass


def next_paper_id(trades):
    mx = 0
    for t in trades:
        try:
            n = int(t.get("id", "0"))
            if n > mx:
                mx = n
        except (TypeError, ValueError):
            pass
    return str(mx + 1)

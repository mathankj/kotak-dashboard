"""Paper-trades JSON store.

data/paper_trades.json. Writes are atomic (tmp + os.replace) and the file has
a per-path lock (see _safe_io.file_lock) so the read-modify-write sequences in
strategy/{stocks,options}.py are serialised across threads.
"""
import os

from backend.storage._safe_io import atomic_write_json, file_lock, read_json

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAPER_FILE = os.path.join(_REPO_ROOT, "data", "paper_trades.json")


def read_paper_trades():
    return read_json(PAPER_FILE, [])


def write_paper_trades(trades):
    try:
        with file_lock(PAPER_FILE):
            atomic_write_json(PAPER_FILE, trades)
    except Exception:
        pass


def next_paper_id(trades):
    """Return the next sequential paper-trade id as a string."""
    mx = 0
    for t in trades:
        try:
            n = int(t.get("id", "0"))
            if n > mx:
                mx = n
        except (TypeError, ValueError):
            pass
    return str(mx + 1)

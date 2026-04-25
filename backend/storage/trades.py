"""Paper-trades JSON store.

Plain JSON file at the repo root (paper_trades.json). Phase 5 will add atomic
writes + file locks; this Phase 4 version preserves the original semantics
exactly so strategy modules can depend on a stable interface.
"""
import json
import os

PAPER_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "paper_trades.json",
)


def read_paper_trades():
    try:
        if os.path.exists(PAPER_FILE):
            with open(PAPER_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def write_paper_trades(trades):
    try:
        with open(PAPER_FILE, "w") as f:
            json.dump(trades, f, indent=2)
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

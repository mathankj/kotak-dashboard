"""Blocked-attempts JSONL store.

data/blocked_attempts.jsonl. Append-only line-delimited JSON, one record
per blocked order attempt. Used by the /blockers UI tab and the real-time
toaster on the dashboard.

A "blocked attempt" is any order the auto-strategy or manual ticket TRIED
to place but the safety wrapper refused — insufficient margin, kill switch
engaged, broker error, position-not-found-at-Kotak before exit, etc.

Why JSONL not JSON:
  Append-only writes are crash-safe (no read-modify-write race), and JSONL
  doesn't grow a giant in-memory list — we tail the last N lines for the UI.

Format of each line:
  {
    "ts": "2026-04-27T09:23:14+05:30",
    "kind": "ENTRY" | "EXIT",
    "scrip": "NIFTY 25500 CE",
    "underlying": "NIFTY",
    "strike": 25500,
    "option_type": "CE",
    "side": "B" | "S",
    "qty": 75,
    "price": 120.0,
    "trading_symbol": "NIFTY01MAY2625500CE",
    "result": "BLOCKED_MARGIN" | "BLOCKED_HALTED" | "KOTAK_ERROR" | "EXIT_REFUSED_NO_KOTAK_POSITION" | ...,
    "message": "Insufficient margin. Need approx Rs.9,000.00, available Rs.0.00.",
    "trigger_spot": 25503.5,
    "trigger_level": "BUY",
    "source": "auto_options" | "manual_ticket"
  }
"""
import json
import os

from backend.utils import now_ist

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BLOCKED_FILE = os.path.join(_REPO_ROOT, "data", "blocked_attempts.jsonl")


def append_blocked(*, kind, scrip, side, qty, price, result, message,
                   underlying=None, strike=None, option_type=None,
                   trading_symbol=None, trigger_spot=None,
                   trigger_level=None, source="auto_options"):
    """Append one blocked-attempt record. Best-effort — never raises."""
    record = {
        "ts": now_ist().isoformat(),
        "kind": kind,
        "scrip": scrip,
        "underlying": underlying,
        "strike": strike,
        "option_type": option_type,
        "side": side,
        "qty": qty,
        "price": price,
        "trading_symbol": trading_symbol,
        "result": result,
        "message": message,
        "trigger_spot": trigger_spot,
        "trigger_level": trigger_level,
        "source": source,
    }
    try:
        os.makedirs(os.path.dirname(BLOCKED_FILE), exist_ok=True)
        with open(BLOCKED_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass
    return record


def read_recent_blocked(n=200):
    """Return the last `n` blocked-attempt records, newest first.
    Returns [] if the file doesn't exist yet.
    """
    if not os.path.exists(BLOCKED_FILE):
        return []
    try:
        with open(BLOCKED_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    out.reverse()
    return out


def read_blocked_since(since_ts):
    """Return blocked-attempt records strictly newer than `since_ts` (ISO string).
    Used by the toaster's poll endpoint. Always returns at most the last 50,
    newest first, to bound the payload.
    """
    rows = read_recent_blocked(50)
    if not since_ts:
        return rows
    return [r for r in rows if (r.get("ts") or "") > since_ts]

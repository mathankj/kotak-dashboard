"""Trade ledger JSON store.

data/trade_ledger.json. Writes are atomic (tmp + os.replace) and the file has
a per-path lock (see _safe_io.file_lock) so the read-modify-write sequences in
strategy/options.py are serialised across threads.

Carries every executed trade — LIVE rows from the auto-strategy, plus any
manual orders that flowed through place_order_safe. Each row has a Kotak
order id (for LIVE) and a `mode` field for forensics.

Migration: on first import we one-shot rename data/paper_trades.json
(legacy name from when this was paper-only) to data/trade_ledger.json so
existing data is not lost.
"""
import json
import os
import threading

from backend.storage._safe_io import atomic_write_json, file_lock, read_json

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEDGER_FILE = os.path.join(_REPO_ROOT, "data", "trade_ledger.json")
_LEGACY_FILE = os.path.join(_REPO_ROOT, "data", "paper_trades.json")

# D.2 — memoize the parsed ledger keyed on (mtime, size). The snapshot
# producer reads the ledger 3x per 2s tick (options + gann + futures
# builders), and request handlers (/api/trades, /trades, /paper-trades,
# the today-P&L context processor) also call it. With ~hundreds of rows
# this was a measurable hot path. Cache stores a JSON blob so each call
# json.loads() into FRESH dicts — preserves the existing semantics where
# callers may mutate the returned list (e.g. options.py inserts a new row
# then calls write_trade_ledger which invalidates the cache).
_LEDGER_CACHE = {"key": None, "json": "[]"}
_LEDGER_CACHE_LOCK = threading.Lock()


def _migrate_legacy_file_once():
    """One-time rename: data/paper_trades.json -> data/trade_ledger.json.
    Idempotent: if the target already exists, the legacy file is left alone
    (we don't merge — production data wins).
    """
    if os.path.exists(LEDGER_FILE):
        return
    if not os.path.exists(_LEGACY_FILE):
        return
    try:
        os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
        os.replace(_LEGACY_FILE, LEDGER_FILE)
    except Exception:
        # Migration is best-effort. If it fails, the next read will just
        # return [] and operation continues with an empty ledger.
        pass


_migrate_legacy_file_once()


def read_trade_ledger():
    """Return the parsed ledger. D.2 — memoized; see _LEDGER_CACHE comment."""
    try:
        st = os.stat(LEDGER_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        # No file yet (or unreadable). Skip cache so we never serve a
        # stale list when the ledger is recreated.
        return []
    with _LEDGER_CACHE_LOCK:
        if _LEDGER_CACHE["key"] == key:
            return json.loads(_LEDGER_CACHE["json"])
    rows = read_json(LEDGER_FILE, [])
    try:
        blob = json.dumps(rows)
    except (TypeError, ValueError):
        # Non-serialisable row (shouldn't happen — atomic_write writes
        # JSON). Bypass cache rather than raising.
        return rows
    with _LEDGER_CACHE_LOCK:
        _LEDGER_CACHE["key"] = key
        _LEDGER_CACHE["json"] = blob
    return rows


def write_trade_ledger(trades):
    try:
        with file_lock(LEDGER_FILE):
            atomic_write_json(LEDGER_FILE, trades)
        # D.2 — force next read to re-stat so the cache picks up the new
        # mtime even if the file's stat resolution is coarse on this OS.
        with _LEDGER_CACHE_LOCK:
            _LEDGER_CACHE["key"] = None
    except Exception:
        pass


def next_trade_id(trades):
    """Return the next sequential trade id as a string."""
    mx = 0
    for t in trades:
        try:
            n = int(t.get("id", "0"))
            if n > mx:
                mx = n
        except (TypeError, ValueError):
            pass
    return str(mx + 1)

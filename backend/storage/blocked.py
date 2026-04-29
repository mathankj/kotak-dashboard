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
import threading
from datetime import datetime

from backend.utils import now_ist

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BLOCKED_FILE = os.path.join(_REPO_ROOT, "data", "blocked_attempts.jsonl")

# D.3 — in-process memo for read_recent_blocked. The /api/recent-blocks
# toaster polls every 3s on every open page, and a 200-page-deep tail-read
# was the dominant tail-latency source (p95 180ms). Invalidated when this
# process calls append_blocked OR when the underlying file's mtime/size
# changes (handles other writers — e.g. a manual edit or a separate
# script). Only memoizes the last-N read since that's all the toaster
# uses; pagination still re-parses for the /blockers page.
_RECENT_CACHE = {"key": None, "rows": []}
_RECENT_CACHE_LOCK = threading.Lock()


def _bump_recent_cache():
    """Forced invalidation hook — called from append_blocked so the next
    read_recent_blocked call re-parses without waiting for mtime to flip."""
    with _RECENT_CACHE_LOCK:
        _RECENT_CACHE["key"] = None


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
        _bump_recent_cache()  # D.3 — force next toaster read to re-parse.
    except Exception:
        pass
    return record


def read_recent_blocked(n=200):
    """Return the last `n` blocked-attempt records, newest first.
    Returns [] if the file doesn't exist yet.

    D.3 — memoized on (mtime, size, n). The /api/recent-blocks toaster
    polls every 3s; without this cache each call re-read the full
    JSONL even when nothing had changed.
    """
    if not os.path.exists(BLOCKED_FILE):
        return []
    try:
        st = os.stat(BLOCKED_FILE)
        cache_key = (st.st_mtime_ns, st.st_size, n)
    except OSError:
        cache_key = None
    if cache_key is not None:
        with _RECENT_CACHE_LOCK:
            if _RECENT_CACHE["key"] == cache_key:
                # Return a shallow copy of the cached list — the rows
                # themselves are fresh dicts from json.loads so mutating
                # the returned list slice can't corrupt the cache.
                return list(_RECENT_CACHE["rows"])
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
    if cache_key is not None:
        with _RECENT_CACHE_LOCK:
            _RECENT_CACHE["key"] = cache_key
            _RECENT_CACHE["rows"] = list(out)
    return out


def _parse_iso(ts):
    """Parse an ISO-8601 timestamp into a tz-aware datetime, or None.

    Accepts both server format (IST `+05:30`) and browser `Date.toISOString()`
    output (UTC `Z`). We normalize `Z` -> `+00:00` because Python's
    `fromisoformat` only learned to handle `Z` in 3.11. After parsing we
    can compare across timezones safely (string compare cannot — `"04..."`
    sorts before `"09..."` even when 04:00 UTC is later than 09:00 IST).
    """
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(s)
    except Exception:
        return None


def read_blocked_page(page=1, page_size=50, date=None, kind=None, source=None):
    """Return one page of blocked-attempt records, newest first.

    page:      1-based page number (clamped to 1..pages).
    page_size: rows per page.
    date:      optional 'YYYY-MM-DD' filter — only rows whose `ts`
               starts with that prefix are included.

    Returns: {
      "items":     [<page slice>, newest first],
      "total":     int (matching the optional date filter),
      "page":      int (clamped),
      "page_size": int,
      "pages":     int (>=1),
    }

    The full file is parsed every call. JSONL stays cheap to stream and
    we only render `page_size` rows so the browser never gets the
    full list at once — that's the entire point of paginating: stop
    hauling thousands of rows over the wire just to throw them away.
    """
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, min(500, int(page_size)))
    except (TypeError, ValueError):
        page_size = 50
    date_prefix = (date or "").strip()[:10] or None
    kind_filter = (kind or "").strip() or None
    source_filter = (source or "").strip() or None

    empty = {"items": [], "total": 0, "page": 1,
             "page_size": page_size, "pages": 1,
             "distinct_kinds": [], "distinct_sources": []}
    if not os.path.exists(BLOCKED_FILE):
        return empty
    try:
        with open(BLOCKED_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return empty
    # Distinct kind/source values are gathered from the date-filtered slice
    # so the dropdowns only offer choices that exist for the chosen day.
    parsed = []
    kinds = set()
    sources = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if date_prefix and not str(r.get("ts", ""))[:10] == date_prefix:
            continue
        k = r.get("kind")
        s = r.get("source")
        if k:
            kinds.add(k)
        if s:
            sources.add(s)
        if kind_filter and k != kind_filter:
            continue
        if source_filter and s != source_filter:
            continue
        parsed.append(r)
    parsed.reverse()  # newest first
    total = len(parsed)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    items = parsed[start:start + page_size]
    return {"items": items, "total": total, "page": page,
            "page_size": page_size, "pages": pages,
            "distinct_kinds": sorted(kinds),
            "distinct_sources": sorted(sources)}


def read_blocked_since(since_ts):
    """Return blocked-attempt records strictly newer than `since_ts` (ISO string).
    Used by the toaster's poll endpoint. Always returns at most the last 50,
    newest first, to bound the payload.

    Cross-timezone safe: the browser cursor is UTC (`...Z`) but server records
    are IST (`+05:30`). We parse both to tz-aware datetimes so the comparison
    is real wall-clock ordering, not lexicographic string ordering.
    """
    rows = read_recent_blocked(50)
    if not since_ts:
        return rows
    since_dt = _parse_iso(since_ts)
    if since_dt is None:
        # Unparseable cursor -> treat as no cursor (return everything).
        return rows
    out = []
    for r in rows:
        r_dt = _parse_iso(r.get("ts"))
        if r_dt is not None and r_dt > since_dt:
            out.append(r)
    return out

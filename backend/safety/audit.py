"""Append-only audit log of every order intent.

Plain English:
  Every time the bot considers placing an order — whether it ends up sending
  to Kotak (LIVE) or just recording a paper trade — one line is appended to
  `data/audit.log`. The line is JSON so it stays grep-able AND machine
  parseable, but each line stands alone (JSONL, not a JSON array) so an
  abrupt process kill never corrupts the file.

What gets logged:
  * Every place_order_safe() call (intent, blocks, successes, failures)
  * Every kill switch toggle (HALT / UNHALT)
  * Every LIVE_MODE state change announced at startup

The log is NEVER rotated/truncated automatically. Ganesh wants to be able
to read it line-by-line for forensics. If it gets too big later, archive
manually (mv audit.log audit.YYYY-MM.log).
"""
import json
import os
from datetime import datetime

from backend.utils import now_ist

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDIT_FILE = os.path.join(_REPO_ROOT, "data", "audit.log")


def audit(event, **fields):
    """Append one event to the audit log. Never raises — audit failures
    must not break order flow.

    event: short uppercase string ("PLACE_ORDER", "KILL_SWITCH_HALT",
           "MODE_STARTUP", "BLOCKED_HALTED", "BLOCKED_MARGIN", ...)
    fields: anything JSON-serialisable — scrip, side, qty, price, reason, etc.
    """
    try:
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        record = {
            "ts": now_ist().isoformat(),
            "event": event,
            **fields,
        }
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        # Last-resort: if we can't even write to disk, print so something
        # appears in journalctl / app.log. Never propagate.
        try:
            print(f"[audit-fail] {event} {fields}")
        except Exception:
            pass


def read_audit_tail(n=100):
    """Read last n lines of the audit log as parsed JSON records."""
    if not os.path.exists(AUDIT_FILE):
        return []
    try:
        with open(AUDIT_FILE, "r") as f:
            lines = f.readlines()
        out = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"raw": line})
        return out
    except Exception:
        return []


def read_audit_page(page=1, page_size=50, date=None, event=None):
    """Return one page of audit events, newest first.

    Same shape/semantics as storage.blocked.read_blocked_page — see that
    docstring for the rationale. Kept inline (not factored into a shared
    helper) because the two stores are independent and might diverge.

    F.4: optional `event` filter narrows to a single event type
    (e.g. PLACE_ORDER_OK). The response also carries `distinct_events`
    — every event type seen in the date-filtered slice — so the template
    can populate its dropdown without re-reading the file.
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
    event_filter = (event or "").strip() or None

    empty = {"items": [], "total": 0, "page": 1,
             "page_size": page_size, "pages": 1, "distinct_events": []}
    if not os.path.exists(AUDIT_FILE):
        return empty
    try:
        with open(AUDIT_FILE, "r") as f:
            lines = f.readlines()
    except Exception:
        return empty
    # Two-pass walk: distinct event types come from the date-filtered slice
    # (so the dropdown only offers values that actually exist for the day),
    # then the event filter is applied to produce the page slice.
    parsed = []
    distinct = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            r = {"raw": line}
        if date_prefix and not str(r.get("ts", ""))[:10] == date_prefix:
            continue
        ev = r.get("event")
        if ev:
            distinct.add(ev)
        if event_filter and ev != event_filter:
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
            "distinct_events": sorted(distinct)}

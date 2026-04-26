"""Pending order intents — the manual-confirm gate for live trading.

Plain English:
  When the auto-strategy sees a signal, it does NOT fire the order. It writes
  a 'pending intent' to data/pending_intents.json with a 60-second TTL.

  The dashboard polls /api/pending-intents every 2s. When an intent is open,
  a sticky red banner appears at the top of every page:

      "PENDING ENTRY: BUY 1 lot NIFTY 25000 CE @ Rs.42.50  expires 0:23
       [CONFIRM]  [REJECT]"

  Ganesh clicks CONFIRM -> place_order_safe() fires the real Kotak order.
  Ganesh clicks REJECT  -> intent discarded, audit-logged.
  60 seconds elapse     -> intent auto-expires, no order, audit-logged.

  This is the Phase 3 safety net: every live order requires human consent.
  After 2-3 days of clean rehearsal we remove the gate (Phase 4).

Statuses:
  PENDING   - waiting for human action (visible in banner)
  CONFIRMED - human clicked confirm, order in flight (kept briefly for UI)
  REJECTED  - human clicked reject
  EXPIRED   - 60s elapsed without action
  PLACED    - terminal: Kotak accepted the order (kotak_order_id set)
  FAILED    - terminal: Kotak rejected / errored (error message set)

The file holds last 200 intents (newest first) for history. Only PENDING
ones drive the banner.
"""
import os
import time
import uuid

from backend.storage._safe_io import atomic_write_json, file_lock, read_json
from backend.utils import now_ist


_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INTENTS_FILE = os.path.join(_REPO_ROOT, "data", "pending_intents.json")

# How long a pending intent stays open before auto-expiring.
# 60s = enough for Ganesh to look at his phone, not so long that the price
# has moved meaningfully past the trigger.
INTENT_TTL_SECONDS = 60


def _now_ts():
    return time.time()


def _read():
    return read_json(INTENTS_FILE, [])


def _write(rows):
    atomic_write_json(INTENTS_FILE, rows[:200])  # keep last 200


def queue_intent(*, kind, scrip_symbol, trading_symbol, exchange, side, qty,
                 price, lot_size, source, extra=None):
    """Create a new PENDING intent and return it.

    kind:           "ENTRY" or "EXIT"
    scrip_symbol:   human-readable scrip ("NIFTY 25000 CE")
    trading_symbol: what Kotak place_order needs (e.g. "NIFTY26APR25000CE")
    exchange:       Kotak exchange_segment ("nse_fo", "bse_fo", ...)
    side:           "B" (buy) or "S" (sell)
    qty:            integer qty (already multiplied by lot_size if applicable)
    price:          numeric LTP at signal time (used for LIMIT order)
    lot_size:       lots count (display only — qty is already in shares)
    source:         "auto_options" / "manual_ticket" / etc.
    extra:          dict of strategy-specific context (underlying, exit_reason, etc.)
                    Survives in audit log + trade ledger.
    """
    intent = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "scrip_symbol": scrip_symbol,
        "trading_symbol": trading_symbol,
        "exchange": exchange,
        "side": side,
        "qty": int(qty),
        "price": round(float(price), 2),
        "lot_size": int(lot_size),
        "source": source,
        "status": "PENDING",
        "created_ts": _now_ts(),
        "created_at": now_ist().strftime("%H:%M:%S"),
        "expires_at_ts": _now_ts() + INTENT_TTL_SECONDS,
        "kotak_order_id": None,
        "error": None,
        "extra": extra or {},
    }
    with file_lock(INTENTS_FILE):
        rows = _read()
        rows.insert(0, intent)
        _write(rows)
    return intent


def list_active():
    """All non-terminal intents whose TTL hasn't elapsed.

    Auto-expires intents past their TTL as a side effect (so the banner
    never shows a stale signal even if no one calls expire_old).
    """
    expire_old()
    return [r for r in _read() if r.get("status") == "PENDING"]


def list_recent(n=50):
    """Last n intents (newest first) for an audit-style view."""
    return _read()[:n]


def get(intent_id):
    for r in _read():
        if r.get("id") == intent_id:
            return r
    return None


def _update(intent_id, **fields):
    """Merge fields into an intent. Returns the updated row, or None."""
    with file_lock(INTENTS_FILE):
        rows = _read()
        for r in rows:
            if r.get("id") == intent_id:
                r.update(fields)
                _write(rows)
                return r
    return None


def mark_confirmed(intent_id):
    return _update(intent_id, status="CONFIRMED",
                   confirmed_ts=_now_ts(),
                   confirmed_at=now_ist().strftime("%H:%M:%S"))


def mark_rejected(intent_id, reason="user_rejected"):
    return _update(intent_id, status="REJECTED",
                   rejected_ts=_now_ts(),
                   rejected_at=now_ist().strftime("%H:%M:%S"),
                   error=reason)


def mark_placed(intent_id, kotak_order_id):
    return _update(intent_id, status="PLACED",
                   kotak_order_id=str(kotak_order_id),
                   placed_ts=_now_ts(),
                   placed_at=now_ist().strftime("%H:%M:%S"))


def mark_failed(intent_id, error):
    return _update(intent_id, status="FAILED",
                   failed_ts=_now_ts(),
                   failed_at=now_ist().strftime("%H:%M:%S"),
                   error=str(error))


def expire_old():
    """Move any PENDING intent past its TTL to EXPIRED. Idempotent.
    Called by list_active() so the banner always reflects fresh state."""
    now = _now_ts()
    changed = False
    with file_lock(INTENTS_FILE):
        rows = _read()
        for r in rows:
            if r.get("status") == "PENDING" and r.get("expires_at_ts", 0) < now:
                r["status"] = "EXPIRED"
                r["expired_ts"] = now
                r["expired_at"] = now_ist().strftime("%H:%M:%S")
                changed = True
        if changed:
            _write(rows)


def has_pending_for(scrip_symbol):
    """True if there's already a PENDING intent for this scrip — prevents
    the auto-strategy from queueing 30 duplicate intents in 60s while
    Ganesh decides on the first one."""
    return any(r.get("scrip_symbol") == scrip_symbol
               and r.get("status") == "PENDING"
               for r in list_active())

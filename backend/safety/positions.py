"""Kotak position verification — used before any LIVE exit.

Plain English:
  Before we send a SELL to close an open position, we ask Kotak: "do you
  actually have this position open?" If Kotak says no (zero qty), we DON'T
  send the SELL — that would be a fresh short, not a close.

Why this exists:
  Our local `paper_trades.json` says "we have 1 lot NIFTY 25000 CE OPEN".
  But what if:
    - The order never filled at Kotak?
    - Ganesh closed it manually from the Kotak app?
    - It got auto-squared by Kotak risk management?
  In all three cases, sending our auto-exit SELL would OPEN a fresh short
  position. That is exactly the wrong thing.

  This module's `verify_open_position()` is the gate: it returns True only
  if Kotak's positions endpoint shows a non-zero qty for the same trading
  symbol on the same side as our local OPEN trade.

Best-effort:
  If Kotak's positions call fails (network, rate-limit), we treat that as
  "unknown" and BLOCK the exit — better to leave a position open one more
  tick than to fire a wrong-direction order.
"""
from backend.kotak.client import safe_call


def _norm(s):
    return str(s or "").strip().upper()


def verify_open_position(client, trading_symbol, side="BUY"):
    """Return (ok, info) where ok is True if Kotak shows a matching open
    position for this trading symbol.

    side : "BUY" or "B" if our local OPEN trade was a BUY (we're closing
           with a SELL). "SELL" or "S" if we were short (we're closing
           with a BUY). Used to compare against Kotak's net qty sign.
    info : dict with `qty`, `raw`, `error` so caller can log/audit.

    Returns (False, ...) on:
      - Kotak call failure (treat as unknown — fail closed)
      - No matching trading_symbol in positions
      - Matching symbol but qty is 0 / opposite sign
    """
    info = {"qty": 0, "raw": None, "error": None}
    try:
        data, err = safe_call(client.positions)
    except Exception as e:
        info["error"] = f"positions_call_exception: {type(e).__name__}: {e}"
        return False, info
    if err:
        info["error"] = f"positions_call_error: {err}"
        return False, info

    info["raw"] = data
    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        # Kotak sometimes wraps in {"data": [...]}; tolerate both
        rows = data.get("data") if isinstance(data.get("data"), list) else [data]

    target = _norm(trading_symbol)
    expected_buy = _norm(side) in ("BUY", "B")

    for r in rows:
        if not isinstance(r, dict):
            continue
        # Kotak positions row keys vary: trdSym / tradingSymbol / tSym
        sym = _norm(r.get("trdSym")
                    or r.get("tradingSymbol")
                    or r.get("tSym")
                    or r.get("symbol"))
        if sym != target:
            continue
        # Net qty field also varies: flBuyQty - flSellQty, netTrdQtyLot, etc.
        # Try a few common keys; default to 0 if all missing.
        qty = None
        for k in ("netQty", "netTrdQty", "netTrdQtyLot",
                  "flBuyQty", "buyQty"):
            if k in r:
                try:
                    qty = float(r[k])
                    break
                except (TypeError, ValueError):
                    pass
        if qty is None:
            qty = 0
        # If we recorded a BUY, expect a positive net qty (long position).
        # If we recorded a SELL, expect a negative net qty (short position).
        info["qty"] = qty
        if expected_buy and qty > 0:
            return True, info
        if (not expected_buy) and qty < 0:
            return True, info
        # Symbol matched but qty was 0 / opposite sign -> position not open
        # the way we recorded it. Refuse.
        info["error"] = (f"position_mismatch: kotak_qty={qty} "
                         f"recorded_side={side}")
        return False, info

    info["error"] = "position_not_found_at_kotak"
    return False, info

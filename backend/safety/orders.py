"""place_order_safe — the ONE function that ever places a real Kotak order.

Plain English:
  ALL order placement (manual order ticket UI, future auto-strategy live
  orders) MUST go through this wrapper. It is the single choke-point where
  every safety check happens, in this order:

      1. Audit  : log the INTENT before doing anything else
      2. Mode   : if LIVE_MODE is False -> paper-only, return without calling Kotak
      3. Halt   : if kill switch is engaged -> refuse, return clear error
      4. Margin : if not enough cash for (price * qty * lot) -> refuse, friendly error
      5. Place  : try/except around client.place_order, log the result

  Exactly ONE place to read, exactly ONE place that can fire a real order.

LIVE_MODE flag lives in `backend/strategy/options.py` (not here) because
that's the file Ganesh opens when reading strategy logic — having the
master safety switch in his line of sight matters more than co-locating
it with this wrapper.
"""
import json

from backend.safety.audit import audit
from backend.safety.kill_switch import is_halted


# Sentinel codes returned in the result dict so callers can branch reliably
# without parsing free-text error messages.
RESULT_OK              = "OK"
RESULT_PAPER           = "PAPER"            # LIVE_MODE was False — no Kotak call
RESULT_BLOCKED_HALTED  = "BLOCKED_HALTED"
RESULT_BLOCKED_MARGIN  = "BLOCKED_MARGIN"
RESULT_BLOCKED_VALIDATION = "BLOCKED_VALIDATION"
RESULT_KOTAK_ERROR     = "KOTAK_ERROR"


def place_order_safe(*, client, scrip, side, qty, price,
                     order_type="L", product="MIS", validity="DAY",
                     trigger="0", tag="bot",
                     live_mode, available_cash=None,
                     lot_size=1, source="manual"):
    """Single safe entry-point for placing an order.

    Required kwargs (all named to prevent positional mistakes — financial
    code should never accept positional bag-of-numbers):
      client        : initialised Kotak NeoAPI client (only used when LIVE)
      scrip         : SCRIPS dict entry (has trading_symbol + exchange)
      side          : "B" or "S"
      qty           : int (already validated > 0 by caller)
      price         : numeric or string (already validated for LIMIT)
      live_mode     : bool — caller passes the current LIVE_MODE flag value.
                      Wrapper does NOT import options.py to avoid circular
                      imports; the flag travels in as data.
      available_cash: float or None. If None, margin check is skipped (caller
                      didn't fetch limits). If provided, wrapper compares
                      against price*qty*lot_size and refuses if short.
      lot_size      : int — informational only. `qty` is already total shares
                      (e.g. 75 for 1 lot of NIFTY). Recorded in audit log so we
                      can post-process "how many lots" later if needed.
      source        : free-text label ("manual_ticket" / "auto_options" / ...).
                      Lands in the audit log so we can trace WHO triggered.

    Returns: dict with keys {result, order_id, message, raw}
      result    : one of the RESULT_* constants above
      order_id  : Kotak order id string, or None
      message   : human-readable explanation (safe to show in the UI)
      raw       : raw Kotak response (for debug), or None
    """
    intent = {
        "source": source,
        "live_mode": bool(live_mode),
        "scrip": scrip.get("symbol") if isinstance(scrip, dict) else str(scrip),
        "side": side, "qty": qty, "price": price,
        "order_type": order_type, "product": product, "validity": validity,
        "tag": tag, "lot_size": lot_size,
    }

    # ---------------- (1) AUDIT INTENT ----------------
    audit("PLACE_ORDER_INTENT", **intent)

    # ---------------- (2) PAPER MODE GUARD ----------------
    if not live_mode:
        audit("PLACE_ORDER_PAPER", **intent)
        return {"result": RESULT_PAPER, "order_id": None,
                "message": "Paper mode: no real order placed",
                "raw": None}

    # ---------------- (3) KILL SWITCH GUARD ----------------
    if is_halted():
        audit("BLOCKED_HALTED", **intent)
        return {"result": RESULT_BLOCKED_HALTED, "order_id": None,
                "message": ("Trading is HALTED — kill switch is engaged. "
                            "Re-arm via SSH (rm data/HALTED.flag) "
                            "after investigating."),
                "raw": None}

    # ---------------- (4) MARGIN PRE-CHECK ----------------
    # Note: qty is total shares (e.g. 75 for 1 NIFTY lot at premium ₹120 ->
    # need = 9,000). lot_size is informational only — multiplying by it here
    # would double-count and falsely block trades on funded accounts.
    if available_cash is not None:
        try:
            need = float(price) * float(qty)
        except (TypeError, ValueError):
            need = None
        if need is not None and need > float(available_cash):
            msg = (f"Insufficient margin. Need approx Rs.{need:,.2f}, "
                   f"available Rs.{float(available_cash):,.2f}. "
                   f"Top up funds or reduce quantity.")
            audit("BLOCKED_MARGIN", need=need, available=available_cash, **intent)
            return {"result": RESULT_BLOCKED_MARGIN, "order_id": None,
                    "message": msg, "raw": None}

    # ---------------- (5) PLACE THE REAL ORDER ----------------
    try:
        resp = client.place_order(
            exchange_segment=scrip["exchange"],
            product=product,
            price=str(price) if order_type == "L" else "0",
            order_type=order_type,
            quantity=str(qty),
            validity=validity,
            trading_symbol=scrip["trading_symbol"],
            transaction_type=side,
            trigger_price=str(trigger),
            tag=tag,
        )
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        audit("PLACE_ORDER_EXCEPTION", error=msg, **intent)
        return {"result": RESULT_KOTAK_ERROR, "order_id": None,
                "message": (f"Broker call failed: {msg}. "
                            "Check connectivity and Kotak portal status."),
                "raw": None}

    # Parse Kotak response (same shape-handling as the original /api/place-order)
    if isinstance(resp, dict):
        oid = (resp.get("nOrdNo")
               or (resp.get("data") or {}).get("orderId")
               or (resp.get("data") or {}).get("nOrdNo"))
        err = resp.get("error") or resp.get("Error") or resp.get("errMsg")
        msg_field = (resp.get("stat")
                     or resp.get("Message")
                     or resp.get("statusDescription"))
        if oid:
            audit("PLACE_ORDER_OK", order_id=str(oid),
                  message=msg_field or "Order accepted", **intent)
            return {"result": RESULT_OK, "order_id": str(oid),
                    "message": msg_field or "Order accepted", "raw": resp}
        if err:
            err_str = err if isinstance(err, str) else json.dumps(err)
            audit("PLACE_ORDER_REJECTED", error=err_str, **intent)
            return {"result": RESULT_KOTAK_ERROR, "order_id": None,
                    "message": f"Broker rejected: {err_str}", "raw": resp}

    # Unexpected response shape — treat as failure, surface as much as possible
    raw_str = json.dumps(resp)[:200] if resp is not None else "None"
    audit("PLACE_ORDER_UNEXPECTED", raw=raw_str, **intent)
    return {"result": RESULT_KOTAK_ERROR, "order_id": None,
            "message": f"Unexpected broker response: {raw_str}", "raw": resp}

"""Auto-trading strategy for index options — Phase 3 (intent-gated).

Plain English:
  - Watch the index spot (NIFTY 50, BANKNIFTY, SENSEX).
  - When spot crosses BUY level upward -> propose buying 1 lot ATM CE.
  - When spot crosses SELL level downward -> propose buying 1 lot ATM PE.
  - For each OPEN trade, watch THREE exits (first hit wins):
      1. Stop loss   (3 variants — see EXIT block below; one is active)
      2. Profit T1   (CE: spot >= T1; PE: spot <= S1)
      3. Time exit   (force-close at 15:15 IST)

Phase 3 change vs. Phase 2:
  This module no longer writes to paper_trades.json directly. Instead, every
  signal becomes a "pending intent" via backend.strategy.pending_intents.
  Ganesh sees a banner on the dashboard, clicks CONFIRM, and ONLY THEN does
  /api/intent-confirm call place_order_safe() and write the trade record.

  When LIVE_MODE=False the flow is identical EXCEPT place_order_safe short-
  circuits to PAPER mode (no Kotak call). This lets Ganesh rehearse the full
  live UX with zero risk before flipping the master switch.

  Before queuing an EXIT intent in LIVE mode, we verify with Kotak that the
  position actually exists (via backend.safety.positions). If Kotak says no
  position, we DO NOT propose the exit — refuses to send a SELL into nothing
  (which would open a fresh short).

Note: the caller passes `gann_quotes` (the stock-side fetch_quotes result) so
this module doesn't need to import fetch_quotes — that would create an
app.py <-> strategy circular import. The `client` arg is optional; only
used to verify positions when LIVE_MODE=True.
"""
import threading

from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
from backend.safety.audit import audit
from backend.safety.positions import verify_open_position
from backend.storage.trades import read_paper_trades
from backend.strategy.common import (
    _auto_at_or_after_squareoff, _auto_in_hours,
)
from backend.strategy.pending_intents import (
    has_pending_for, queue_intent,
)
from backend.utils import now_ist


# ======================================================================
# !!!  LIVE TRADING MASTER SWITCH  !!!
# ======================================================================
# When True  -> place_order_safe() will fire REAL Kotak orders for every
#               auto-strategy entry / exit. Real money moves.
# When False -> auto-strategy keeps writing to paper_trades.json only.
#               No call to client.place_order ever happens. SAFE.
#
# Flip ceremony (Phase 3 only):
#   1. Confirm Phase 2 paper-mode tests passed for >= 2 days.
#   2. Confirm IP whitelist is active in Kotak Neo developer portal.
#   3. Confirm kill switch (header STOP button) works on staging.
#   4. Edit this line to True. Commit. Push. systemctl restart kotak.
#
# DO NOT flip this from a config file or env var. The deliberate
# code-edit + commit + restart IS the safety. - matha
# ======================================================================
LIVE_MODE = False

# ----------------------------------------------------------------------
# TUNABLES
# ----------------------------------------------------------------------
AUTO_OPTION_STRATEGY_ENABLED = True

# Set to None for unlimited entries per index per day.
# Or an int (e.g. 2) to cap trades per index per day.
MAX_TRADES_PER_INDEX_PER_DAY = None

# Used by the ₹5-fixed SL variant (only matters if that variant is active).
PREMIUM_SL_FIXED_POINTS = 5

# Used by the % SL variant (only matters if that variant is active).
# 0.30 = exit when premium drops to 70% of entry (a 30% loss).
PREMIUM_SL_PCT = 0.30
# ----------------------------------------------------------------------


_option_auto_state = {
    "last_spot": {},  # index_name -> last seen spot
    "lock": threading.Lock(),
}


def _check_exit_reason(open_t, opt_ltp, spot,
                       buy_lvl, sell_lvl, t1_lvl, s1_lvl):
    """Return the exit reason string for an open option trade, or None.

    =======================================================================
    EXIT CONDITIONS — checked in order, first hit wins.
    =======================================================================

    1) STOP LOSS — three variants. Only ONE is active at a time.
       To switch: comment out the active block, uncomment the desired one.
       Currently active: variant C (spot reaches opposite Gann level).

       Variant A — Fixed premium drop (e.g. ₹5):
       --------------------------------------------------------
       # if opt_ltp is not None and opt_ltp <= entry_price - PREMIUM_SL_FIXED_POINTS:
       #     return "SL_PREMIUM_FIXED"

       Variant B — Premium percentage drop (e.g. 30%):
       --------------------------------------------------------
       # if opt_ltp is not None and opt_ltp <= entry_price * (1 - PREMIUM_SL_PCT):
       #     return "SL_PREMIUM_PCT"

       Variant C — Spot reaches opposite Gann level (ACTIVE):
       --------------------------------------------------------
       (CE: spot < SELL level   |   PE: spot > BUY level)

    2) PROFIT TARGET — spot reaches T1 (CE) or S1 (PE).

    3) TIME SQUAREOFF — clock hits 15:15 IST. Handled at top of tick(),
       not in this function.
    =======================================================================
    """
    entry_price = open_t.get("entry_price")
    side = open_t.get("option_type")  # 'CE' or 'PE'

    # ---------- (1) STOP LOSS — pick ONE variant ----------

    # --- Variant A: fixed premium drop (e.g. ₹5 below entry) ---
    # if opt_ltp is not None and entry_price is not None:
    #     if opt_ltp <= entry_price - PREMIUM_SL_FIXED_POINTS:
    #         return "SL_PREMIUM_FIXED"

    # --- Variant B: percentage premium drop (e.g. 30%) ---
    # if opt_ltp is not None and entry_price is not None:
    #     if opt_ltp <= entry_price * (1 - PREMIUM_SL_PCT):
    #         return "SL_PREMIUM_PCT"

    # --- Variant C (ACTIVE): spot reverses through opposite Gann level ---
    if side == "CE":
        if sell_lvl is not None and spot < sell_lvl:
            return "SL_SELL_LVL"
    else:  # PE
        if buy_lvl is not None and spot > buy_lvl:
            return "SL_BUY_LVL"

    # ---------- (2) PROFIT TARGET ----------
    if side == "CE":
        if t1_lvl is not None and spot >= t1_lvl:
            return "TARGET_T1"
    else:  # PE
        if s1_lvl is not None and spot <= s1_lvl:
            return "TARGET_S1"

    return None


def _can_open_more(idx_name, counts):
    """Per-day cap check. None means unlimited."""
    if MAX_TRADES_PER_INDEX_PER_DAY is None:
        return True
    return counts.get(idx_name, 0) < MAX_TRADES_PER_INDEX_PER_DAY


def _option_trading_symbol(idx_name, atm, option_type, expiry):
    """Build the Kotak F&O trading symbol for an index option.

    Format example: NIFTY26APR25000CE   (yymmm + strike + CE/PE)
    `expiry` is whatever option_index_meta gave us — a date or a string. We
    accept both. If we can't form a clean symbol we return None and the
    intent is refused upstream (better than firing a malformed order).
    """
    if not expiry:
        return None
    try:
        # expiry may be a datetime.date or a string like "28Apr2026"
        if hasattr(expiry, "strftime"):
            tag = expiry.strftime("%d%b%y").upper()  # "28APR26"
        else:
            tag = str(expiry).upper().replace(" ", "")
        return f"{idx_name}{tag}{int(atm)}{option_type}"
    except Exception:
        return None


def option_auto_strategy_tick(option_data, option_index_meta, gann_quotes,
                              client=None):
    """One tick of the option auto-strategy.

    option_data:        {key: {index, strike, option_type, ltp, ...}}
    option_index_meta:  {index_name: {spot, atm, expiry, ...}}
    gann_quotes:        stock-side quotes (need .levels for the index spots)
    client:             Kotak NeoAPI client. Required when LIVE_MODE=True
                        (used by verify_open_position). Optional otherwise.

    The strategy NEVER writes paper_trades.json directly. It produces pending
    intents; the /api/intent-confirm route is the single place where trades
    actually get recorded (after Ganesh confirms and place_order_safe runs).
    """
    if not AUTO_OPTION_STRATEGY_ENABLED or not option_index_meta:
        return
    now = now_ist()

    with _option_auto_state["lock"]:
        trades = read_paper_trades()

        # 1. SQUARE OFF at/after 15:15 — propose EXIT intents for everything OPEN
        if _auto_at_or_after_squareoff(now):
            for t in trades:
                if (t.get("status") != "OPEN"
                        or t.get("asset_type") != "option"):
                    continue
                q = option_data.get(t.get("option_key"))
                if not q or q.get("ltp") is None:
                    continue
                _propose_exit(t, float(q["ltp"]), "AUTO_SQUARE_OFF",
                              option_index_meta, client)
            return

        if not _auto_in_hours(now):
            return

        # Daily per-index trade cap
        today = now.strftime("%Y-%m-%d")
        counts = {}
        for t in trades:
            if t.get("date") == today and t.get("asset_type") == "option":
                u = t.get("underlying", "")
                counts[u] = counts.get(u, 0) + 1
        open_by_underlying = {
            t["underlying"]: t for t in trades
            if t.get("status") == "OPEN" and t.get("asset_type") == "option"
        }

        for idx_name, m in option_index_meta.items():
            spot = m.get("spot")
            atm  = m.get("atm")
            if spot is None or atm is None:
                continue
            gann_sym = INDEX_OPTIONS_CONFIG[idx_name]["spot_symbol_key"]
            gq = gann_quotes.get(gann_sym) or {}
            levels  = gq.get("levels") or {}
            buy_lvl  = (levels.get("buy")  or {}).get("BUY")
            sell_lvl = (levels.get("sell") or {}).get("SELL")
            t1_lvl   = (levels.get("buy")  or {}).get("T1")
            s1_lvl   = (levels.get("sell") or {}).get("S1")
            prev_spot = _option_auto_state["last_spot"].get(idx_name)

            # ---- EXIT check ----
            open_t = open_by_underlying.get(idx_name)
            if open_t:
                opt_q = option_data.get(open_t.get("option_key"))
                opt_ltp = (opt_q or {}).get("ltp")
                reason = _check_exit_reason(
                    open_t, opt_ltp, spot,
                    buy_lvl, sell_lvl, t1_lvl, s1_lvl,
                )
                if reason and opt_ltp is not None:
                    _propose_exit(open_t, float(opt_ltp), reason,
                                  option_index_meta, client)
                    _option_auto_state["last_spot"][idx_name] = spot
                    continue

            # ---- ENTRY check — spot crossing a Gann level ----
            if (idx_name not in open_by_underlying
                    and _can_open_more(idx_name, counts)
                    and prev_spot is not None):
                option_type = None
                if buy_lvl is not None and prev_spot <= buy_lvl < spot:
                    option_type = "CE"
                elif sell_lvl is not None and prev_spot >= sell_lvl > spot:
                    option_type = "PE"

                if option_type:
                    opt_key = f"{idx_name} {atm} {option_type}"
                    opt_q = option_data.get(opt_key)
                    opt_ltp = (opt_q or {}).get("ltp")
                    if opt_ltp is not None:
                        _propose_entry(idx_name, atm, option_type, opt_key,
                                       float(opt_ltp), float(spot),
                                       m.get("expiry"))
                        # Don't increment counts here — we increment when
                        # the intent is CONFIRMED (in /api/intent-confirm).
                        # Otherwise rejecting an intent would still burn the cap.

            _option_auto_state["last_spot"][idx_name] = spot


def _propose_entry(idx_name, atm, option_type, opt_key,
                   opt_ltp, spot, expiry):
    """Queue a pending ENTRY intent. Skip if one is already pending for
    this scrip (avoids stacking 30 duplicates while Ganesh decides)."""
    if has_pending_for(opt_key):
        return
    cfg = INDEX_OPTIONS_CONFIG.get(idx_name) or {}
    lot_size = cfg.get("lot_size")
    if not lot_size:
        # Fail-safe: refuse to propose without a known lot size.
        audit("INTENT_REFUSED_NO_LOT_SIZE", scrip=opt_key, idx=idx_name)
        return
    trading_symbol = _option_trading_symbol(idx_name, atm, option_type, expiry)
    if not trading_symbol:
        audit("INTENT_REFUSED_NO_TRADING_SYMBOL",
              scrip=opt_key, idx=idx_name, expiry=str(expiry))
        return
    intent = queue_intent(
        kind="ENTRY",
        scrip_symbol=opt_key,
        trading_symbol=trading_symbol,
        exchange=cfg["exchange_segment"],
        side="B",
        qty=lot_size,                 # 1 lot in shares (e.g. 75 for NIFTY)
        price=opt_ltp,
        lot_size=lot_size,
        source="auto_options",
        extra={
            "asset_type": "option",
            "underlying": idx_name,
            "strike": atm,
            "option_type": option_type,
            "expiry": str(expiry) if expiry else None,
            "trigger_spot": round(spot, 2),
            "trigger_level": "BUY" if option_type == "CE" else "SELL",
        },
    )
    audit("INTENT_QUEUED_ENTRY", intent_id=intent["id"], scrip=opt_key,
          qty=intent["qty"], price=intent["price"])


def _propose_exit(open_trade, opt_ltp, reason, option_index_meta, client):
    """Queue a pending EXIT intent for an OPEN trade.

    LIVE_MODE additionally verifies with Kotak that the position is still
    open before queuing (don't send a SELL into thin air). PAPER skips
    verification — there's nothing real to verify.
    """
    opt_key = open_trade.get("option_key")
    if has_pending_for(opt_key):
        return
    idx_name = open_trade.get("underlying")
    cfg = INDEX_OPTIONS_CONFIG.get(idx_name) or {}
    lot_size = cfg.get("lot_size") or open_trade.get("qty") or 1
    # Trading symbol: prefer the one we recorded at entry (survives expiry
    # confusion); fall back to rebuilding it.
    trading_symbol = (open_trade.get("trading_symbol")
                      or _option_trading_symbol(
                          idx_name, open_trade.get("strike"),
                          open_trade.get("option_type"),
                          (option_index_meta.get(idx_name) or {}).get("expiry"),
                      ))
    if not trading_symbol:
        audit("EXIT_INTENT_REFUSED_NO_TRADING_SYMBOL", scrip=opt_key)
        return

    # LIVE only: verify Kotak shows the position open before proposing exit.
    if LIVE_MODE:
        if client is None:
            audit("EXIT_INTENT_REFUSED_NO_CLIENT", scrip=opt_key,
                  reason=reason)
            return
        ok, info = verify_open_position(client, trading_symbol, side="BUY")
        if not ok:
            audit("EXIT_INTENT_REFUSED_NO_KOTAK_POSITION",
                  scrip=opt_key, trading_symbol=trading_symbol,
                  kotak_info=info)
            return

    qty = int(open_trade.get("qty") or lot_size)
    intent = queue_intent(
        kind="EXIT",
        scrip_symbol=opt_key,
        trading_symbol=trading_symbol,
        exchange=cfg.get("exchange_segment", "nse_fo"),
        side="S",                     # closing a long with a SELL
        qty=qty,
        price=opt_ltp,
        lot_size=lot_size,
        source="auto_options",
        extra={
            "asset_type": "option",
            "underlying": idx_name,
            "exit_reason": reason,
            "linked_open_trade_id": open_trade.get("id"),
            "entry_price": open_trade.get("entry_price"),
        },
    )
    audit("INTENT_QUEUED_EXIT", intent_id=intent["id"], scrip=opt_key,
          reason=reason, qty=qty, price=opt_ltp)

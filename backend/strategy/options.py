"""Auto-trading strategy for index options — Phase 4 (LIVE, fully automatic).

Plain English:
  - Watch the index spot (NIFTY 50, BANKNIFTY, SENSEX).
  - ENTRY — two paths:
      (a) MARKET-OPEN: the first time we see an index today, if spot is
          ALREADY above BUY -> buy 1 lot ATM CE; if ALREADY below SELL ->
          buy 1 lot ATM PE. Fires once per index per day (gated by the
          ledger via counts[idx]>0, so a restart can't double-fire).
      (b) CROSSING: after the open evaluation, normal Gann logic — when
          spot crosses BUY upward -> CE; when spot crosses SELL downward
          -> PE.
  - For each OPEN trade, watch THREE exits (first hit wins):
      1. Stop loss   (3 variants — see EXIT block below; one is active)
      2. Profit T1   (CE: spot >= T1; PE: spot <= S1)
      3. Time exit   (force-close at 15:15 IST)

Phase 4 change vs. Phase 3:
  No more pending-intent banner. No human confirm step. Every signal goes
  straight through `place_order_safe()` and writes to the trade ledger.
  The single safety wrapper still gates: margin pre-check, kill switch,
  Kotak error handling. Position verification still runs before any EXIT
  (refuses to send a SELL if Kotak shows no matching open position).

  The kill switch (data/HALTED.flag, /STOP route) is the panic button.
  Auto-strategy keeps proposing signals while halted, but place_order_safe
  refuses to send them — they're audited as BLOCKED_HALTED and skipped.

LIVE_MODE master switch:
  Editing this constant is the ONE intentional ceremony to enable / disable
  real-money trading. Must be edited in source, committed, pushed, and the
  service restarted. No web toggle. - matha
"""
import threading

from backend.kotak.client import safe_call
from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
from backend.safety.audit import audit
from backend.safety.orders import (
    place_order_safe,
    RESULT_OK, RESULT_PAPER, RESULT_BLOCKED_HALTED,
    RESULT_BLOCKED_MARGIN, RESULT_KOTAK_ERROR,
)
from backend.safety.positions import verify_open_position
from backend.storage.blocked import append_blocked
from backend.storage.trades import (
    read_trade_ledger, write_trade_ledger, next_trade_id,
)
from backend.strategy.common import (
    _auto_at_or_after_squareoff, _auto_in_hours,
)
from backend.utils import now_ist


# ======================================================================
# !!!  LIVE TRADING MASTER SWITCH  !!!
# ======================================================================
# When True  -> place_order_safe() will fire REAL Kotak orders for every
#               auto-strategy entry / exit. Real money moves.
# When False -> place_order_safe() short-circuits to PAPER mode. No call
#               to client.place_order is ever made. SAFE rehearsal.
#
# Flip ceremony:
#   1. Confirm Phase 3 paper-mode tests passed for >= 2 days (recommended).
#   2. Confirm IP whitelist is active in Kotak Neo developer portal.
#   3. Confirm kill switch (header STOP button) works on staging.
#   4. Edit this line. Commit. Push. systemctl restart kotak.
#
# DO NOT flip this from a config file or env var. The deliberate
# code-edit + commit + restart IS the safety. - matha
# ======================================================================
LIVE_MODE = True

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
    "last_spot": {},        # index_name -> last seen spot
    "open_evaluated": {},   # index_name -> "YYYY-MM-DD" once we've done the
                            # market-open evaluation for that day. Resets at
                            # midnight (in-memory, but counts[idx] from the
                            # trade ledger is the persistent backstop — see
                            # ENTRY block in option_auto_strategy_tick).
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
    order is refused upstream (better than firing a malformed order).
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


def _fetch_available_cash(client):
    """Best-effort margin fetch. None means 'skip the pre-check'.

    place_order_safe treats available_cash=None as 'don't gate on margin'
    (so a flaky limits() call doesn't block legitimate trades). Margin is
    still ultimately enforced by Kotak itself.
    """
    if client is None:
        return None
    try:
        ld, _ = safe_call(client.limits, segment="ALL",
                          exchange="ALL", product="ALL")
        if isinstance(ld, dict):
            for k in ("Net", "net", "CashAvailable", "cashAvailable",
                      "AvailableCash", "availableCash", "DepositValue"):
                if k in ld:
                    try:
                        return float(ld[k])
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    return None


def option_auto_strategy_tick(option_data, option_index_meta, gann_quotes,
                              client=None):
    """One tick of the option auto-strategy.

    option_data:        {key: {index, strike, option_type, ltp, ...}}
    option_index_meta:  {index_name: {spot, atm, expiry, ...}}
    gann_quotes:        stock-side quotes (need .levels for the index spots)
    client:             Kotak NeoAPI client. Required when LIVE_MODE=True.

    Phase 4: every signal goes straight through place_order_safe and writes
    a trade row on success. No banner, no human confirm.
    """
    if not AUTO_OPTION_STRATEGY_ENABLED or not option_index_meta:
        return
    now = now_ist()

    with _option_auto_state["lock"]:
        trades = read_trade_ledger()

        # 1. SQUARE OFF at/after 15:15 — close everything OPEN
        if _auto_at_or_after_squareoff(now):
            for t in trades:
                if (t.get("status") != "OPEN"
                        or t.get("asset_type") != "option"):
                    continue
                q = option_data.get(t.get("option_key"))
                if not q or q.get("ltp") is None:
                    continue
                _execute_exit(t, float(q["ltp"]), "AUTO_SQUARE_OFF",
                              option_index_meta, client)
            return

        if not _auto_in_hours(now):
            return

        # Daily per-index trade cap — counts trades placed today
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
                    _execute_exit(open_t, float(opt_ltp), reason,
                                  option_index_meta, client)
                    _option_auto_state["last_spot"][idx_name] = spot
                    continue

            # ---- ENTRY check ----
            # Two paths:
            #   (a) MARKET-OPEN evaluation — the first time we see this index
            #       today (no auto trade yet, no prior tick recorded), fire
            #       immediately if spot is already past a level. This is what
            #       Ganesh wants: "if NIFTY opens above BUY, just buy CE."
            #       counts[idx] is the persistent (ledger-derived) gate that
            #       prevents this firing twice on the same day across restarts.
            #   (b) CROSSING — for every subsequent tick, the original strict
            #       "prev_spot ≤ BUY < spot" rule applies.
            if (idx_name not in open_by_underlying
                    and _can_open_more(idx_name, counts)):
                option_type = None
                today_str = today  # already computed above
                already_evaluated_open = (
                    _option_auto_state["open_evaluated"].get(idx_name)
                        == today_str
                    or counts.get(idx_name, 0) > 0
                )
                if not already_evaluated_open:
                    # (a) MARKET-OPEN evaluation
                    if buy_lvl is not None and spot > buy_lvl:
                        option_type = "CE"
                    elif sell_lvl is not None and spot < sell_lvl:
                        option_type = "PE"
                    if option_type is None:
                        # Spot is in the channel — no signal at open. Mark
                        # evaluated so we don't keep re-checking; subsequent
                        # ticks fall into the crossing branch as intended.
                        _option_auto_state["open_evaluated"][idx_name] = today_str
                    # If option_type IS set, we leave open_evaluated UNSET
                    # here. It gets stamped down below right before the
                    # _execute_entry call — so a missing opt_ltp at this
                    # exact tick won't burn the open-evaluation slot for
                    # the day. Retries on the next tick when LTP lands.
                elif prev_spot is not None:
                    # (b) CROSSING
                    if buy_lvl is not None and prev_spot <= buy_lvl < spot:
                        option_type = "CE"
                    elif sell_lvl is not None and prev_spot >= sell_lvl > spot:
                        option_type = "PE"

                if option_type:
                    opt_key = f"{idx_name} {atm} {option_type}"
                    opt_q = option_data.get(opt_key)
                    opt_ltp = (opt_q or {}).get("ltp")
                    if opt_ltp is not None:
                        # Stamp open_evaluated NOW (whether the order
                        # ultimately succeeds, blocks on margin, or hits a
                        # Kotak error). Either way we tried — don't burn the
                        # slot earlier when LTP might still be missing.
                        _option_auto_state["open_evaluated"][idx_name] = today_str
                        placed = _execute_entry(
                            idx_name, atm, option_type, opt_key,
                            float(opt_ltp), float(spot),
                            m.get("expiry"), client,
                        )
                        if placed:
                            counts[idx_name] = counts.get(idx_name, 0) + 1

            _option_auto_state["last_spot"][idx_name] = spot


def _execute_entry(idx_name, atm, option_type, opt_key,
                   opt_ltp, spot, expiry, client):
    """Place a real BUY order via place_order_safe and write trade ledger row.

    Returns True if the order was PLACED (LIVE) or recorded (PAPER), False
    if it was refused. The caller uses this to decide whether to bump the
    daily count.
    """
    cfg = INDEX_OPTIONS_CONFIG.get(idx_name) or {}
    lot_size = cfg.get("lot_size")
    if not lot_size:
        audit("ORDER_REFUSED_NO_LOT_SIZE", scrip=opt_key, idx=idx_name)
        append_blocked(
            kind="ENTRY", scrip=opt_key, side="B", qty=0, price=opt_ltp,
            result="NO_LOT_SIZE",
            message=f"No lot size configured for {idx_name}",
            underlying=idx_name, strike=atm, option_type=option_type,
            trigger_spot=spot,
            trigger_level="BUY" if option_type == "CE" else "SELL",
        )
        return False
    trading_symbol = _option_trading_symbol(idx_name, atm, option_type, expiry)
    if not trading_symbol:
        audit("ORDER_REFUSED_NO_TRADING_SYMBOL",
              scrip=opt_key, idx=idx_name, expiry=str(expiry))
        append_blocked(
            kind="ENTRY", scrip=opt_key, side="B", qty=lot_size, price=opt_ltp,
            result="NO_TRADING_SYMBOL",
            message=f"Could not build trading symbol for expiry={expiry}",
            underlying=idx_name, strike=atm, option_type=option_type,
            trigger_spot=spot,
            trigger_level="BUY" if option_type == "CE" else "SELL",
        )
        return False

    scrip = {
        "symbol": opt_key,
        "trading_symbol": trading_symbol,
        "exchange": cfg["exchange_segment"],
    }
    res = place_order_safe(
        client=client, scrip=scrip, side="B",
        qty=lot_size, price=opt_ltp,
        order_type="L", product="MIS", validity="DAY",
        trigger="0", tag=f"auto:{opt_key}",
        live_mode=LIVE_MODE,
        available_cash=_fetch_available_cash(client),
        lot_size=lot_size, source="auto_options",
    )

    if res["result"] not in (RESULT_OK, RESULT_PAPER):
        # HALTED / MARGIN / KOTAK_ERROR — wrapper already audited it.
        # Record a Blockers row so Ganesh sees the refused attempt with full
        # context (scrip, side, qty, price, reason) without parsing audit.log.
        append_blocked(
            kind="ENTRY", scrip=opt_key, side="B", qty=lot_size,
            price=opt_ltp, result=res["result"], message=res["message"],
            underlying=idx_name, strike=atm, option_type=option_type,
            trading_symbol=trading_symbol, trigger_spot=spot,
            trigger_level="BUY" if option_type == "CE" else "SELL",
        )
        return False

    # Build and persist the trade row.
    mode = "LIVE" if res["result"] == RESULT_OK else "PAPER"
    kotak_order_id = res.get("order_id") if mode == "LIVE" else None
    now = now_ist()
    trades = read_trade_ledger()
    row = {
        "id": next_trade_id(trades),
        "date": now.strftime("%Y-%m-%d"),
        "scrip": opt_key,
        "option_key": opt_key,
        "asset_type": "option",
        "underlying": idx_name,
        "strike": atm,
        "option_type": option_type,
        "expiry": str(expiry) if expiry else None,
        "trading_symbol": trading_symbol,
        "order_type": "BUY",
        "entry_time": now.strftime("%H:%M:%S"),
        "entry_ts": now.timestamp(),
        "entry_price": round(opt_ltp, 2),
        "qty": lot_size,
        "trigger_spot": round(spot, 2),
        "trigger_level": "BUY" if option_type == "CE" else "SELL",
        "max_min_target_price": round(opt_ltp, 2),
        "target_level_reached": None,
        "exit_time": None, "exit_ts": None,
        "exit_price": None, "exit_reason": None,
        "pnl_points": None, "pnl_pct": None, "duration_seconds": None,
        "status": "OPEN",
        "auto": True,
        "mode": mode,
        "kotak_entry_order_id": kotak_order_id,
        "kotak_exit_order_id": None,
    }
    trades.insert(0, row)
    write_trade_ledger(trades)
    audit("AUTO_ENTRY_PLACED", scrip=opt_key, mode=mode,
          kotak_order_id=kotak_order_id, qty=lot_size, price=opt_ltp)
    return True


def _execute_exit(open_trade, opt_ltp, reason, option_index_meta, client):
    """Place a real SELL order to close `open_trade` and update the ledger.

    LIVE only: verify with Kotak that the position is still open before
    sending the SELL (don't open a fresh short on a closed position).
    PAPER skips verification — there's nothing real to verify.
    """
    opt_key = open_trade.get("option_key")
    idx_name = open_trade.get("underlying")
    cfg = INDEX_OPTIONS_CONFIG.get(idx_name) or {}
    lot_size = cfg.get("lot_size") or open_trade.get("qty") or 1
    trading_symbol = (open_trade.get("trading_symbol")
                      or _option_trading_symbol(
                          idx_name, open_trade.get("strike"),
                          open_trade.get("option_type"),
                          (option_index_meta.get(idx_name) or {}).get("expiry"),
                      ))
    if not trading_symbol:
        audit("EXIT_REFUSED_NO_TRADING_SYMBOL", scrip=opt_key)
        append_blocked(
            kind="EXIT", scrip=opt_key, side="S", qty=int(lot_size),
            price=opt_ltp, result="NO_TRADING_SYMBOL",
            message="Could not build trading symbol for exit",
            underlying=idx_name, strike=open_trade.get("strike"),
            option_type=open_trade.get("option_type"),
        )
        return False

    # LIVE only: verify Kotak shows the position open before exit.
    if LIVE_MODE:
        if client is None:
            audit("EXIT_REFUSED_NO_CLIENT", scrip=opt_key, reason=reason)
            append_blocked(
                kind="EXIT", scrip=opt_key, side="S", qty=int(lot_size),
                price=opt_ltp, result="NO_CLIENT",
                message="Kotak client not initialised — cannot verify position",
                underlying=idx_name, strike=open_trade.get("strike"),
                option_type=open_trade.get("option_type"),
                trading_symbol=trading_symbol,
            )
            return False
        ok, info = verify_open_position(client, trading_symbol, side="BUY")
        if not ok:
            audit("EXIT_REFUSED_NO_KOTAK_POSITION",
                  scrip=opt_key, trading_symbol=trading_symbol,
                  kotak_info=info)
            append_blocked(
                kind="EXIT", scrip=opt_key, side="S", qty=int(lot_size),
                price=opt_ltp, result="NO_KOTAK_POSITION",
                message=("Kotak shows no matching open position — refusing "
                         "to send SELL (would open a fresh short)."),
                underlying=idx_name, strike=open_trade.get("strike"),
                option_type=open_trade.get("option_type"),
                trading_symbol=trading_symbol,
            )
            return False

    qty = int(open_trade.get("qty") or lot_size)
    scrip = {
        "symbol": opt_key,
        "trading_symbol": trading_symbol,
        "exchange": cfg.get("exchange_segment", "nse_fo"),
    }
    res = place_order_safe(
        client=client, scrip=scrip, side="S",
        qty=qty, price=opt_ltp,
        order_type="L", product="MIS", validity="DAY",
        trigger="0", tag=f"auto:exit:{opt_key}",
        live_mode=LIVE_MODE,
        available_cash=_fetch_available_cash(client),
        lot_size=lot_size, source="auto_options",
    )

    if res["result"] not in (RESULT_OK, RESULT_PAPER):
        append_blocked(
            kind="EXIT", scrip=opt_key, side="S", qty=qty,
            price=opt_ltp, result=res["result"], message=res["message"],
            underlying=idx_name, strike=open_trade.get("strike"),
            option_type=open_trade.get("option_type"),
            trading_symbol=trading_symbol,
        )
        return False

    mode = "LIVE" if res["result"] == RESULT_OK else "PAPER"
    kotak_order_id = res.get("order_id") if mode == "LIVE" else None
    now = now_ist()
    exit_price = float(opt_ltp)
    entry_price = float(open_trade.get("entry_price") or exit_price)
    pnl = (exit_price - entry_price) if open_trade.get("order_type") == "BUY" \
          else (entry_price - exit_price)
    # Update the ledger row in place. Re-read to avoid clobbering concurrent
    # writes from other paths (manual ticket, etc.).
    trades = read_trade_ledger()
    for t in trades:
        if t.get("id") == open_trade.get("id") and t.get("status") == "OPEN":
            t["exit_time"] = now.strftime("%H:%M:%S")
            t["exit_ts"] = now.timestamp()
            t["exit_price"] = round(exit_price, 2)
            t["exit_reason"] = reason
            t["pnl_points"] = round(pnl, 2)
            t["pnl_pct"] = (round((pnl / entry_price) * 100, 2)
                            if entry_price else 0.0)
            t["duration_seconds"] = round(
                now.timestamp() - float(t.get("entry_ts") or now.timestamp()), 1)
            t["status"] = "CLOSED"
            t["mode"] = mode
            t["kotak_exit_order_id"] = kotak_order_id
            break
    write_trade_ledger(trades)
    audit("AUTO_EXIT_PLACED", scrip=opt_key, mode=mode, reason=reason,
          kotak_order_id=kotak_order_id, qty=qty, price=opt_ltp)
    return True

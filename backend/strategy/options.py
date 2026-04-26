"""Paper auto-trading strategy for index options.

Plain English:
  - Watch the index spot (NIFTY 50, BANKNIFTY, SENSEX).
  - When spot crosses BUY level upward -> paper buy 1 lot ATM CE.
  - When spot crosses SELL level downward -> paper buy 1 lot ATM PE.
  - For each open option trade, watch THREE exits (first hit wins):
      1. Stop loss   (3 variants — see EXIT block below; one is active)
      2. Profit T1   (CE: spot >= T1; PE: spot <= S1)
      3. Time exit   (force-close at 15:15 IST)

Trades go to paper_trades.json only. Never calls Kotak place_order.

Note: the caller passes `gann_quotes` (the stock-side fetch_quotes result) so
this module doesn't need to import fetch_quotes — that would create an
app.py <-> strategy circular import.
"""
import threading

from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
from backend.storage.trades import (
    read_paper_trades, write_paper_trades, next_paper_id,
)
from backend.strategy.common import (
    _auto_at_or_after_squareoff, _auto_close, _auto_in_hours,
)
from backend.utils import now_ist


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


def option_auto_strategy_tick(option_data, option_index_meta, gann_quotes):
    """One tick of the paper option auto-strategy.

    option_data: {key: {index, strike, option_type, ltp, ...}}
    option_index_meta: {index_name: {spot, atm, expiry, ...}}
    gann_quotes: stock-side quotes (need .levels for the index spot symbols)
    """
    if not AUTO_OPTION_STRATEGY_ENABLED or not option_index_meta:
        return
    now = now_ist()

    with _option_auto_state["lock"]:
        trades = read_paper_trades()
        modified = False

        # 1. Square off option positions at/after 15:15
        if _auto_at_or_after_squareoff(now):
            for t in trades:
                if (t.get("status") != "OPEN"
                        or t.get("asset_type") != "option"):
                    continue
                q = option_data.get(t.get("option_key"))
                if not q or q.get("ltp") is None:
                    continue
                _auto_close(t, float(q["ltp"]), now, "AUTO_SQUARE_OFF")
                modified = True
            if modified:
                write_paper_trades(trades)
            return

        if not _auto_in_hours(now):
            return

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

            # ---- EXIT check (see _check_exit_reason for the 3 conditions) ----
            open_t = open_by_underlying.get(idx_name)
            if open_t:
                opt_q = option_data.get(open_t.get("option_key"))
                opt_ltp = (opt_q or {}).get("ltp")
                reason = _check_exit_reason(
                    open_t, opt_ltp, spot,
                    buy_lvl, sell_lvl, t1_lvl, s1_lvl,
                )
                if reason and opt_ltp is not None:
                    _auto_close(open_t, float(opt_ltp), now, reason)
                    modified = True
                    open_by_underlying.pop(idx_name, None)
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
                        t = {
                            "id": next_paper_id(trades),
                            "date": now.strftime("%Y-%m-%d"),
                            "scrip": opt_key,
                            "option_key": opt_key,
                            "asset_type": "option",
                            "underlying": idx_name,
                            "strike": atm,
                            "option_type": option_type,
                            "expiry": m.get("expiry"),
                            "order_type": "BUY",
                            "entry_time": now.strftime("%H:%M:%S"),
                            "entry_ts": now.timestamp(),
                            "entry_price": round(float(opt_ltp), 2),
                            "qty": 1,
                            "trigger_spot": round(float(spot), 2),
                            "trigger_level": ("BUY" if option_type == "CE"
                                              else "SELL"),
                            "max_min_target_price": round(float(opt_ltp), 2),
                            "target_level_reached": None,
                            "exit_time": None, "exit_ts": None,
                            "exit_price": None,
                            "exit_reason": None, "pnl_points": None,
                            "pnl_pct": None,
                            "duration_seconds": None,
                            "status": "OPEN",
                            "auto": True,
                        }
                        trades.insert(0, t)
                        modified = True
                        counts[idx_name] = counts.get(idx_name, 0) + 1

            _option_auto_state["last_spot"][idx_name] = spot

        if modified:
            write_paper_trades(trades)

"""Paper auto-trading strategy for index options (Gann spot-crossing → ATM CE/PE).

Triggered each /api/option-prices fetch. Same Gann-level crossing concept as
the stock strategy, applied to index options:
  - Spot crosses BUY level UP    -> paper BUY 1 lot ATM CE
  - Spot crosses SELL level DOWN -> paper BUY 1 lot ATM PE
Exits:
  - CE: spot >= T1 (target) OR spot < SELL level (SL) OR 15:15 square-off
  - PE: spot <= S1 (target) OR spot > BUY level  (SL) OR 15:15 square-off

Entry/exit prices are option LTPs at that moment (paper, not Kotak orders).

Note: the caller passes `gann_quotes` (the stock-side fetch_quotes result) so
this module doesn't need to import fetch_quotes — that would create an
app.py ↔ strategy circular import.
"""
import threading

from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
from backend.storage.trades import (
    read_paper_trades, write_paper_trades, next_paper_id,
)
from backend.strategy.stocks import (
    AUTO_MAX_TRADES_PER_SCRIP, _auto_at_or_after_squareoff,
    _auto_close, _auto_in_hours,
)
from backend.utils import now_ist


AUTO_OPTION_STRATEGY_ENABLED = True

_option_auto_state = {
    "last_spot": {},  # index_name -> last seen spot
    "lock": threading.Lock(),
}


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

            # Exit open position (based on spot level hits)
            open_t = open_by_underlying.get(idx_name)
            if open_t:
                opt_q = option_data.get(open_t.get("option_key"))
                opt_ltp = (opt_q or {}).get("ltp")
                reason = None
                if open_t.get("option_type") == "CE":
                    if t1_lvl is not None and spot >= t1_lvl:
                        reason = "TARGET_T1"
                    elif sell_lvl is not None and spot < sell_lvl:
                        reason = "SL_SELL_LVL"
                else:  # PE
                    if s1_lvl is not None and spot <= s1_lvl:
                        reason = "TARGET_S1"
                    elif buy_lvl is not None and spot > buy_lvl:
                        reason = "SL_BUY_LVL"
                if reason and opt_ltp is not None:
                    _auto_close(open_t, float(opt_ltp), now, reason)
                    modified = True
                    open_by_underlying.pop(idx_name, None)
                    _option_auto_state["last_spot"][idx_name] = spot
                    continue

            # Entry check — spot crossing
            if (idx_name not in open_by_underlying
                    and counts.get(idx_name, 0) < AUTO_MAX_TRADES_PER_SCRIP
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

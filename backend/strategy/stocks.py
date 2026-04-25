"""Paper auto-trading strategy for stocks (Gann Square of 9 crossings).

Rules (v1):
  Hours:        09:15 - 15:15 IST (square off all OPEN at 15:15)
  Entry BUY:    LTP crosses above BUY level (from <=BUY to >BUY)
  Entry SELL:   LTP crosses below SELL level (from >=SELL to <SELL)
  Target BUY:   exit when LTP >= T1
  Target SELL:  exit when LTP <= S1
  SL BUY:       exit when LTP < SELL level
  SL SELL:      exit when LTP > BUY level
  Qty:          AUTO_QTY per trade
  Max/scrip/day: AUTO_MAX_TRADES_PER_SCRIP
  Side effect:  paper_trades.json only — never places a real order.
"""
import threading

from backend.kotak.instruments import SCRIPS
from backend.storage.trades import (
    read_paper_trades, write_paper_trades, next_paper_id,
)
from backend.strategy.gann import compute_target_level_reached
from backend.utils import now_ist


AUTO_STRATEGY_ENABLED = True
AUTO_HOURS_START = (9, 15)
AUTO_HOURS_END   = (15, 15)
AUTO_MAX_TRADES_PER_SCRIP = 2
AUTO_QTY = 1

_auto_state = {
    "last_ltp": {},  # symbol -> last LTP seen (to detect crossings)
    "lock": threading.Lock(),
}


def _auto_in_hours(now):
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return AUTO_HOURS_START <= hm < AUTO_HOURS_END


def _auto_at_or_after_squareoff(now):
    if now.weekday() >= 5:
        return False  # weekends: idle, don't force square-off
    return (now.hour, now.minute) >= AUTO_HOURS_END


def _auto_close(trade, ltp, now, reason):
    trade["exit_time"] = now.strftime("%H:%M:%S")
    trade["exit_ts"]   = now.timestamp()
    trade["exit_price"] = round(ltp, 2)
    trade["exit_reason"] = reason
    if trade["order_type"] == "BUY":
        pnl = ltp - trade["entry_price"]
    else:
        pnl = trade["entry_price"] - ltp
    trade["pnl_points"] = round(pnl, 2)
    trade["pnl_pct"]    = (round((pnl / trade["entry_price"]) * 100, 2)
                            if trade["entry_price"] else 0.0)
    trade["duration_seconds"] = round(
        now.timestamp() - trade.get("entry_ts", now.timestamp()), 1)
    trade["status"] = "CLOSED"


def _auto_open(sym, side, qty, ltp, now, trades):
    t = {
        "id": next_paper_id(trades),
        "date": now.strftime("%Y-%m-%d"),
        "scrip": sym,
        "order_type": "BUY" if side == "B" else "SELL",
        "entry_time": now.strftime("%H:%M:%S"),
        "entry_ts": now.timestamp(),
        "entry_price": round(ltp, 2),
        "qty": qty,
        "max_min_target_price": round(ltp, 2),
        "target_level_reached": None,
        "exit_time": None, "exit_ts": None, "exit_price": None,
        "exit_reason": None, "pnl_points": None, "pnl_pct": None,
        "duration_seconds": None,
        "status": "OPEN",
        "auto": True,
    }
    trades.insert(0, t)


def _auto_check_entry(q, prev_ltp, cur_ltp):
    """Return 'B' or 'S' on a level crossing, else None."""
    levels = q.get("levels") or {}
    buy_px  = (levels.get("buy")  or {}).get("BUY")
    sell_px = (levels.get("sell") or {}).get("SELL")
    if buy_px is not None and prev_ltp <= buy_px < cur_ltp:
        return "B"
    if sell_px is not None and prev_ltp >= sell_px > cur_ltp:
        return "S"
    return None


def _auto_check_exit(trade, q, ltp):
    """Return exit reason string if exit conditions met, else None."""
    levels = q.get("levels") or {}
    buy    = levels.get("buy")  or {}
    sell   = levels.get("sell") or {}
    if trade["order_type"] == "BUY":
        t1 = buy.get("T1")
        sl = sell.get("SELL")
        if t1 is not None and ltp >= t1:
            return "TARGET_T1"
        if sl is not None and ltp < sl:
            return "SL_SELL_LVL"
    else:
        s1 = sell.get("S1")
        sl = buy.get("BUY")
        if s1 is not None and ltp <= s1:
            return "TARGET_S1"
        if sl is not None and ltp > sl:
            return "SL_BUY_LVL"
    return None


def auto_strategy_tick(quotes):
    """One tick of the paper auto-strategy. Called after each fetch_quotes."""
    if not AUTO_STRATEGY_ENABLED or not quotes:
        return
    now = now_ist()
    with _auto_state["lock"]:
        trades = read_paper_trades()
        modified = False

        # 1. Square off at/after 15:15
        if _auto_at_or_after_squareoff(now):
            for t in trades:
                if t.get("status") != "OPEN":
                    continue
                q = quotes.get(t["scrip"])
                if not q or q.get("ltp") is None:
                    continue
                _auto_close(t, float(q["ltp"]), now, "AUTO_SQUARE_OFF")
                modified = True
            if modified:
                write_paper_trades(trades)
            return

        # 2. Only run during market hours
        if not _auto_in_hours(now):
            return

        today = now.strftime("%Y-%m-%d")
        counts = {}
        for t in trades:
            if t.get("date") == today:
                counts[t["scrip"]] = counts.get(t["scrip"], 0) + 1
        open_by_sym = {t["scrip"]: t for t in trades if t.get("status") == "OPEN"}

        for scrip in SCRIPS:
            if not scrip.get("tradeable"):
                continue
            sym = scrip["symbol"]
            q = quotes.get(sym)
            if not q or q.get("ltp") is None:
                continue
            ltp = float(q["ltp"])
            prev = _auto_state["last_ltp"].get(sym)

            # Exit check first
            open_t = open_by_sym.get(sym)
            if open_t:
                reason = _auto_check_exit(open_t, q, ltp)
                if reason:
                    _auto_close(open_t, ltp, now, reason)
                    modified = True
                    open_by_sym.pop(sym, None)
                    _auto_state["last_ltp"][sym] = ltp
                    continue

            # Entry check
            if (sym not in open_by_sym
                    and counts.get(sym, 0) < AUTO_MAX_TRADES_PER_SCRIP):
                if prev is not None:
                    side = _auto_check_entry(q, prev, ltp)
                    if side:
                        _auto_open(sym, side, AUTO_QTY, ltp, now, trades)
                        modified = True
                        counts[sym] = counts.get(sym, 0) + 1

            _auto_state["last_ltp"][sym] = ltp

        if modified:
            write_paper_trades(trades)


def update_open_trades_mfe(quotes_by_symbol):
    """For every OPEN trade, update max_min_target_price and
    target_level_reached based on the current LTP. Called each quote refresh."""
    trades = read_paper_trades()
    changed = False
    for t in trades:
        if t.get("status") != "OPEN":
            continue
        q = quotes_by_symbol.get(t["scrip"])
        if not q:
            continue
        ltp = q.get("ltp")
        if ltp is None:
            continue
        prev_mfe = t.get("max_min_target_price")
        if t["order_type"] == "BUY":
            new_mfe = ltp if prev_mfe is None else max(prev_mfe, ltp)
        else:
            new_mfe = ltp if prev_mfe is None else min(prev_mfe, ltp)
        if new_mfe != prev_mfe:
            t["max_min_target_price"] = round(new_mfe, 2)
            changed = True
        side = "B" if t["order_type"] == "BUY" else "S"
        reached = compute_target_level_reached(
            side, t["entry_price"], new_mfe, q.get("levels"))
        if reached and reached != t.get("target_level_reached"):
            t["target_level_reached"] = reached
            changed = True
    if changed:
        write_paper_trades(trades)

"""Shared helpers used by the trading strategy.

These four utilities are not specific to stocks or options — they're the
plumbing that any strategy needs:

  AUTO_HOURS_START / AUTO_HOURS_END   trading window (09:15 - 15:15 IST)
  _auto_in_hours(now)                 True when 'now' is inside the window on a weekday
  _auto_at_or_after_squareoff(now)    True at/after 15:15 IST on a weekday
  _auto_close(trade, ltp, now, reason)
                                      mark a trade CLOSED, fill exit fields, compute P&L
  update_open_trades_mfe(quotes)      track max favourable price + farthest Gann level
                                      reached on every OPEN trade in the ledger
"""
from backend import config_loader
from backend.storage.trades import read_trade_ledger, write_trade_ledger
from backend.strategy.gann import compute_target_level_reached


# ---------- trading window ----------
# Defaults — overridden live by config.yaml via config_loader.trading_window().
# Kept here so anything still importing the constants keeps working.
AUTO_HOURS_START = (9, 15)   # market opens
AUTO_HOURS_END   = (15, 15)  # auto square-off cut-off


def _auto_in_hours(now):
    """True if 'now' is a weekday inside [market_start, square_off) IST.
    Window read fresh from config each call — supports hot-reload."""
    if now.weekday() >= 5:  # 5 = Sat, 6 = Sun
        return False
    start, end = config_loader.trading_window()
    hm = (now.hour, now.minute)
    return start <= hm < end


def _auto_at_or_after_squareoff(now):
    """True if 'now' is a weekday at/after configured square-off time IST.
    Weekend = idle (no force-exit)."""
    if now.weekday() >= 5:
        return False
    _, end = config_loader.trading_window()
    return (now.hour, now.minute) >= end


# ---------- close a paper trade ----------
def _auto_close(trade, ltp, now, reason):
    """Stamp exit fields and compute P&L on an OPEN trade. Mutates the dict."""
    trade["exit_time"]   = now.strftime("%H:%M:%S")
    trade["exit_ts"]     = now.timestamp()
    trade["exit_price"]  = round(ltp, 2)
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


# ---------- track best price reached so far ----------
def update_open_trades_mfe(quotes_by_symbol):
    """For every OPEN trade, update max_min_target_price and target_level_reached
    based on the current LTP. Called once per quote refresh."""
    trades = read_trade_ledger()
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
        write_trade_ledger(trades)

"""Shared helpers used by the trading strategy.

These four utilities are not specific to stocks or options — they're the
plumbing that any strategy needs:

  AUTO_HOURS_START / AUTO_HOURS_END   trading window (09:15 - 15:15 IST)
  _auto_in_hours(now)                 True when 'now' is inside the window on a weekday
  _auto_at_or_after_squareoff(now)    True at/after 15:15 IST on a weekday
  _auto_close(trade, ltp, now, reason)
                                      mark a trade CLOSED, fill exit fields, compute P&L
  update_open_trades_mfe(quotes)      track max favourable price + farthest Gann level
                                      reached on every OPEN trade in the ledger; for
                                      stoploss variant D also maintains trail_sl_price
"""
from backend import config_loader
from backend.storage.trades import read_trade_ledger, write_trade_ledger
from backend.strategy.gann import compute_target_level_reached
from backend.utils import now_ist


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


# ---------- variant D trail SL helpers ----------
def _trade_is_bullish(t):
    """CE option = bullish; PE option = bearish; future BUY = bullish; future SELL = bearish."""
    if t.get("asset_type") == "option":
        return t.get("option_type") == "CE"
    return t.get("order_type") == "BUY"


def _resolve_spot_quote(t, quotes_by_symbol):
    """Look up the SPOT quote for a trade. Options trades store
    `t["scrip"]` as the option key, so we resolve via
    INDEX_OPTIONS_CONFIG[underlying]["spot_symbol_key"]. Futures
    trades may also be keyed by the futures-instrument symbol, so we
    use the same indirection for consistency."""
    from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
    underlying = t.get("underlying")
    if not underlying:
        return None
    cfg = INDEX_OPTIONS_CONFIG.get(underlying) or {}
    spot_key = cfg.get("spot_symbol_key")
    if not spot_key:
        return None
    return quotes_by_symbol.get(spot_key)


def _compute_trail_for_trade(t, spot, spot_levels):
    """Returns (new_trail_price, new_high_rung_name) for variant D, or
    (None, None) if the ladder can't be resolved."""
    from backend.strategy.gann import BUY_LEVEL_ORDER, SELL_LEVEL_ORDER
    is_bullish = _trade_is_bullish(t)
    entry_price = t.get("entry_price")
    if is_bullish:
        ladder = [(n, (spot_levels.get("buy") or {}).get(n))
                  for n in BUY_LEVEL_ORDER]
        ladder = [(n, p) for n, p in ladder if p is not None]
        # current_idx = highest rung with spot >= price(rung)
        current_idx = -1
        for i, (_n, p) in enumerate(ladder):
            if spot >= p:
                current_idx = i
        if current_idx < 0:
            # Below first rung — initial breakeven (entry).
            return (entry_price, None)
        if current_idx == 0:
            return (entry_price, ladder[0][0])
        return (ladder[current_idx - 1][1], ladder[current_idx][0])
    else:
        ladder = [(n, (spot_levels.get("sell") or {}).get(n))
                  for n in SELL_LEVEL_ORDER]
        ladder = [(n, p) for n, p in ladder if p is not None]
        current_idx = -1
        for i, (_n, p) in enumerate(ladder):
            if spot <= p:
                current_idx = i
        if current_idx < 0:
            return (entry_price, None)
        if current_idx == 0:
            return (entry_price, ladder[0][0])
        return (ladder[current_idx - 1][1], ladder[current_idx][0])


# ---------- track best price reached so far ----------
def update_open_trades_mfe(quotes_by_symbol):
    """For every OPEN trade, update max_min_target_price /
    target_level_reached / (variant D) trail_sl_price."""
    trades = read_trade_ledger()
    changed = False
    cfg = config_loader.get()
    trail_active = cfg["stoploss"]["active"] == "D"
    in_hours = _auto_in_hours(now_ist())

    for t in trades:
        if t.get("status") != "OPEN":
            continue

        # ---- existing MFE block (unchanged for trades where q exists) ----
        q = quotes_by_symbol.get(t["scrip"])
        if q:
            ltp = q.get("ltp")
            if ltp is not None:
                prev_mfe = t.get("max_min_target_price")
                if t["order_type"] == "BUY":
                    new_mfe = ltp if prev_mfe is None else max(prev_mfe, ltp)
                else:
                    new_mfe = ltp if prev_mfe is None else min(prev_mfe, ltp)
                if new_mfe != prev_mfe:
                    t["max_min_target_price"] = round(new_mfe, 2)
                    changed = True
                side_bs = "B" if t["order_type"] == "BUY" else "S"
                reached = compute_target_level_reached(
                    side_bs, t["entry_price"], new_mfe, q.get("levels"))
                if reached and reached != t.get("target_level_reached"):
                    t["target_level_reached"] = reached
                    changed = True

        # ---- Variant D: trail SL — gated, walks SPOT ladder ----
        # Only in-hours; trail_sl_price is load-bearing for SL correctness
        # and must not ratchet on stale weekend prints.
        if not (trail_active and in_hours):
            continue
        try:
            spot_q = _resolve_spot_quote(t, quotes_by_symbol)
            if not spot_q:
                continue
            spot = spot_q.get("ltp")
            spot_levels = spot_q.get("levels") or {}
            if spot is None:
                continue
            new_trail, new_high = _compute_trail_for_trade(
                t, spot, spot_levels)
            if new_trail is None:
                continue
            prev = t.get("trail_sl_price")
            # Ratchet direction depends on bias:
            #   bullish bias (CE / future BUY) — trail rises monotonically
            #   bearish bias (PE / future SELL) — trail falls monotonically
            is_bullish = _trade_is_bullish(t)
            if prev is None \
                    or (is_bullish and new_trail > prev) \
                    or ((not is_bullish) and new_trail < prev):
                t["trail_sl_price"] = round(float(new_trail), 2)
                t["trail_high_rung"] = new_high
                changed = True
        except Exception as e:
            # Snapshot-thread error swallowing was flagged in the spec.
            # Log explicitly so a malformed quote doesn't silently
            # disarm the trail SL.
            print(f"[trail] update failed for trade {t.get('id')}: "
                  f"{type(e).__name__}: {e}")

    if changed:
        write_trade_ledger(trades)

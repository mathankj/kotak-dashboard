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
from backend.storage.paper_ledger import read_paper_ledger, write_paper_ledger
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
def _derive_exit_level(trade, reason):
    """Pick the Gann-level name to show alongside exit_spot.
    SL_TRAIL → highest rung the trail had reached.
    TARGET_X → strip prefix → e.g. 'T1', 'S2'.
    Any other reason (manual, AUTO_SQUARE_OFF, SL_FUT_FIXED, etc.) → no level."""
    if reason == "SL_TRAIL":
        return trade.get("trail_high_rung")
    if reason and reason.startswith("TARGET_"):
        return reason.split("_", 1)[1]
    return None


def _auto_close(trade, ltp, now, reason, spot=None):
    """Stamp exit fields and compute P&L on an OPEN trade. Mutates the dict.

    `spot` (optional) records the underlying spot at exit so the UI
    can show "exited at spot 76998 (S2)" instead of just the option
    premium. Level name comes from `_derive_exit_level`."""
    trade["exit_time"]   = now.strftime("%H:%M:%S")
    trade["exit_ts"]     = now.timestamp()
    trade["exit_price"]  = round(ltp, 2)
    trade["exit_reason"] = reason
    trade["exit_spot"]   = round(float(spot), 2) if spot is not None else None
    trade["exit_level"]  = _derive_exit_level(trade, reason)
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


def _breakeven_for_trail(t):
    """Spot-dimension breakeven price for variant D's initial trail.

    For FUTURES: entry_price is the futures price (≈ spot) — comparing
    against current spot is meaningful, so we use it.

    For OPTIONS: entry_price is the option premium (e.g. 404.5) which
    is in a totally different dimension than spot (e.g. 76984). Using
    it as a "trail level" makes `spot >= trail` trivially true on PE
    and trivially false on CE — neither what variant D intends. Use
    `trigger_spot` (spot at entry) instead so the trail compares
    apples-to-apples.
    """
    if t.get("asset_type") == "option":
        return t.get("trigger_spot")
    return t.get("entry_price")


def _compute_trail_for_trade(t, spot, spot_levels):
    """Returns (new_trail_price, new_high_rung_name) for variant D, or
    (None, None) if the ladder can't be resolved."""
    from backend.strategy.gann import BUY_LEVEL_ORDER, SELL_LEVEL_ORDER
    is_bullish = _trade_is_bullish(t)
    breakeven = _breakeven_for_trail(t)
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
            return (breakeven, None)
        if current_idx == 0:
            return (breakeven, ladder[0][0])
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
            return (breakeven, None)
        if current_idx == 0:
            return (breakeven, ladder[0][0])
        return (ladder[current_idx - 1][1], ladder[current_idx][0])


# ---------- track best price reached so far ----------
def _apply_mfe_and_trail(trades, quotes_by_symbol, in_hours):
    """Mutate `trades` in place: bump max_min_target_price /
    target_level_reached and (variant D) trail_sl_price for every OPEN
    row. Returns True if anything changed (so the caller can persist).

    Phase 3: engine-aware. Each open row carries an `engine` field
    ("current" or "reverse"; legacy rows default to "current"). The
    target_level_reached badge and the variant-D trail walk the row's
    OWN ladder — open-anchored `levels` for current, low/high-anchored
    `rev_levels` for reverse — and trail-active is decided per engine
    (since current and reverse can pick different SL variants from
    their respective `stoploss.active` configs).

    Same logic for live and paper books — only the storage differs."""
    # Pre-resolve trail-active once per engine so we don't refetch the
    # config for every open row.
    trail_active_by_engine = {
        "current": (config_loader.engine_block("current").get("stoploss") or {})
                       .get("active") == "D",
        "reverse": (config_loader.engine_block("reverse").get("stoploss") or {})
                       .get("active") == "D",
    }
    changed = False
    for t in trades:
        if t.get("status") != "OPEN":
            continue
        engine = t.get("engine") or "current"
        # Reverse-engine rows track target/trail along the rev ladder.
        levels_key = "rev_levels" if engine == "reverse" else "levels"

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
                    side_bs, t["entry_price"], new_mfe, q.get(levels_key))
                if reached and reached != t.get("target_level_reached"):
                    t["target_level_reached"] = reached
                    changed = True

        # ---- Variant D: trail SL — gated, walks SPOT ladder ----
        # Only in-hours; trail_sl_price is load-bearing for SL correctness
        # and must not ratchet on stale weekend prints.
        if not (trail_active_by_engine.get(engine, False) and in_hours):
            continue
        try:
            spot_q = _resolve_spot_quote(t, quotes_by_symbol)
            if not spot_q:
                continue
            spot = spot_q.get("ltp")
            spot_levels = spot_q.get(levels_key) or {}
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
    return changed


def update_open_trades_mfe(quotes_by_symbol):
    """For every OPEN trade in BOTH the live and paper ledgers, update
    max_min_target_price / target_level_reached / (variant D)
    trail_sl_price. Trail SL must apply to paper trades too — that's the
    whole point of paper-mode validation.

    Phase 3: trail-active and the levels ladder are decided per row
    inside `_apply_mfe_and_trail` based on each row's `engine` field,
    so this entry point no longer pre-computes a global trail-active."""
    in_hours = _auto_in_hours(now_ist())

    live = read_trade_ledger()
    if _apply_mfe_and_trail(live, quotes_by_symbol, in_hours):
        write_trade_ledger(live)

    paper = read_paper_ledger()
    if _apply_mfe_and_trail(paper, quotes_by_symbol, in_hours):
        write_paper_ledger(paper)

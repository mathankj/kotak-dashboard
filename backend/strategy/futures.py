"""Auto-trading strategy for index FUTURES — Phase 4 (LIVE).

Mirrors the options strategy (backend/strategy/options.py) but trades the
nearest-expiry FUT contract directly. Long + short:
  - bullish Gann signal -> BUY future  (long)
  - bearish Gann signal -> SELL future (short)

Master switch in config.yaml -> apply_to (options | futures | both).
When apply_to is "options", future_auto_strategy_tick short-circuits.
All other knobs (entry paths, SL variant, target levels, lots, per-day cap)
are SHARED with options.py — one config drives both strategies.

LIMIT-PRICE ROUNDING (Ganesh's spec):
  Round futures LTP to the per-index step (config.yaml -> futures_round_step).
  BUY rounds DOWN (try to fill cheaper). SELL rounds UP (try to fill higher).
  Same rounding rule applies to exits — closing-side derived from
  open trade direction:
    - closing a LONG  = SELL -> round UP
    - closing a SHORT = BUY  -> round DOWN

EXITS (4 variants, exactly one active per `stoploss.active`):
  A) Fixed ₹X drop in futures LTP  (long: ltp <= entry-X; short: ltp >= entry+X)
  B) Percentage drop in futures LTP (long: ltp <= entry*(1-p%); short: ltp >= entry*(1+p%))
  C) Spot reverses through opposite Gann level
     (long exits when spot < variant_c_sell_level pick;
      short exits when spot > variant_c_buy_level pick)
  D) Trailing along the Gann ladder — SL trails one rung behind the
     spot's current rung. Initial SL = entry price. Triggers on spot
     crossing trail_sl_price; close fills at fut_ltp.

PROFIT TARGET — spot reaches configured Gann level:
  long  -> spot >= target.ce_level (T1/T2/T3/BUY_WA on CE side)
  short -> spot <= target.pe_level (S1/S2/S3/SELL_WA on PE side)

Reuses the same place_order_safe wrapper, kill-switch gate, ledger format,
and position-verification step as options. Trade rows carry
asset_type="future" and order_type="BUY" / "SELL" so the existing PnL
math in _auto_close and the ledger UI Just Work.
"""
import math
import threading

from backend import config_loader
from backend.kotak.client import safe_call
from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
from backend.safety.audit import audit
from backend.safety.orders import (
    place_order_safe,
    RESULT_OK, RESULT_PAPER,
)
from backend.safety.positions import verify_open_position
from backend.storage.blocked import append_blocked
from backend.storage.trades import (
    read_trade_ledger, write_trade_ledger, next_trade_id,
)
from backend.strategy.common import (
    _auto_at_or_after_squareoff, _auto_in_hours,
)
# Reuse the SAME LIVE_MODE master switch as options. One source of truth —
# flipping options live also flips futures live (and vice versa). That's
# intentional: avoids the foot-gun of "I thought I was paper-only on futures".
from backend.strategy.options import LIVE_MODE
from backend.utils import now_ist


AUTO_FUTURE_STRATEGY_ENABLED = True


_future_auto_state = {
    "last_spot": {},        # idx_name -> last seen spot
    "open_evaluated": {},   # idx_name -> "YYYY-MM-DD" once we've done the
                            # market-open evaluation for that day
    "lock": threading.Lock(),
}


# ---------- rounding ----------
def _round_for_buy(price, step):
    """BUY rounds DOWN to nearest `step` — try to fill cheaper."""
    if step <= 0:
        return float(price)
    return math.floor(float(price) / step) * step


def _round_for_sell(price, step):
    """SELL rounds UP to nearest `step` — try to fill higher."""
    if step <= 0:
        return float(price)
    return math.ceil(float(price) / step) * step


def _close_round(open_trade, ltp, step):
    """Round LTP for the closing order based on open trade direction."""
    if open_trade.get("order_type") == "BUY":
        # Long open -> closing SELL, round UP.
        return _round_for_sell(ltp, step)
    # Short open -> closing BUY, round DOWN.
    return _round_for_buy(ltp, step)


# ---------- exits ----------
def _check_futures_exit_reason(open_t, fut_ltp, spot,
                                buy_lvl, sell_lvl,
                                long_target_lvl, short_target_lvl):
    """Return exit-reason string or None. Handles LONG and SHORT.

    open_t["order_type"] == "BUY"  -> long position
    open_t["order_type"] == "SELL" -> short position
    """
    entry_price = open_t.get("entry_price")
    side = open_t.get("order_type")  # "BUY" (long) or "SELL" (short)

    cfg = config_loader.get()
    fsl = cfg["stoploss"]    # unified — same SL config as options
    active = fsl["active"]   # validated A|B|C|D

    is_long = (side == "BUY")

    # ---------- (1) STOP LOSS — exactly ONE variant runs ----------
    if active == "A":
        # Fixed ₹X drop in futures LTP.
        drop_rs = fsl["variant_a_drop_rs"]
        if fut_ltp is not None and entry_price is not None:
            if is_long and fut_ltp <= entry_price - drop_rs:
                return "SL_FUT_FIXED"
            if (not is_long) and fut_ltp >= entry_price + drop_rs:
                return "SL_FUT_FIXED"

    elif active == "B":
        # Percentage drop in futures LTP.
        drop_pct = fsl["variant_b_drop_pct"]
        if fut_ltp is not None and entry_price is not None:
            if is_long and fut_ltp <= entry_price * (1 - drop_pct / 100.0):
                return "SL_FUT_PCT"
            if (not is_long) and fut_ltp >= entry_price * (1 + drop_pct / 100.0):
                return "SL_FUT_PCT"

    elif active == "C":
        # Spot reverses through opposite Gann level.
        if is_long and sell_lvl is not None and spot < sell_lvl:
            return "SL_SELL_LVL"
        if (not is_long) and buy_lvl is not None and spot > buy_lvl:
            return "SL_BUY_LVL"

    elif active == "D":
        # Trailing along Gann ladder. trail_sl_price is set by
        # update_open_trades_mfe on each in-hours snapshot refresh
        # (~2s cadence). Until the first refresh after entry it may
        # be None — guard explicitly.
        trail = open_t.get("trail_sl_price")
        if trail is not None and spot is not None:
            if is_long and spot <= trail:
                return "SL_TRAIL"
            if (not is_long) and spot >= trail:
                return "SL_TRAIL"

    # ---------- (2) PROFIT TARGET ----------
    # Unified target config — futures long uses ce_level (BUY-side Gann),
    # futures short uses pe_level (SELL-side Gann). Same picks as options.
    ftgt = cfg["target"]
    if is_long:
        if long_target_lvl is not None and spot >= long_target_lvl:
            return f"TARGET_{ftgt['ce_level']}"
    else:
        if short_target_lvl is not None and spot <= short_target_lvl:
            return f"TARGET_{ftgt['pe_level']}"

    return None


def _can_open_more(idx_name, counts):
    # Unified per_day_cap — same caps as options strategy.
    cap = config_loader.per_day_cap(idx_name)
    if cap is None:
        return True
    return counts.get(idx_name, 0) < cap


def _fetch_available_cash(client):
    """Best-effort margin fetch. None means 'skip pre-check'."""
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


# ---------- entry signal helper (shared with paper book) ----------
def _compute_futures_entry_signal(idx_name, spot, prev_spot, levels, cfg,
                                  already_evaluated_open):
    """Pure decision: which side ("BUY"|"SELL"|None) and stamp_now flag.

    Mirrors backend.strategy.options._compute_entry_signal but produces
    futures-side semantics (BUY = long, SELL = short).

    stamp_now=True means: caller should stamp open_evaluated[idx]=today
    NOW. stamp_now=False means: caller defers (used when side IS set on
    the market-open path, since we want to wait until fut_ltp is
    available before burning the open-evaluation slot).
    """
    entry_cfg = cfg["entry"]
    mo_buy_lvl  = config_loader.resolve_buy_level (levels, entry_cfg["market_open_buy_level"])
    mo_sell_lvl = config_loader.resolve_sell_level(levels, entry_cfg["market_open_sell_level"])
    cr_buy_lvl  = config_loader.resolve_buy_level (levels, entry_cfg["crossing_buy_level"])
    cr_sell_lvl = config_loader.resolve_sell_level(levels, entry_cfg["crossing_sell_level"])

    side = None
    stamp_now = False
    if not already_evaluated_open:
        if entry_cfg["market_open_path"]:
            if mo_buy_lvl is not None and spot > mo_buy_lvl:
                side = "BUY"
            elif mo_sell_lvl is not None and spot < mo_sell_lvl:
                side = "SELL"
            stamp_now = (side is None)
        else:
            stamp_now = True
    elif prev_spot is not None and entry_cfg["crossing_path"]:
        if cr_buy_lvl is not None and prev_spot <= cr_buy_lvl < spot:
            side = "BUY"
        elif cr_sell_lvl is not None and prev_spot >= cr_sell_lvl > spot:
            side = "SELL"

    return side, stamp_now


# ---------- main tick ----------
def future_auto_strategy_tick(future_data, gann_quotes, client=None):
    """One tick of the futures auto-strategy.

    future_data:   {idx_name: {trading_symbol, token, exchange, expiry,
                               lot_size, ltp, open, low, high, close}}
                   — produced by quotes.fetch_future_quotes()
    gann_quotes:   stock-side quotes (need .levels for index spots)
    client:        Kotak NeoAPI client. Required when LIVE_MODE=True.
    """
    if not AUTO_FUTURE_STRATEGY_ENABLED or not future_data:
        return
    # apply_to gate — config switch decides whether futures strategy runs.
    # When apply_to is "options", this short-circuits the whole tick.
    if not config_loader.futures_enabled():
        return

    now = now_ist()

    with _future_auto_state["lock"]:
        trades = read_trade_ledger()

        # 1. SQUARE OFF at/after configured square_off — close everything OPEN
        if _auto_at_or_after_squareoff(now):
            for t in trades:
                if (t.get("status") != "OPEN"
                        or t.get("asset_type") != "future"):
                    continue
                idx_name = t.get("underlying")
                fut = future_data.get(idx_name)
                if not fut or fut.get("ltp") is None:
                    continue
                _execute_futures_exit(t, float(fut["ltp"]),
                                       "AUTO_SQUARE_OFF",
                                       future_data, client)
            return

        if not _auto_in_hours(now):
            return

        today = now.strftime("%Y-%m-%d")
        counts = {}
        for t in trades:
            if t.get("date") == today and t.get("asset_type") == "future":
                u = t.get("underlying", "")
                counts[u] = counts.get(u, 0) + 1
        open_by_underlying = {
            t["underlying"]: t for t in trades
            if t.get("status") == "OPEN" and t.get("asset_type") == "future"
        }

        cfg = config_loader.get()
        entry_cfg  = cfg["entry"]
        sl_cfg_idx = cfg["stoploss"]
        target_cfg = cfg["target"]

        for idx_name, fut in future_data.items():
            spot_q_key = INDEX_OPTIONS_CONFIG[idx_name]["spot_symbol_key"]
            gq = gann_quotes.get(spot_q_key) or {}
            spot = gq.get("ltp")
            if spot is None:
                continue
            spot = float(spot)
            levels = gq.get("levels") or {}
            # Per-row level resolution (same picks as options — unified config):
            # entry uses market_open_* / crossing_* dropdowns,
            # variant C exit uses variant_c_* dropdowns (long exits below
            # sell pick; short exits above buy pick).
            mo_buy_lvl   = config_loader.resolve_buy_level (levels, entry_cfg["market_open_buy_level"])
            mo_sell_lvl  = config_loader.resolve_sell_level(levels, entry_cfg["market_open_sell_level"])
            cr_buy_lvl   = config_loader.resolve_buy_level (levels, entry_cfg["crossing_buy_level"])
            cr_sell_lvl  = config_loader.resolve_sell_level(levels, entry_cfg["crossing_sell_level"])
            slc_buy_lvl  = config_loader.resolve_buy_level (levels, sl_cfg_idx["variant_c_buy_level"])
            slc_sell_lvl = config_loader.resolve_sell_level(levels, sl_cfg_idx["variant_c_sell_level"])
            long_target_lvl  = (levels.get("buy")  or {}).get(target_cfg["ce_level"])
            short_target_lvl = (levels.get("sell") or {}).get(target_cfg["pe_level"])
            prev_spot = _future_auto_state["last_spot"].get(idx_name)
            fut_ltp = fut.get("ltp")

            # ---- EXIT check ----
            open_t = open_by_underlying.get(idx_name)
            if open_t:
                reason = _check_futures_exit_reason(
                    open_t, fut_ltp, spot,
                    slc_buy_lvl, slc_sell_lvl,
                    long_target_lvl, short_target_lvl,
                )
                if reason and fut_ltp is not None:
                    _execute_futures_exit(open_t, float(fut_ltp), reason,
                                           future_data, client)
                    _future_auto_state["last_spot"][idx_name] = spot
                    continue

            # ---- ENTRY check ----
            if (idx_name not in open_by_underlying
                    and _can_open_more(idx_name, counts)):
                already_evaluated_open = (
                    _future_auto_state["open_evaluated"].get(idx_name) == today
                    or counts.get(idx_name, 0) > 0
                )
                side, stamp_now = _compute_futures_entry_signal(
                    idx_name, spot, prev_spot, levels, cfg,
                    already_evaluated_open=already_evaluated_open,
                )
                if stamp_now:
                    _future_auto_state["open_evaluated"][idx_name] = today
                # If side is set on the market-open path, the stamp is
                # deferred — it lands just before _execute_futures_entry
                # below, so a missing fut_ltp at this tick won't burn
                # the open-evaluation slot.

                if side and fut_ltp is not None:
                    _future_auto_state["open_evaluated"][idx_name] = today
                    placed = _execute_futures_entry(
                        idx_name, side, fut, float(fut_ltp), float(spot),
                        client,
                    )
                    if placed:
                        counts[idx_name] = counts.get(idx_name, 0) + 1

            _future_auto_state["last_spot"][idx_name] = spot


# ---------- entry / exit order placement ----------
def _execute_futures_entry(idx_name, side, fut, fut_ltp, spot, client):
    """Place a real BUY (long) or SELL (short) future order.

    `side` is "BUY" or "SELL" (full word — matches ledger order_type
    semantics so PnL math in _auto_close works without translation).
    """
    trading_symbol = fut.get("trading_symbol")
    exchange       = fut.get("exchange")
    expiry         = fut.get("expiry")
    sdk_lot_size   = fut.get("lot_size")
    if not trading_symbol or not exchange or not sdk_lot_size:
        audit("FUT_ENTRY_REFUSED_NO_CONTRACT",
              idx=idx_name, trading_symbol=trading_symbol,
              exchange=exchange, lot_size=sdk_lot_size)
        append_blocked(
            kind="ENTRY", scrip=f"{idx_name} FUT", side="B" if side == "BUY" else "S",
            qty=0, price=fut_ltp,
            result="NO_CONTRACT",
            message=f"Missing futures contract metadata for {idx_name}",
            underlying=idx_name, trading_symbol=trading_symbol,
            trigger_spot=spot,
            trigger_level="BUY" if side == "BUY" else "SELL",
            source="auto_futures",
        )
        return False

    step = config_loader.futures_round_step(idx_name)
    if side == "BUY":
        limit_price = _round_for_buy(fut_ltp, step)
    else:
        limit_price = _round_for_sell(fut_ltp, step)

    # Unified lot multiplier — same lots config as options strategy.
    qty = int(sdk_lot_size) * config_loader.lot_multiplier(idx_name)

    scrip = {
        "symbol": f"{idx_name} FUT",
        "trading_symbol": trading_symbol,
        "exchange": exchange,
    }
    res = place_order_safe(
        client=client, scrip=scrip,
        side="B" if side == "BUY" else "S",
        qty=qty, price=limit_price,
        order_type="L", product="MIS", validity="DAY",
        trigger="0", tag=f"auto:fut:{idx_name}:{side}",
        live_mode=LIVE_MODE,
        available_cash=_fetch_available_cash(client),
        lot_size=int(sdk_lot_size), source="auto_futures",
    )

    if res["result"] not in (RESULT_OK, RESULT_PAPER):
        append_blocked(
            kind="ENTRY", scrip=f"{idx_name} FUT",
            side="B" if side == "BUY" else "S",
            qty=qty, price=limit_price,
            result=res["result"], message=res["message"],
            underlying=idx_name, trading_symbol=trading_symbol,
            trigger_spot=spot,
            trigger_level="BUY" if side == "BUY" else "SELL",
            source="auto_futures",
        )
        return False

    mode = "LIVE" if res["result"] == RESULT_OK else "PAPER"
    kotak_order_id = res.get("order_id") if mode == "LIVE" else None
    now = now_ist()
    trades = read_trade_ledger()
    row = {
        "id": next_trade_id(trades),
        "date": now.strftime("%Y-%m-%d"),
        "scrip": f"{idx_name} FUT",
        "option_key": None,
        "asset_type": "future",
        "underlying": idx_name,
        "strike": None,
        "option_type": None,
        "expiry": str(expiry) if expiry else None,
        "trading_symbol": trading_symbol,
        "order_type": side,    # "BUY" (long) or "SELL" (short)
        "entry_time": now.strftime("%H:%M:%S"),
        "entry_ts": now.timestamp(),
        "entry_price": round(float(limit_price), 2),
        "qty": qty,
        "trigger_spot": round(spot, 2),
        "trigger_level": "BUY" if side == "BUY" else "SELL",
        "max_min_target_price": round(float(limit_price), 2),
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
    audit("AUTO_FUT_ENTRY_PLACED", scrip=f"{idx_name} FUT",
          side=side, mode=mode, kotak_order_id=kotak_order_id,
          qty=qty, price=limit_price, ltp=fut_ltp, step=step)
    return True


def _execute_futures_exit(open_trade, fut_ltp, reason, future_data, client):
    """Close a futures position. Long open -> SELL; short open -> BUY.

    Uses position verification (same as options) before sending in LIVE
    mode, so a closed/flat Kotak position never produces a fresh
    opposite-side order.
    """
    idx_name = open_trade.get("underlying")
    open_side = open_trade.get("order_type")  # "BUY" or "SELL"
    fut = future_data.get(idx_name) or {}
    trading_symbol = (open_trade.get("trading_symbol")
                      or fut.get("trading_symbol"))
    exchange = fut.get("exchange") or INDEX_OPTIONS_CONFIG[idx_name]["exchange_segment"]

    if not trading_symbol:
        audit("FUT_EXIT_REFUSED_NO_TRADING_SYMBOL",
              scrip=f"{idx_name} FUT", reason=reason)
        append_blocked(
            kind="EXIT", scrip=f"{idx_name} FUT",
            side="S" if open_side == "BUY" else "B",
            qty=int(open_trade.get("qty") or 0),
            price=fut_ltp, result="NO_TRADING_SYMBOL",
            message="Could not resolve trading symbol for futures exit",
            underlying=idx_name, source="auto_futures",
        )
        return False

    # LIVE only: verify position is still open with Kotak before exit.
    if LIVE_MODE:
        if client is None:
            audit("FUT_EXIT_REFUSED_NO_CLIENT",
                  scrip=f"{idx_name} FUT", reason=reason)
            append_blocked(
                kind="EXIT", scrip=f"{idx_name} FUT",
                side="S" if open_side == "BUY" else "B",
                qty=int(open_trade.get("qty") or 0),
                price=fut_ltp, result="NO_CLIENT",
                message="Kotak client not initialised — cannot verify position",
                underlying=idx_name, trading_symbol=trading_symbol,
                source="auto_futures",
            )
            return False
        verify_side = "BUY" if open_side == "BUY" else "SELL"
        ok, info = verify_open_position(client, trading_symbol,
                                         side=verify_side)
        if not ok:
            audit("FUT_EXIT_REFUSED_NO_KOTAK_POSITION",
                  scrip=f"{idx_name} FUT",
                  trading_symbol=trading_symbol,
                  kotak_info=info)
            append_blocked(
                kind="EXIT", scrip=f"{idx_name} FUT",
                side="S" if open_side == "BUY" else "B",
                qty=int(open_trade.get("qty") or 0),
                price=fut_ltp, result="NO_KOTAK_POSITION",
                message=("Kotak shows no matching open position — refusing "
                         "to send close (would open opposite-side fresh)."),
                underlying=idx_name, trading_symbol=trading_symbol,
                source="auto_futures",
            )
            return False

    qty = int(open_trade.get("qty") or 0)
    if qty <= 0:
        audit("FUT_EXIT_REFUSED_NO_QTY", scrip=f"{idx_name} FUT")
        return False

    step = config_loader.futures_round_step(idx_name)
    limit_price = _close_round(open_trade, fut_ltp, step)

    scrip = {
        "symbol": f"{idx_name} FUT",
        "trading_symbol": trading_symbol,
        "exchange": exchange,
    }
    # Close opposite of open side: long(BUY) -> SELL("S"); short(SELL) -> BUY("B").
    close_side = "S" if open_side == "BUY" else "B"
    res = place_order_safe(
        client=client, scrip=scrip, side=close_side,
        qty=qty, price=limit_price,
        order_type="L", product="MIS", validity="DAY",
        trigger="0", tag=f"auto:fut:exit:{idx_name}",
        live_mode=LIVE_MODE,
        available_cash=_fetch_available_cash(client),
        lot_size=qty, source="auto_futures",
    )

    if res["result"] not in (RESULT_OK, RESULT_PAPER):
        append_blocked(
            kind="EXIT", scrip=f"{idx_name} FUT",
            side=close_side, qty=qty, price=limit_price,
            result=res["result"], message=res["message"],
            underlying=idx_name, trading_symbol=trading_symbol,
            source="auto_futures",
        )
        return False

    mode = "LIVE" if res["result"] == RESULT_OK else "PAPER"
    kotak_order_id = res.get("order_id") if mode == "LIVE" else None
    now = now_ist()
    exit_price = float(limit_price)
    entry_price = float(open_trade.get("entry_price") or exit_price)
    # Long(BUY): pnl = exit - entry. Short(SELL): pnl = entry - exit.
    pnl = (exit_price - entry_price) if open_side == "BUY" \
          else (entry_price - exit_price)

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
    audit("AUTO_FUT_EXIT_PLACED", scrip=f"{idx_name} FUT",
          mode=mode, reason=reason, kotak_order_id=kotak_order_id,
          qty=qty, price=limit_price, side=close_side)
    return True

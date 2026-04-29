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

from backend import config_loader
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
    _auto_at_or_after_squareoff, _auto_in_hours, _derive_exit_level,
)
# Reuse the same entry-reason derivation the paper book uses, so the
# live ledger and the paper book label entries identically (e.g.
# OPEN_ABOVE_BUY_WA, CROSS_UP_BUY_WA, OPEN_BELOW_SELL, CROSS_DOWN_SELL_WA).
from backend.strategy.paper_book import _derive_option_entry_reason
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
# Master enable flag for the whole auto-strategy. Code-only switch on
# purpose; all OTHER tunables now live in config.yaml (editable from the
# /config web page) — see backend/config_loader.py.
AUTO_OPTION_STRATEGY_ENABLED = True
# ----------------------------------------------------------------------


_option_auto_state = {
    # Phase 2: per-engine state. Each logic engine ("current" / "reverse")
    # tracks its own last_spot + open_evaluated independently so a CE entry
    # on the current engine doesn't suppress a CE entry on the reverse
    # engine (and vice-versa). The lock is shared — only one engine ticks
    # at a time inside a single tick() call.
    "current": {
        "last_spot": {},        # index_name -> last seen spot
        "open_evaluated": {},   # index_name -> "YYYY-MM-DD" once we've done
                                # the market-open evaluation for that day.
    },
    "reverse": {
        "last_spot": {},
        "open_evaluated": {},
    },
    "lock": threading.Lock(),
}


def _check_exit_reason(open_t, opt_ltp, spot,
                       buy_lvl, sell_lvl, ce_target_lvl, pe_target_lvl,
                       cfg=None):
    """Return the exit reason string for an open option trade, or None.

    =======================================================================
    EXIT CONDITIONS — checked in order, first hit wins.
    =======================================================================

    1) STOP LOSS — four variants. The ACTIVE one is selected by
       config.yaml -> stoploss.active (A | B | C | D). All four remain in
       code so Ganesh can flip between them from the /config page.

       Variant A — Fixed premium drop (₹X below entry).
                   Param:  stoploss.variant_a_drop_rs
       Variant B — Percentage premium drop (X% from entry).
                   Param:  stoploss.variant_b_drop_pct
       Variant C — Spot reverses through chosen Gann level.
                   CE: spot < variant_c_sell_level pick (SELL or SELL_WA)
                   PE: spot > variant_c_buy_level  pick (BUY  or BUY_WA)
       Variant D — Trailing along the Gann ladder. SL trails one rung
                   behind spot's current rung. Initial SL = entry price.
                   Triggers on spot crossing trail_sl_price; close fills
                   at instrument LTP.

    2) PROFIT TARGET — spot reaches the configured Gann level.
       config.yaml -> target.ce_level / target.pe_level. The level value
       (T1/T2/.../WA) is resolved by the caller and passed in here.

    3) TIME SQUAREOFF — clock hits configured square_off time. Handled at
       the top of tick(), not in this function.
    =======================================================================
    """
    entry_price = open_t.get("entry_price")
    side = open_t.get("option_type")  # 'CE' or 'PE'

    # Phase 2: when called from the per-engine tick, `cfg` is the engine
    # block ({stoploss, target, ...}). Fall back to the top-level (current
    # engine) config for legacy callers that don't pass cfg.
    if cfg is None:
        cfg = config_loader.engine_block("current")
    sl_cfg = cfg["stoploss"]
    active_sl = sl_cfg["active"]   # validated to A|B|C|D by loader

    # ---------- (1) STOP LOSS — exactly ONE variant runs ----------
    if active_sl == "A":
        # Fixed ₹X premium drop below entry.
        drop_rs = sl_cfg["variant_a_drop_rs"]
        if opt_ltp is not None and entry_price is not None:
            if opt_ltp <= entry_price - drop_rs:
                return "SL_PREMIUM_FIXED"

    elif active_sl == "B":
        # Percentage premium drop from entry. 30 -> exit when premium <= 70% of entry.
        drop_pct = sl_cfg["variant_b_drop_pct"]
        if opt_ltp is not None and entry_price is not None:
            if opt_ltp <= entry_price * (1 - drop_pct / 100.0):
                return "SL_PREMIUM_PCT"

    elif active_sl == "C":
        # Spot reverses through opposite Gann level.
        if side == "CE":
            if sell_lvl is not None and spot < sell_lvl:
                return "SL_SELL_LVL"
        else:  # PE
            if buy_lvl is not None and spot > buy_lvl:
                return "SL_BUY_LVL"

    elif active_sl == "D":
        # Trailing along Gann ladder. trail_sl_price is set by
        # update_open_trades_mfe on each in-hours snapshot refresh
        # (~2s cadence). Until the first refresh after entry it may
        # be None — guard explicitly.
        # Direction: CE = bullish-bet (long premium), exit when spot
        # reverses DOWN through trail. PE = bearish-bet (long
        # premium on a put), exit when spot reverses UP through
        # trail. Both branches are "long the option premium" but
        # walk opposite spot ladders.
        trail = open_t.get("trail_sl_price")
        if trail is not None and spot is not None:
            if side == "CE" and spot <= trail:
                return "SL_TRAIL"
            if side == "PE" and spot >= trail:
                return "SL_TRAIL"

    # ---------- (2) PROFIT TARGET ----------
    # ce_target_lvl / pe_target_lvl are the resolved numeric Gann level
    # values for the configured target name (T1/T2/T3/BUY_WA on CE side,
    # S1/S2/S3/SELL_WA on PE side).
    if side == "CE":
        if ce_target_lvl is not None and spot >= ce_target_lvl:
            return f"TARGET_{cfg['target']['ce_level']}"
    else:  # PE
        if pe_target_lvl is not None and spot <= pe_target_lvl:
            return f"TARGET_{cfg['target']['pe_level']}"

    return None


def _can_open_more(idx_name, counts, engine="current"):
    """Per-day cap check. None (or missing) in config means unlimited.
    Reads the engine-specific cap so the reverse engine has its own cap."""
    cap = config_loader.engine_per_day_cap(engine, idx_name)
    if cap is None:
        return True
    return counts.get(idx_name, 0) < cap


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


def _compute_entry_signal(idx_name, spot, prev_spot, levels, cfg,
                          already_evaluated_open):
    """Pure decision: which side (if any), and should the caller stamp
    `open_evaluated` immediately?

    Returns ("CE"|"PE"|None, stamp_now: bool).

    stamp_now=True means: caller should stamp open_evaluated[idx]=today
    NOW. stamp_now=False means: caller defers (used when side IS set on
    the market-open path, since we want to wait until opt_ltp is
    available before burning the open-evaluation slot).

    No I/O, no ledger reads, no order placement — just the
    market-open-path / crossing-path logic that previously lived
    inline in the live tick. Shared by live and paper books.
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
                side = "CE"
            elif mo_sell_lvl is not None and spot < mo_sell_lvl:
                side = "PE"
            # Stamp NOW only if no signal — defer if side is set.
            stamp_now = (side is None)
        else:
            # Path A disabled — stamp NOW so subsequent ticks fall
            # through to the crossing branch.
            stamp_now = True
    elif prev_spot is not None and entry_cfg["crossing_path"]:
        if cr_buy_lvl is not None and prev_spot <= cr_buy_lvl < spot:
            side = "CE"
        elif cr_sell_lvl is not None and prev_spot >= cr_sell_lvl > spot:
            side = "PE"

    return side, stamp_now


def option_auto_strategy_tick(option_data, option_index_meta, gann_quotes,
                              client=None, engine="current"):
    """One tick of the option auto-strategy.

    option_data:        {key: {index, strike, option_type, ltp, ...}}
    option_index_meta:  {index_name: {spot, atm, expiry, ...}}
    gann_quotes:        stock-side quotes (need .levels / .rev_levels for the index spots)
    client:             Kotak NeoAPI client. Required when LIVE_MODE=True.
    engine:             "current" (open-anchored Gann; default) or
                        "reverse" (anchored to today's running intraday
                        low/high — see strategy/gann.py reverse_buy_levels
                        / reverse_sell_levels). The reverse engine reads
                        gq["rev_levels"] and the `reverse_engine` config
                        block, and tags every row/blocker with
                        engine="reverse".

    Phase 4: every signal goes straight through place_order_safe and writes
    a trade row on success. No banner, no human confirm.
    """
    if not AUTO_OPTION_STRATEGY_ENABLED or not option_index_meta:
        return
    # Phase 2 master flag — current_logic.enabled / reverse_logic.enabled.
    if not config_loader.engine_enabled(engine):
        return
    # Engine gate — real (sub-)engine on AND apply_to includes options.
    if not config_loader.real_options_enabled():
        return
    now = now_ist()

    # Engine-specific config block + state slot. Reverse uses `rev_levels`
    # off gann_quotes; current uses `levels`.
    cfg_eng    = config_loader.engine_block(engine)
    eng_state  = _option_auto_state[engine]
    levels_key = "rev_levels" if engine == "reverse" else "levels"

    with _option_auto_state["lock"]:
        trades = read_trade_ledger()

        # 1. SQUARE OFF at/after 15:15 — close everything OPEN for THIS engine
        if _auto_at_or_after_squareoff(now):
            for t in trades:
                if (t.get("status") != "OPEN"
                        or t.get("asset_type") != "option"):
                    continue
                # Only close rows owned by this engine (legacy rows with
                # no engine field are treated as 'current').
                if (t.get("engine") or "current") != engine:
                    continue
                q = option_data.get(t.get("option_key"))
                if not q or q.get("ltp") is None:
                    continue
                # Look up spot for this row's underlying so the exit
                # row records spot at exit (UI shows "exited at N").
                so_meta = option_index_meta.get(t.get("underlying")) or {}
                _execute_exit(t, float(q["ltp"]), "AUTO_SQUARE_OFF",
                              option_index_meta, client,
                              spot=so_meta.get("spot"), engine=engine)
            return

        if not _auto_in_hours(now):
            return

        # Daily per-index trade cap — counts trades placed today FOR THIS engine.
        today = now.strftime("%Y-%m-%d")
        counts = {}
        for t in trades:
            if (t.get("date") == today
                    and t.get("asset_type") == "option"
                    and (t.get("engine") or "current") == engine):
                u = t.get("underlying", "")
                counts[u] = counts.get(u, 0) + 1
        open_by_underlying = {
            t["underlying"]: t for t in trades
            if (t.get("status") == "OPEN"
                and t.get("asset_type") == "option"
                and (t.get("engine") or "current") == engine)
        }

        for idx_name, m in option_index_meta.items():
            spot = m.get("spot")
            atm  = m.get("atm")
            if spot is None or atm is None:
                continue
            gann_sym = INDEX_OPTIONS_CONFIG[idx_name]["spot_symbol_key"]
            gq = gann_quotes.get(gann_sym) or {}
            levels  = gq.get(levels_key) or {}
            entry_cfg  = cfg_eng["entry"]
            sl_cfg_idx = cfg_eng["stoploss"]
            cfg_target = cfg_eng["target"]
            # Per-row level resolution (Ganesh's BUY/BUY_WA + SELL/SELL_WA dropdowns):
            # entry uses market_open_* / crossing_* picks.
            # variant C exit uses variant_c_* picks (CE exit if spot < sell pick;
            # PE exit if spot > buy pick).
            cr_buy_lvl   = config_loader.resolve_buy_level (levels, entry_cfg["crossing_buy_level"])  # noqa: F841
            cr_sell_lvl  = config_loader.resolve_sell_level(levels, entry_cfg["crossing_sell_level"]) # noqa: F841
            slc_buy_lvl  = config_loader.resolve_buy_level (levels, sl_cfg_idx["variant_c_buy_level"])
            slc_sell_lvl = config_loader.resolve_sell_level(levels, sl_cfg_idx["variant_c_sell_level"])
            # Profit-target levels per engine config: CE side reads from `buy`
            # group (BUY/BUY_WA/T1/T2/T3); PE side reads from `sell` group
            # (SELL/SELL_WA/S1/S2/S3).
            ce_target_lvl = (levels.get("buy")  or {}).get(cfg_target["ce_level"])
            pe_target_lvl = (levels.get("sell") or {}).get(cfg_target["pe_level"])
            prev_spot = eng_state["last_spot"].get(idx_name)

            # ---- EXIT check ----
            open_t = open_by_underlying.get(idx_name)
            if open_t:
                opt_q = option_data.get(open_t.get("option_key"))
                opt_ltp = (opt_q or {}).get("ltp")
                # Fallback: when this strike has drifted out of the
                # ATM window option_data carries, read the WS feed
                # directly via the row's stored token+exchange. Without
                # this, variant-D SL_TRAIL never fires on out-of-window
                # strikes because the opt_ltp guard below blocks it.
                if opt_ltp is None:
                    token = open_t.get("instrument_token")
                    exch  = open_t.get("exchange_segment")
                    if token and exch:
                        from backend.quotes import _feed
                        tick = _feed.get(exch, str(token)) or {}
                        opt_ltp = tick.get("ltp")
                reason = _check_exit_reason(
                    open_t, opt_ltp, spot,
                    slc_buy_lvl, slc_sell_lvl,
                    ce_target_lvl, pe_target_lvl,
                    cfg=cfg_eng,
                )
                if reason and opt_ltp is not None:
                    _execute_exit(open_t, float(opt_ltp), reason,
                                  option_index_meta, client, spot=spot,
                                  engine=engine)
                    eng_state["last_spot"][idx_name] = spot
                    continue

            # Per-index gate: when Ganesh unticks this index for the
            # real (sub-)engine of this logic engine on the config page,
            # skip ENTRY here. Exits + square-off above still run so any
            # already-OPEN position gets cleaned up.
            if not config_loader.engine_index_enabled_for(engine, "real", idx_name):
                eng_state["last_spot"][idx_name] = spot
                continue

            # Phase 3 — per-engine halt gate. is_halted is global; the
            # per-engine flag is engaged automatically by auto-drawdown
            # when this engine alone breaches its threshold. Either way,
            # block NEW entries; exits + square-off above still run so
            # OPEN positions can be cleaned up.
            from backend.safety.kill_switch import is_halted, is_engine_halted
            if is_halted() or is_engine_halted(engine):
                eng_state["last_spot"][idx_name] = spot
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
                    and _can_open_more(idx_name, counts, engine=engine)):
                today_str = today  # already computed above
                already_evaluated_open = (
                    eng_state["open_evaluated"].get(idx_name) == today_str
                    or counts.get(idx_name, 0) > 0
                )
                option_type, stamp_now = _compute_entry_signal(
                    idx_name, spot, prev_spot, levels, cfg_eng,
                    already_evaluated_open=already_evaluated_open,
                )
                if stamp_now:
                    # In-channel at market open, or market_open_path
                    # disabled — mark evaluated so we don't re-check
                    # market-open every tick. (Crossing branch never
                    # needs a stamp.)
                    eng_state["open_evaluated"][idx_name] = today_str
                # If option_type IS set on the market-open path, the
                # stamp is deferred — it lands just before _execute_entry,
                # so a missing opt_ltp at this exact tick won't burn the
                # open-evaluation slot for the day.

                if option_type:
                    opt_key = f"{idx_name} {atm} {option_type}"
                    opt_q = option_data.get(opt_key)
                    opt_ltp = (opt_q or {}).get("ltp")
                    if opt_ltp is not None:
                        # Stamp open_evaluated NOW (whether the order
                        # ultimately succeeds, blocks on margin, or hits a
                        # Kotak error). Either way we tried — don't burn the
                        # slot earlier when LTP might still be missing.
                        eng_state["open_evaluated"][idx_name] = today_str
                        # Same entry_reason derivation as the paper book —
                        # feeds the new "Entry Reason" column on /trades.
                        entry_reason = _derive_option_entry_reason(
                            option_type, already_evaluated_open, entry_cfg,
                        )
                        if engine == "reverse" and entry_reason:
                            entry_reason = "REV_" + entry_reason
                        placed = _execute_entry(
                            idx_name, atm, option_type, opt_key,
                            float(opt_ltp), float(spot),
                            m.get("expiry"), client,
                            entry_reason=entry_reason,
                            option_quote=opt_q,
                            engine=engine,
                        )
                        if placed:
                            counts[idx_name] = counts.get(idx_name, 0) + 1

            eng_state["last_spot"][idx_name] = spot


def _execute_entry(idx_name, atm, option_type, opt_key,
                   opt_ltp, spot, expiry, client,
                   entry_reason=None, option_quote=None, engine="current"):
    """Place a real BUY order via place_order_safe and write trade ledger row.

    Returns True if the order was PLACED (LIVE) or recorded (PAPER), False
    if it was refused. The caller uses this to decide whether to bump the
    daily count.

    `engine` is the logic engine name ("current" or "reverse") — written
    onto the trade row and the blocker row so /trades and /blockers can
    show which logic engine fired.
    """
    cfg = INDEX_OPTIONS_CONFIG.get(idx_name) or {}
    lot_size = cfg.get("lot_size")
    if not lot_size:
        audit("ORDER_REFUSED_NO_LOT_SIZE", scrip=opt_key, idx=idx_name,
              engine=engine)
        append_blocked(
            kind="ENTRY", scrip=opt_key, side="B", qty=0, price=opt_ltp,
            result="NO_LOT_SIZE",
            message=f"No lot size configured for {idx_name}",
            underlying=idx_name, strike=atm, option_type=option_type,
            trigger_spot=spot,
            trigger_level="BUY" if option_type == "CE" else "SELL",
            engine=engine,
        )
        return False
    # Apply user-configured lot multiplier — engine-specific (current vs
    # reverse can have different lot sizes). Final qty = broker's lot_size ×
    # multiplier (e.g. NIFTY 65 × 2 = 130 qty).
    qty = lot_size * config_loader.engine_lot_multiplier(engine, idx_name)
    trading_symbol = _option_trading_symbol(idx_name, atm, option_type, expiry)
    if not trading_symbol:
        audit("ORDER_REFUSED_NO_TRADING_SYMBOL",
              scrip=opt_key, idx=idx_name, expiry=str(expiry),
              engine=engine)
        append_blocked(
            kind="ENTRY", scrip=opt_key, side="B", qty=qty, price=opt_ltp,
            result="NO_TRADING_SYMBOL",
            message=f"Could not build trading symbol for expiry={expiry}",
            underlying=idx_name, strike=atm, option_type=option_type,
            trigger_spot=spot,
            trigger_level="BUY" if option_type == "CE" else "SELL",
            engine=engine,
        )
        return False

    scrip = {
        "symbol": opt_key,
        "trading_symbol": trading_symbol,
        "exchange": cfg["exchange_segment"],
    }
    res = place_order_safe(
        client=client, scrip=scrip, side="B",
        qty=qty, price=opt_ltp,
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
            kind="ENTRY", scrip=opt_key, side="B", qty=qty,
            price=opt_ltp, result=res["result"], message=res["message"],
            underlying=idx_name, strike=atm, option_type=option_type,
            trading_symbol=trading_symbol, trigger_spot=spot,
            trigger_level="BUY" if option_type == "CE" else "SELL",
            engine=engine,
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
        # Phase 2: which logic engine fired this trade.
        "engine": engine,
        "strike": atm,
        "option_type": option_type,
        "expiry": str(expiry) if expiry else None,
        "trading_symbol": trading_symbol,
        # A.1 — persist the instrument identity so /trades, exits, and any
        # downstream re-subscription can resolve the leg without re-deriving
        # it from option_key. Mirrors the paper-book row shape.
        "instrument_token":   (option_quote or {}).get("token"),
        "exchange_segment":   cfg.get("exchange_segment"),
        "order_type": "BUY",
        "entry_time": now.strftime("%H:%M:%S"),
        "entry_ts": now.timestamp(),
        "entry_price": round(opt_ltp, 2),
        "qty": qty,
        "trigger_spot": round(spot, 2),
        "trigger_level": "BUY" if option_type == "CE" else "SELL",
        # entry_reason mirrors the paper-book convention so /trades and
        # /paper-trades label the WHY identically.
        "entry_reason": entry_reason,
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
          kotak_order_id=kotak_order_id, qty=qty, price=opt_ltp,
          engine=engine)
    return True


def _execute_exit(open_trade, opt_ltp, reason, option_index_meta, client,
                  spot=None, engine=None):
    """Place a real SELL order to close `open_trade` and update the ledger.

    LIVE only: verify with Kotak that the position is still open before
    sending the SELL (don't open a fresh short on a closed position).
    PAPER skips verification — there's nothing real to verify.

    `spot` records the underlying spot at exit so the closed row carries
    the spot value and Gann level (T1/S2/etc.) for the UI.

    `engine` (optional) overrides the open trade's stored engine name.
    If not given, the open trade's engine field is used (legacy rows
    without one fall back to "current").
    """
    if engine is None:
        engine = open_trade.get("engine") or "current"
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
        audit("EXIT_REFUSED_NO_TRADING_SYMBOL", scrip=opt_key, engine=engine)
        append_blocked(
            kind="EXIT", scrip=opt_key, side="S", qty=int(lot_size),
            price=opt_ltp, result="NO_TRADING_SYMBOL",
            message="Could not build trading symbol for exit",
            underlying=idx_name, strike=open_trade.get("strike"),
            option_type=open_trade.get("option_type"),
            engine=engine,
        )
        return False

    # LIVE only: verify Kotak shows the position open before exit.
    if LIVE_MODE:
        if client is None:
            audit("EXIT_REFUSED_NO_CLIENT", scrip=opt_key, reason=reason,
                  engine=engine)
            append_blocked(
                kind="EXIT", scrip=opt_key, side="S", qty=int(lot_size),
                price=opt_ltp, result="NO_CLIENT",
                message="Kotak client not initialised — cannot verify position",
                underlying=idx_name, strike=open_trade.get("strike"),
                option_type=open_trade.get("option_type"),
                trading_symbol=trading_symbol,
                engine=engine,
            )
            return False
        ok, info = verify_open_position(client, trading_symbol, side="BUY")
        if not ok:
            audit("EXIT_REFUSED_NO_KOTAK_POSITION",
                  scrip=opt_key, trading_symbol=trading_symbol,
                  kotak_info=info, engine=engine)
            append_blocked(
                kind="EXIT", scrip=opt_key, side="S", qty=int(lot_size),
                price=opt_ltp, result="NO_KOTAK_POSITION",
                message=("Kotak shows no matching open position — refusing "
                         "to send SELL (would open a fresh short)."),
                underlying=idx_name, strike=open_trade.get("strike"),
                option_type=open_trade.get("option_type"),
                trading_symbol=trading_symbol,
                engine=engine,
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
            engine=engine,
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
            t["exit_spot"] = (round(float(spot), 2)
                              if spot is not None else None)
            t["exit_level"] = _derive_exit_level(t, reason)
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
          kotak_order_id=kotak_order_id, qty=qty, price=opt_ltp,
          engine=engine)
    return True

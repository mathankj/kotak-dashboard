"""Paper book — runs the live strategy logic against its own ledger.

Operates fully independently of the live trade ledger. Never sends
real orders. Kill switch does not freeze paper. Per-day caps are
counted per ledger.

Spec: docs/superpowers/specs/2026-04-27-trailing-paper-l5-design.md
(Phase 2).
"""
import threading

from backend import config_loader
from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
from backend.storage.paper_ledger import (
    read_paper_ledger, write_paper_ledger, next_paper_id,
)
from backend.strategy.common import (
    _auto_at_or_after_squareoff, _auto_in_hours, _auto_close,
)
from backend.utils import now_ist


_paper_state = {
    "options_lock": threading.Lock(),
    "futures_lock": threading.Lock(),
    "options_open_evaluated": {},
    "futures_open_evaluated": {},
    "options_last_spot": {},
    "futures_last_spot": {},
}


# ---------- entry reason helpers ----------
# Derive a short human label so the paper-book UI can show WHY the
# trade fired (e.g. "OPEN_ABOVE_BUY_WA" vs "CROSS_UP_BUY_WA"). The
# pure-decision _compute_entry_signal() hides this distinction; we
# reconstruct it from the same inputs the caller already has.

def _derive_option_entry_reason(option_type, already_evaluated_open, entry_cfg):
    """option_type: 'CE' bullish or 'PE' bearish. entry_cfg: cfg['entry']."""
    market_open_path = (not already_evaluated_open
                        and entry_cfg.get("market_open_path"))
    if option_type == "CE":
        if market_open_path:
            return f"OPEN_ABOVE_{entry_cfg['market_open_buy_level']}"
        return f"CROSS_UP_{entry_cfg['crossing_buy_level']}"
    if option_type == "PE":
        if market_open_path:
            return f"OPEN_BELOW_{entry_cfg['market_open_sell_level']}"
        return f"CROSS_DOWN_{entry_cfg['crossing_sell_level']}"
    return None


def _derive_futures_entry_reason(side, already_evaluated_open, entry_cfg):
    """side: 'BUY' long or 'SELL' short."""
    market_open_path = (not already_evaluated_open
                        and entry_cfg.get("market_open_path"))
    if side == "BUY":
        if market_open_path:
            return f"OPEN_ABOVE_{entry_cfg['market_open_buy_level']}"
        return f"CROSS_UP_{entry_cfg['crossing_buy_level']}"
    if side == "SELL":
        if market_open_path:
            return f"OPEN_BELOW_{entry_cfg['market_open_sell_level']}"
        return f"CROSS_DOWN_{entry_cfg['crossing_sell_level']}"
    return None


# ---------- low-level paper writes ----------
def _paper_execute_entry(row):
    """Insert an OPEN paper row. `row` MUST be a fully-populated dict
    (same schema as a live trade-ledger row). Caller assigns id; we
    stamp mode/status/kotak_*_order_id."""
    rows = read_paper_ledger()
    row = dict(row)  # never mutate the caller's dict
    row["mode"] = "PAPER_BOOK"
    row["status"] = "OPEN"
    row["kotak_entry_order_id"] = None
    row["kotak_exit_order_id"] = None
    if "id" not in row or not row["id"]:
        row["id"] = next_paper_id(rows)
    rows.insert(0, row)
    write_paper_ledger(rows)
    return row


def _paper_execute_exit(open_row, ltp, reason, spot=None):
    """Close a paper OPEN row at `ltp` with the given reason. `spot`
    is the underlying spot at exit — recorded so the UI can show
    'exited at spot N (level)'."""
    rows = read_paper_ledger()
    for t in rows:
        if t.get("id") == open_row.get("id") and t.get("status") == "OPEN":
            _auto_close(t, float(ltp), now_ist(), reason, spot=spot)
            t["mode"] = "PAPER_BOOK"
            t["kotak_exit_order_id"] = None
            break
    write_paper_ledger(rows)


# ---------- high-level ticks ----------
def paper_options_tick(option_data, option_index_meta, gann_quotes):
    """Paper analogue of option_auto_strategy_tick.

    No `client` param — never sends orders. Reuses the SAME entry
    signal + exit reason functions as the live tick (imported lazily
    from backend.strategy.options) — single source of truth for
    strategy logic.
    """
    # Lazy import to avoid module-load cycles.
    from backend.strategy.options import (
        _compute_entry_signal, _check_exit_reason,
        _option_trading_symbol,
    )

    if not option_index_meta:
        return
    if not config_loader.paper_options_enabled():
        return

    now = now_ist()

    with _paper_state["options_lock"]:
        rows = read_paper_ledger()
        today = now.strftime("%Y-%m-%d")

        # 1. SQUARE OFF at/after configured square_off — close all OPEN
        if _auto_at_or_after_squareoff(now):
            for t in rows:
                if (t.get("status") != "OPEN"
                        or t.get("asset_type") != "option"):
                    continue
                q = option_data.get(t.get("option_key"))
                if not q or q.get("ltp") is None:
                    continue
                # Look up spot from gann_quotes for the row's
                # underlying so the exit row records spot at exit.
                so_key = (INDEX_OPTIONS_CONFIG.get(t.get("underlying"))
                          or {}).get("spot_symbol_key")
                so_val = ((gann_quotes.get(so_key) or {}).get("ltp")
                          if so_key else None)
                _paper_execute_exit(t, float(q["ltp"]), "AUTO_SQUARE_OFF",
                                    spot=so_val)
            return

        if not _auto_in_hours(now):
            return

        # Paper-side counts — independent of live ledger.
        paper_counts = {}
        for r in rows:
            if (r.get("asset_type") == "option"
                    and r.get("date") == today):
                u = r.get("underlying")
                if u:
                    paper_counts[u] = paper_counts.get(u, 0) + 1
        open_by_underlying = {
            t["underlying"]: t for t in rows
            if t.get("status") == "OPEN" and t.get("asset_type") == "option"
        }

        def _paper_can_open_more(idx_name):
            cap = config_loader.per_day_cap(idx_name)
            if cap is None:
                return True
            return paper_counts.get(idx_name, 0) < cap

        cfg_full   = config_loader.get()
        sl_cfg_idx = cfg_full["stoploss"]
        cfg_target = cfg_full["target"]

        for idx_name, m in option_index_meta.items():
            spot = m.get("spot")
            atm  = m.get("atm")
            if spot is None or atm is None:
                continue
            gann_sym = INDEX_OPTIONS_CONFIG[idx_name]["spot_symbol_key"]
            gq = gann_quotes.get(gann_sym) or {}
            levels = gq.get("levels") or {}
            slc_buy_lvl  = config_loader.resolve_buy_level (levels, sl_cfg_idx["variant_c_buy_level"])
            slc_sell_lvl = config_loader.resolve_sell_level(levels, sl_cfg_idx["variant_c_sell_level"])
            ce_target_lvl = (levels.get("buy")  or {}).get(cfg_target["ce_level"])
            pe_target_lvl = (levels.get("sell") or {}).get(cfg_target["pe_level"])
            prev_spot = _paper_state["options_last_spot"].get(idx_name)

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
                )
                if reason and opt_ltp is not None:
                    _paper_execute_exit(open_t, float(opt_ltp), reason,
                                        spot=spot)
                    _paper_state["options_last_spot"][idx_name] = spot
                    continue

            # Per-index gate (paper engine). Exits above already ran;
            # block only new entries when this index is unticked.
            if not config_loader.index_enabled_for("paper", idx_name):
                _paper_state["options_last_spot"][idx_name] = spot
                continue

            # ---- ENTRY check ----
            if (idx_name not in open_by_underlying
                    and _paper_can_open_more(idx_name)):
                already_evaluated_open = (
                    _paper_state["options_open_evaluated"].get(idx_name)
                        == today
                    or paper_counts.get(idx_name, 0) > 0
                )
                option_type, stamp_now = _compute_entry_signal(
                    idx_name, spot, prev_spot, levels, cfg_full,
                    already_evaluated_open=already_evaluated_open,
                )
                if stamp_now:
                    _paper_state["options_open_evaluated"][idx_name] = today

                if option_type:
                    opt_key = f"{idx_name} {atm} {option_type}"
                    opt_q = option_data.get(opt_key)
                    opt_ltp = (opt_q or {}).get("ltp")
                    if opt_ltp is not None:
                        _paper_state["options_open_evaluated"][idx_name] = today
                        cfg = INDEX_OPTIONS_CONFIG.get(idx_name) or {}
                        lot_size = cfg.get("lot_size") or 0
                        qty = lot_size * config_loader.lot_multiplier(idx_name)
                        trading_symbol = _option_trading_symbol(
                            idx_name, atm, option_type, m.get("expiry"))
                        entry_reason = _derive_option_entry_reason(
                            option_type, already_evaluated_open,
                            cfg_full["entry"])
                        row = {
                            "date": today,
                            "scrip": opt_key,
                            "option_key": opt_key,
                            "asset_type": "option",
                            "underlying": idx_name,
                            "strike": atm,
                            "option_type": option_type,
                            "expiry": (str(m.get("expiry"))
                                       if m.get("expiry") else None),
                            "trading_symbol": trading_symbol,
                            # Stash WS token+exchange so the live API can
                            # read _feed.get() directly even after this
                            # strike drifts out of the ATM window.
                            "instrument_token":   (opt_q or {}).get("token"),
                            "exchange_segment":   (opt_q or {}).get("exchange"),
                            "order_type": "BUY",
                            "entry_time": now.strftime("%H:%M:%S"),
                            "entry_ts": now.timestamp(),
                            "entry_price": round(float(opt_ltp), 2),
                            "qty": qty,
                            "trigger_spot": round(float(spot), 2),
                            "trigger_level": ("BUY" if option_type == "CE"
                                              else "SELL"),
                            "entry_reason": entry_reason,
                            "max_min_target_price": round(float(opt_ltp), 2),
                            "target_level_reached": None,
                            "exit_time": None, "exit_ts": None,
                            "exit_price": None, "exit_reason": None,
                            "pnl_points": None, "pnl_pct": None,
                            "duration_seconds": None,
                            "auto": True,
                        }
                        _paper_execute_entry(row)
                        paper_counts[idx_name] = paper_counts.get(idx_name, 0) + 1

            _paper_state["options_last_spot"][idx_name] = spot


def paper_futures_tick(future_data, gann_quotes):
    """Paper analogue of future_auto_strategy_tick. See above."""
    from backend.strategy.futures import (
        _compute_futures_entry_signal, _check_futures_exit_reason,
        _round_for_buy, _round_for_sell,
    )

    if not future_data:
        return
    if not config_loader.paper_futures_enabled():
        return

    now = now_ist()

    with _paper_state["futures_lock"]:
        rows = read_paper_ledger()
        today = now.strftime("%Y-%m-%d")

        if _auto_at_or_after_squareoff(now):
            for t in rows:
                if (t.get("status") != "OPEN"
                        or t.get("asset_type") != "future"):
                    continue
                idx_name = t.get("underlying")
                fut = future_data.get(idx_name)
                if not fut or fut.get("ltp") is None:
                    continue
                # Look up spot from gann_quotes for this underlying
                # so the exit row records spot at exit.
                so_key = (INDEX_OPTIONS_CONFIG.get(idx_name)
                          or {}).get("spot_symbol_key")
                so_val = ((gann_quotes.get(so_key) or {}).get("ltp")
                          if so_key else None)
                _paper_execute_exit(t, float(fut["ltp"]), "AUTO_SQUARE_OFF",
                                    spot=so_val)
            return

        if not _auto_in_hours(now):
            return

        paper_counts = {}
        for r in rows:
            if (r.get("asset_type") == "future"
                    and r.get("date") == today):
                u = r.get("underlying")
                if u:
                    paper_counts[u] = paper_counts.get(u, 0) + 1
        open_by_underlying = {
            t["underlying"]: t for t in rows
            if t.get("status") == "OPEN" and t.get("asset_type") == "future"
        }

        def _paper_can_open_more(idx_name):
            cap = config_loader.per_day_cap(idx_name)
            if cap is None:
                return True
            return paper_counts.get(idx_name, 0) < cap

        cfg = config_loader.get()
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
            slc_buy_lvl  = config_loader.resolve_buy_level (levels, sl_cfg_idx["variant_c_buy_level"])
            slc_sell_lvl = config_loader.resolve_sell_level(levels, sl_cfg_idx["variant_c_sell_level"])
            long_target_lvl  = (levels.get("buy")  or {}).get(target_cfg["ce_level"])
            short_target_lvl = (levels.get("sell") or {}).get(target_cfg["pe_level"])
            prev_spot = _paper_state["futures_last_spot"].get(idx_name)
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
                    _paper_execute_exit(open_t, float(fut_ltp), reason,
                                        spot=spot)
                    _paper_state["futures_last_spot"][idx_name] = spot
                    continue

            # Per-index gate (paper engine). Block only new entries.
            if not config_loader.index_enabled_for("paper", idx_name):
                _paper_state["futures_last_spot"][idx_name] = spot
                continue

            # ---- ENTRY check ----
            if (idx_name not in open_by_underlying
                    and _paper_can_open_more(idx_name)):
                already_evaluated_open = (
                    _paper_state["futures_open_evaluated"].get(idx_name)
                        == today
                    or paper_counts.get(idx_name, 0) > 0
                )
                side, stamp_now = _compute_futures_entry_signal(
                    idx_name, spot, prev_spot, levels, cfg,
                    already_evaluated_open=already_evaluated_open,
                )
                if stamp_now:
                    _paper_state["futures_open_evaluated"][idx_name] = today

                if side and fut_ltp is not None:
                    _paper_state["futures_open_evaluated"][idx_name] = today
                    sdk_lot_size = fut.get("lot_size") or 0
                    qty = int(sdk_lot_size) * config_loader.lot_multiplier(idx_name)
                    step = config_loader.futures_round_step(idx_name)
                    if side == "BUY":
                        limit_price = _round_for_buy(fut_ltp, step)
                    else:
                        limit_price = _round_for_sell(fut_ltp, step)
                    entry_reason = _derive_futures_entry_reason(
                        side, already_evaluated_open, cfg["entry"])
                    row = {
                        "date": today,
                        "scrip": f"{idx_name} FUT",
                        "option_key": None,
                        "asset_type": "future",
                        "underlying": idx_name,
                        "strike": None,
                        "option_type": None,
                        "expiry": (str(fut.get("expiry"))
                                   if fut.get("expiry") else None),
                        "trading_symbol": fut.get("trading_symbol"),
                        # Future cache already carries token+exchange.
                        "instrument_token":   fut.get("token"),
                        "exchange_segment":   fut.get("exchange"),
                        "order_type": side,
                        "entry_time": now.strftime("%H:%M:%S"),
                        "entry_ts": now.timestamp(),
                        "entry_price": round(float(limit_price), 2),
                        "qty": qty,
                        "trigger_spot": round(spot, 2),
                        "trigger_level": ("BUY" if side == "BUY"
                                          else "SELL"),
                        "entry_reason": entry_reason,
                        "max_min_target_price": round(float(limit_price), 2),
                        "target_level_reached": None,
                        "exit_time": None, "exit_ts": None,
                        "exit_price": None, "exit_reason": None,
                        "pnl_points": None, "pnl_pct": None,
                        "duration_seconds": None,
                        "auto": True,
                    }
                    _paper_execute_entry(row)
                    paper_counts[idx_name] = paper_counts.get(idx_name, 0) + 1

            _paper_state["futures_last_spot"][idx_name] = spot

"""Tests for stoploss variant D — trailing along the Gann ladder.

Spec: docs/superpowers/specs/2026-04-27-trailing-paper-l5-design.md
(Phase 3).
"""
from datetime import datetime
from unittest.mock import patch

import pytest

from backend.utils import IST


# ---- shared fixtures / helpers ----

@pytest.fixture
def isolated_trade_ledger(tmp_path, monkeypatch):
    """Point both LIVE and PAPER ledgers at temp files.
    update_open_trades_mfe walks both — leaving either pointing at the
    real on-disk file would corrupt production data during tests."""
    from backend.storage import trades as tr
    from backend.storage import paper_ledger as pl
    fake_live = tmp_path / "trade_ledger.json"
    fake_paper = tmp_path / "paper_ledger.json"
    monkeypatch.setattr(tr, "LEDGER_FILE", str(fake_live))
    monkeypatch.setattr(pl, "LEDGER_FILE", str(fake_paper))
    # The strategy.common module re-imports the read/write helpers at
    # module load time, so patch those bindings too.
    from backend.strategy import common as cm
    monkeypatch.setattr(cm, "read_trade_ledger",
                        lambda: tr.read_json(str(fake_live), []))
    monkeypatch.setattr(cm, "write_trade_ledger",
                        lambda rows: tr.atomic_write_json(str(fake_live), rows))
    monkeypatch.setattr(cm, "read_paper_ledger",
                        lambda: pl.read_json(str(fake_paper), []))
    monkeypatch.setattr(cm, "write_paper_ledger",
                        lambda rows: pl.atomic_write_json(str(fake_paper), rows))
    return fake_live


def _in_hours():
    """Mon 2026-04-27 10:30 IST — inside the 09:15-15:15 window."""
    return datetime(2026, 4, 27, 10, 30, 0, tzinfo=IST)


def _weekend():
    """Sat 2026-05-02 10:30 IST — weekend, _auto_in_hours -> False."""
    return datetime(2026, 5, 2, 10, 30, 0, tzinfo=IST)


def _cfg_d():
    """Config dict with stoploss variant D and a valid trading window."""
    return {
        "stoploss": {"active": "D"},
        "target":   {"ce_level": "T1", "pe_level": "S1"},
        "timings":  {"market_start": "09:15", "square_off": "15:15"},
    }


def _nifty_levels():
    """Symmetric Gann ladder around 25000 used by the trail tests."""
    return {
        "buy":  {"BUY": 25050.0, "BUY_WA": 25075.0,
                 "T1": 25100.0, "T2": 25150.0, "T3": 25200.0,
                 "T4": 25250.0, "T5": 25300.0},
        "sell": {"SELL": 24950.0, "SELL_WA": 24925.0,
                 "S1": 24900.0, "S2": 24850.0, "S3": 24800.0,
                 "S4": 24750.0, "S5": 24700.0},
    }


def _ce_open_trade():
    """A bullish (CE option) OPEN trade keyed by its option scrip."""
    return {
        "id": "1", "status": "OPEN",
        "asset_type": "option", "option_type": "CE",
        "underlying": "NIFTY", "scrip": "NIFTY 25000 CE",
        "order_type": "BUY", "entry_price": 100.0, "entry_ts": 1000.0,
        "trail_sl_price": None,
    }


def _quotes_with_spot(spot):
    """Quotes dict where the NIFTY spot key carries the ladder."""
    return {"NIFTY 50": {"ltp": spot, "levels": _nifty_levels()}}


# ---- 1. fresh OPEN with no trail must not trigger SL_TRAIL ----

def test_trail_initial_breakeven():
    """A fresh OPEN trade with no trail_sl_price set yet must NOT
    trigger SL_TRAIL — the variant-D branch must None-guard."""
    from backend.strategy.options import _check_exit_reason
    open_t = {
        "option_type": "CE", "entry_price": 100.0,
        "trail_sl_price": None,
    }
    with patch("backend.strategy.options.config_loader.get",
               return_value=_cfg_d()):
        result = _check_exit_reason(open_t, opt_ltp=80.0, spot=24500.0,
                                    buy_lvl=None, sell_lvl=None,
                                    ce_target_lvl=None,
                                    pe_target_lvl=None)
        assert result != "SL_TRAIL"


# ---- 2. ratchets up monotonically with spot crossing higher rungs ----

def test_trail_ratchets_up(isolated_trade_ledger):
    """update_open_trades_mfe must ratchet trail_sl_price upward
    monotonically as spot crosses higher rungs."""
    from backend.storage.trades import write_trade_ledger, read_trade_ledger
    from backend.strategy.common import update_open_trades_mfe

    write_trade_ledger([_ce_open_trade()])

    with patch("backend.strategy.common.config_loader.get",
               return_value=_cfg_d()), \
         patch("backend.strategy.common.now_ist", return_value=_in_hours()):

        # Spot=25080 — past BUY (25050), below BUY_WA (25075 < 25080)
        # actually 25080 > BUY_WA so highest crossed = BUY_WA (idx 1)
        # trail = ladder[0].price = 25050, high_rung = "BUY_WA"
        update_open_trades_mfe(_quotes_with_spot(25080.0))
        rows = read_trade_ledger()
        assert rows[0]["trail_sl_price"] == 25050.0
        assert rows[0]["trail_high_rung"] == "BUY_WA"

        # Spot=25110 — past T1 (25100). highest crossed = T1 (idx 2)
        # trail = ladder[1].price = 25075 (BUY_WA), high_rung = "T1"
        update_open_trades_mfe(_quotes_with_spot(25110.0))
        rows = read_trade_ledger()
        assert rows[0]["trail_sl_price"] == 25075.0
        assert rows[0]["trail_high_rung"] == "T1"

        # Spot=25160 — past T2 (25150). highest crossed = T2 (idx 3)
        # trail = ladder[2].price = 25100 (T1), high_rung = "T2"
        update_open_trades_mfe(_quotes_with_spot(25160.0))
        rows = read_trade_ledger()
        assert rows[0]["trail_sl_price"] == 25100.0
        assert rows[0]["trail_high_rung"] == "T2"


# ---- 3. pullback must not lower the trail ----

def test_trail_does_not_lower(isolated_trade_ledger):
    """A pullback below current rung must NOT lower trail_sl_price."""
    from backend.storage.trades import write_trade_ledger, read_trade_ledger
    from backend.strategy.common import update_open_trades_mfe

    write_trade_ledger([_ce_open_trade()])

    with patch("backend.strategy.common.config_loader.get",
               return_value=_cfg_d()), \
         patch("backend.strategy.common.now_ist", return_value=_in_hours()):

        # Walk it up to T2, trail = 25100.
        update_open_trades_mfe(_quotes_with_spot(25160.0))
        assert read_trade_ledger()[0]["trail_sl_price"] == 25100.0

        # Pullback to 25080 (would compute new_trail=25050) — must NOT
        # lower the existing 25100 trail.
        update_open_trades_mfe(_quotes_with_spot(25080.0))
        assert read_trade_ledger()[0]["trail_sl_price"] == 25100.0


# ---- 4. SL fires on pullback through trail ----

def test_trail_fires_on_pullback():
    """Spot dropping back through trail_sl_price must produce SL_TRAIL."""
    from backend.strategy.options import _check_exit_reason
    open_t = {
        "option_type": "CE", "entry_price": 100.0,
        "trail_sl_price": 25100.0,
    }
    with patch("backend.strategy.options.config_loader.get",
               return_value=_cfg_d()):
        # Spot=25090 < trail 25100 — must fire SL_TRAIL.
        result = _check_exit_reason(open_t, opt_ltp=70.0, spot=25090.0,
                                    buy_lvl=None, sell_lvl=None,
                                    ce_target_lvl=None,
                                    pe_target_lvl=None)
        assert result == "SL_TRAIL"

        # Spot still above trail — must NOT fire.
        result_above = _check_exit_reason(open_t, opt_ltp=130.0,
                                          spot=25120.0,
                                          buy_lvl=None, sell_lvl=None,
                                          ce_target_lvl=None,
                                          pe_target_lvl=None)
        assert result_above != "SL_TRAIL"


# ---- 5. update gated by in-hours ----

def test_trail_gated_by_in_hours(isolated_trade_ledger):
    """update_open_trades_mfe must NOT update trail_sl_price outside
    market hours / weekends."""
    from backend.storage.trades import write_trade_ledger, read_trade_ledger
    from backend.strategy.common import update_open_trades_mfe

    write_trade_ledger([_ce_open_trade()])

    with patch("backend.strategy.common.config_loader.get",
               return_value=_cfg_d()), \
         patch("backend.strategy.common.now_ist", return_value=_weekend()):
        update_open_trades_mfe(_quotes_with_spot(25160.0))

    rows = read_trade_ledger()
    # Trail must remain unset — weekend prints must not arm the SL.
    assert rows[0].get("trail_sl_price") is None


# ---- 6. trail also ratchets on paper-book trades ----

def test_trail_ratchets_on_paper_book(isolated_trade_ledger):
    """update_open_trades_mfe must walk the paper ledger too — the
    point of paper-mode is to validate that the SL behaves correctly
    before flipping the kill switch off."""
    from backend.storage.paper_ledger import (
        write_paper_ledger, read_paper_ledger,
    )
    from backend.strategy.common import update_open_trades_mfe

    write_paper_ledger([_ce_open_trade()])

    with patch("backend.strategy.common.config_loader.get",
               return_value=_cfg_d()), \
         patch("backend.strategy.common.now_ist", return_value=_in_hours()):
        update_open_trades_mfe(_quotes_with_spot(25160.0))

    rows = read_paper_ledger()
    assert rows[0]["trail_sl_price"] == 25100.0
    assert rows[0]["trail_high_rung"] == "T2"


# ---- 7. option-trade trail uses trigger_spot, not option premium ----

def test_trail_breakeven_uses_trigger_spot_for_options():
    """For an option PE trade with spot in the breakeven zone (between
    SELL and SELL_WA → current_idx == 0), the returned trail must be
    `trigger_spot` (a spot level), NOT `entry_price` (option premium).

    Repro of the 2026-04-28 SENSEX 77000 PE bug where trail_sl_price
    was stored as 404.5 (the option premium) and the PE exit check
    `spot >= trail` fired immediately because 76998 >= 404.5 is
    trivially true.
    """
    from backend.strategy.common import _compute_trail_for_trade
    pe_trade = {
        "asset_type": "option", "option_type": "PE",
        "entry_price": 404.5,         # option premium (NOT spot)
        "trigger_spot": 76984.2,      # spot at entry
    }
    levels = {
        "buy":  {"BUY": 77100.0, "BUY_WA": 77050.0, "T1": 77000.0,
                 "T2": 76950.0, "T3": 76900.0, "T4": 76850.0, "T5": 76800.0},
        "sell": {"SELL": 77020.0, "SELL_WA": 76990.0,
                 "S1": 76950.0, "S2": 76900.0, "S3": 76850.0,
                 "S4": 76800.0, "S5": 76750.0},
    }
    # Spot 77000 — below SELL (77020), above SELL_WA (76990).
    # PE ladder: spot <= SELL only → current_idx == 0 (breakeven case).
    trail, rung = _compute_trail_for_trade(pe_trade, 77000.0, levels)
    assert trail == 76984.2, (
        f"option trail must be trigger_spot (76984.2), not option "
        f"premium (404.5); got {trail}"
    )
    assert rung == "SELL"


def test_trail_breakeven_uses_entry_price_for_futures():
    """Futures keep using entry_price — entry_price for a future IS
    approximately spot, so it's a valid breakeven level. This is the
    regression guard: the option-specific fix must not change futures
    behavior."""
    from backend.strategy.common import _compute_trail_for_trade
    fut_trade = {
        "asset_type": "future", "order_type": "BUY",
        "entry_price": 25080.0,       # future price ≈ spot
        "trigger_spot": 25081.0,
    }
    levels = {
        "buy":  {"BUY": 25050.0, "BUY_WA": 25075.0,
                 "T1": 25100.0, "T2": 25150.0, "T3": 25200.0,
                 "T4": 25250.0, "T5": 25300.0},
        "sell": {"SELL": 24950.0, "SELL_WA": 24925.0,
                 "S1": 24900.0, "S2": 24850.0, "S3": 24800.0,
                 "S4": 24750.0, "S5": 24700.0},
    }
    # Spot 25060 — above BUY (25050), below BUY_WA (25075).
    # current_idx == 0 → breakeven case.
    trail, rung = _compute_trail_for_trade(fut_trade, 25060.0, levels)
    assert trail == 25080.0, "futures must still use entry_price"
    assert rung == "BUY"


# ---- 8. A/B/C variants unchanged regression ----

def test_abc_variants_unchanged():
    """Variants A, B, C must produce identical exit decisions to the
    pre-Phase-3 build on a fixed fixture. Manually trace each variant
    to derive expected reasons.
    """
    from backend.strategy.options import _check_exit_reason

    # ---- Variant A: fixed ₹ premium drop ----
    cfg_a = {"stoploss": {"active": "A", "variant_a_drop_rs": 30.0},
             "target":   {"ce_level": "T1", "pe_level": "S1"}}
    open_ce = {"option_type": "CE", "entry_price": 100.0,
               "trail_sl_price": None}
    with patch("backend.strategy.options.config_loader.get",
               return_value=cfg_a):
        # opt_ltp 70 == entry-30 → fires SL_PREMIUM_FIXED.
        assert _check_exit_reason(open_ce, opt_ltp=70.0, spot=25000.0,
                                  buy_lvl=None, sell_lvl=None,
                                  ce_target_lvl=None,
                                  pe_target_lvl=None) == "SL_PREMIUM_FIXED"
        # opt_ltp 80 > entry-30 → no SL, no target → None.
        assert _check_exit_reason(open_ce, opt_ltp=80.0, spot=25000.0,
                                  buy_lvl=None, sell_lvl=None,
                                  ce_target_lvl=None,
                                  pe_target_lvl=None) is None

    # ---- Variant B: percentage premium drop ----
    cfg_b = {"stoploss": {"active": "B", "variant_b_drop_pct": 30.0},
             "target":   {"ce_level": "T1", "pe_level": "S1"}}
    with patch("backend.strategy.options.config_loader.get",
               return_value=cfg_b):
        # opt_ltp 70 == entry*0.7 → fires SL_PREMIUM_PCT.
        assert _check_exit_reason(open_ce, opt_ltp=70.0, spot=25000.0,
                                  buy_lvl=None, sell_lvl=None,
                                  ce_target_lvl=None,
                                  pe_target_lvl=None) == "SL_PREMIUM_PCT"
        # opt_ltp 80 > 70 → no fire.
        assert _check_exit_reason(open_ce, opt_ltp=80.0, spot=25000.0,
                                  buy_lvl=None, sell_lvl=None,
                                  ce_target_lvl=None,
                                  pe_target_lvl=None) is None

    # ---- Variant C: spot reverses through opposite Gann level ----
    cfg_c = {"stoploss": {"active": "C"},
             "target":   {"ce_level": "T1", "pe_level": "S1"}}
    open_pe = {"option_type": "PE", "entry_price": 100.0,
               "trail_sl_price": None}
    with patch("backend.strategy.options.config_loader.get",
               return_value=cfg_c):
        # CE: spot 24940 < sell_lvl 24950 → SL_SELL_LVL.
        assert _check_exit_reason(open_ce, opt_ltp=80.0, spot=24940.0,
                                  buy_lvl=25050.0, sell_lvl=24950.0,
                                  ce_target_lvl=None,
                                  pe_target_lvl=None) == "SL_SELL_LVL"
        # PE: spot 25060 > buy_lvl 25050 → SL_BUY_LVL.
        assert _check_exit_reason(open_pe, opt_ltp=80.0, spot=25060.0,
                                  buy_lvl=25050.0, sell_lvl=24950.0,
                                  ce_target_lvl=None,
                                  pe_target_lvl=None) == "SL_BUY_LVL"
        # CE: spot 25000 between levels — no SL, no target → None.
        assert _check_exit_reason(open_ce, opt_ltp=80.0, spot=25000.0,
                                  buy_lvl=25050.0, sell_lvl=24950.0,
                                  ce_target_lvl=None,
                                  pe_target_lvl=None) is None

    # ---- Profit target still resolves under variant A (regression) ----
    with patch("backend.strategy.options.config_loader.get",
               return_value=cfg_a):
        # opt_ltp 90 above SL but spot reaches ce_target_lvl → TARGET_T1.
        assert _check_exit_reason(open_ce, opt_ltp=90.0, spot=25100.0,
                                  buy_lvl=None, sell_lvl=None,
                                  ce_target_lvl=25100.0,
                                  pe_target_lvl=None) == "TARGET_T1"


# ---- 9. engine-aware: reverse-engine row walks rev_levels ladder ----

def test_trail_engine_aware_reverse_uses_rev_levels(isolated_trade_ledger):
    """A reverse-engine OPEN row must trail along the `rev_levels`
    ladder (low/high-anchored) — NOT the current-engine `levels`
    (open-anchored) ladder. Also proves trail-active is decided per
    engine: current on variant C (no trail) must not block reverse on
    variant D (trail active).

    Phase 3d regression guard: the previous _apply_mfe_and_trail walked
    `q.get("levels")` for every row and read trail_active from a single
    config slice, so a reverse-engine row would silently track the
    wrong ladder while a current-engine variant-A/B/C config disabled
    its trail entirely.
    """
    from backend.storage.trades import write_trade_ledger, read_trade_ledger
    from backend.strategy.common import update_open_trades_mfe

    rev_trade = _ce_open_trade()
    rev_trade["engine"] = "reverse"
    write_trade_ledger([rev_trade])

    # Distinct ladders so the assertion proves WHICH was walked.
    # rev ladder is offset +1000 from the current ladder.
    levels_current = _nifty_levels()                 # centered at 25000
    levels_reverse = {                               # centered at 26000
        "buy":  {"BUY": 26050.0, "BUY_WA": 26075.0,
                 "T1": 26100.0, "T2": 26150.0, "T3": 26200.0,
                 "T4": 26250.0, "T5": 26300.0},
        "sell": {"SELL": 25950.0, "SELL_WA": 25925.0,
                 "S1": 25900.0, "S2": 25850.0, "S3": 25800.0,
                 "S4": 25750.0, "S5": 25700.0},
    }
    quotes = {"NIFTY 50": {"ltp": 26110.0,
                           "levels":     levels_current,
                           "rev_levels": levels_reverse}}

    # Current engine on variant C (no trail), reverse engine on variant
    # D (trail active). engine_block synthesises 'current' from
    # top-level keys and pulls 'reverse' from reverse_engine sub-dict.
    cfg_full = {
        "stoploss": {"active": "C"},
        "target":   {"ce_level": "T1", "pe_level": "S1"},
        "timings":  {"market_start": "09:15", "square_off": "15:15"},
        "reverse_engine": {
            "stoploss": {"active": "D"},
            "target":   {"ce_level": "T1", "pe_level": "S1"},
        },
    }

    with patch("backend.strategy.common.config_loader.get",
               return_value=cfg_full), \
         patch("backend.strategy.common.now_ist", return_value=_in_hours()):
        update_open_trades_mfe(quotes)

    rows = read_trade_ledger()
    # Spot 26110 on rev ladder: highest rung where 26110 >= price is T1
    # (idx 2, price 26100). trail = ladder[idx-1].price = BUY_WA = 26075.
    # If the buggy code walked `levels` instead, spot 26110 is past T5
    # (25300) so trail would have been 25250 (T4) — distinct value.
    assert rows[0]["trail_sl_price"] == 26075.0, (
        f"reverse-engine trail must walk rev_levels (BUY_WA=26075), "
        f"got {rows[0].get('trail_sl_price')} — likely walked the "
        f"current-engine `levels` ladder instead"
    )
    assert rows[0]["trail_high_rung"] == "T1"


def test_trail_engine_aware_current_unaffected_by_reverse_variant(
        isolated_trade_ledger):
    """Mirror guard: a current-engine row must keep walking `levels`
    even when reverse_engine.stoploss.active is set to D. Proves the
    levels_key choice is per-row, not global."""
    from backend.storage.trades import write_trade_ledger, read_trade_ledger
    from backend.strategy.common import update_open_trades_mfe

    cur_trade = _ce_open_trade()  # legacy / current — no engine field
    write_trade_ledger([cur_trade])

    levels_current = _nifty_levels()
    levels_reverse = {
        "buy":  {"BUY": 26050.0, "BUY_WA": 26075.0,
                 "T1": 26100.0, "T2": 26150.0, "T3": 26200.0,
                 "T4": 26250.0, "T5": 26300.0},
        "sell": {"SELL": 25950.0, "SELL_WA": 25925.0,
                 "S1": 25900.0, "S2": 25850.0, "S3": 25800.0,
                 "S4": 25750.0, "S5": 25700.0},
    }
    # Spot inside the current ladder, well below the rev ladder.
    quotes = {"NIFTY 50": {"ltp": 25160.0,
                           "levels":     levels_current,
                           "rev_levels": levels_reverse}}

    cfg_full = {
        "stoploss": {"active": "D"},
        "target":   {"ce_level": "T1", "pe_level": "S1"},
        "timings":  {"market_start": "09:15", "square_off": "15:15"},
        "reverse_engine": {
            "stoploss": {"active": "D"},
            "target":   {"ce_level": "T1", "pe_level": "S1"},
        },
    }

    with patch("backend.strategy.common.config_loader.get",
               return_value=cfg_full), \
         patch("backend.strategy.common.now_ist", return_value=_in_hours()):
        update_open_trades_mfe(quotes)

    rows = read_trade_ledger()
    # Spot 25160 on current ladder: highest crossed = T2 (idx 3, 25150).
    # trail = ladder[2].price = T1 = 25100.
    assert rows[0]["trail_sl_price"] == 25100.0
    assert rows[0]["trail_high_rung"] == "T2"

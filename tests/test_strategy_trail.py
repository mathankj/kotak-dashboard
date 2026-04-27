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
    """Point trades.LEDGER_FILE at a temp file. update_open_trades_mfe
    reads/writes via this module, so monkeypatching here isolates the
    test from production data."""
    from backend.storage import trades as tr
    fake = tmp_path / "trade_ledger.json"
    monkeypatch.setattr(tr, "LEDGER_FILE", str(fake))
    # The strategy.common module re-imports read_/write_trade_ledger
    # at module load time, so patch those bindings too.
    from backend.strategy import common as cm
    monkeypatch.setattr(cm, "read_trade_ledger",
                        lambda: tr.read_json(str(fake), []))
    monkeypatch.setattr(cm, "write_trade_ledger",
                        lambda rows: tr.atomic_write_json(str(fake), rows))
    return fake


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


# ---- 6. A/B/C variants unchanged regression ----

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

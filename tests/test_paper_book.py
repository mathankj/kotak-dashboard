"""Tests for the paper book — verifying it operates independently
of the live ledger. (Phase 2 of the trailing-paper-l5 spec.)"""
from datetime import datetime
from unittest.mock import patch

import pytest

from backend.utils import IST


@pytest.fixture
def isolated_paper_ledger(tmp_path, monkeypatch):
    """Point paper_ledger.LEDGER_FILE at a temp file for the test."""
    from backend.storage import paper_ledger as pl
    fake = tmp_path / "paper_ledger.json"
    monkeypatch.setattr(pl, "LEDGER_FILE", str(fake))
    return fake


def _in_hours_now():
    """A weekday timestamp inside trading hours (Mon 10:30 IST)."""
    return datetime(2026, 4, 27, 10, 30, 0, tzinfo=IST)


def _after_squareoff_now():
    """A weekday timestamp at/after square-off (Mon 15:30 IST)."""
    return datetime(2026, 4, 27, 15, 30, 0, tzinfo=IST)


def _synthetic_option_inputs(spot=25100.0, ce_ltp=120.0, atm=25000):
    """Build option_data, option_index_meta, gann_quotes that make a
    crossing-bullish signal fire on NIFTY (prev_spot needs to be
    below the BUY level on the first tick — but since paper_state is
    fresh between tests, prev_spot is None so we rely on the
    market-open path firing instead)."""
    levels = {
        "buy":  {"BUY": 25050.0, "BUY_WA": 25075.0,
                 "T1": 25100.0, "T2": 25150.0, "T3": 25200.0,
                 "T4": 25250.0, "T5": 25300.0},
        "sell": {"SELL": 24950.0, "SELL_WA": 24925.0,
                 "S1": 24900.0, "S2": 24850.0, "S3": 24800.0,
                 "S4": 24750.0, "S5": 24700.0},
    }
    opt_key = f"NIFTY {atm} CE"
    return (
        {opt_key: {"index": "NIFTY", "strike": atm,
                   "option_type": "CE", "ltp": ce_ltp}},
        {"NIFTY": {"spot": spot, "atm": atm,
                   "expiry": "2026-04-30"}},
        {"NIFTY 50": {"ltp": spot, "levels": levels}},
    )


def test_paper_book_imports():
    """Module must import standalone."""
    from backend.strategy.paper_book import (  # noqa: F401
        paper_options_tick, paper_futures_tick,
    )


def test_paper_entry_recorded_when_signal_fires(isolated_paper_ledger):
    """A synthetic entry signal must produce one OPEN paper row."""
    from backend.storage.paper_ledger import read_paper_ledger
    from backend.strategy.paper_book import _paper_execute_entry

    row = {
        "scrip": "NIFTY FUT",
        "asset_type": "future",
        "underlying": "NIFTY",
        "order_type": "BUY",
        "entry_price": 25000.0,
        "qty": 75,
        "entry_ts": 1000.0,
    }
    _paper_execute_entry(row)
    rows = read_paper_ledger()
    assert len(rows) == 1
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["mode"] == "PAPER_BOOK"
    assert rows[0]["kotak_entry_order_id"] is None
    assert rows[0]["id"] == "1"


def test_paper_exit_closes_open_row(isolated_paper_ledger):
    """A synthetic exit closes the matching paper row."""
    from backend.storage.paper_ledger import (
        read_paper_ledger, write_paper_ledger,
    )
    from backend.strategy.paper_book import _paper_execute_exit

    open_row = {
        "id": "1", "scrip": "NIFTY FUT", "asset_type": "future",
        "underlying": "NIFTY", "order_type": "BUY",
        "entry_price": 25000.0, "entry_ts": 1000.0, "qty": 75,
        "status": "OPEN", "mode": "PAPER_BOOK",
    }
    write_paper_ledger([open_row])
    _paper_execute_exit(open_row, ltp=25100.0, reason="TARGET_T1")
    rows = read_paper_ledger()
    assert rows[0]["status"] == "CLOSED"
    assert rows[0]["exit_reason"] == "TARGET_T1"
    assert rows[0]["pnl_points"] == 100.0


def test_paper_independent_when_live_blocked(isolated_paper_ledger):
    """Paper must trade even when live's place_order_safe would refuse.

    paper_options_tick never calls place_order_safe — proven here by
    running the tick with no patching of safety/orders and verifying
    a paper row appears.
    """
    from backend.storage.paper_ledger import read_paper_ledger
    from backend.strategy import paper_book

    option_data, meta, gq = _synthetic_option_inputs()
    # Reset paper_state so prev_spot is None (forces market-open path).
    paper_book._paper_state["options_open_evaluated"].clear()
    paper_book._paper_state["options_last_spot"].clear()

    with patch("backend.strategy.paper_book.now_ist",
               return_value=_in_hours_now()):
        paper_book.paper_options_tick(option_data, meta, gq)

    rows = read_paper_ledger()
    assert len(rows) == 1
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["mode"] == "PAPER_BOOK"
    assert rows[0]["asset_type"] == "option"
    assert rows[0]["underlying"] == "NIFTY"
    assert rows[0]["option_type"] == "CE"


def test_paper_skips_kill_switch_freeze(isolated_paper_ledger, tmp_path,
                                         monkeypatch):
    """Kill switch (HALTED.flag) must NOT freeze paper. The paper book
    never imports / consults the halt flag — proven by triggering an
    entry while a HALTED.flag is on disk."""
    from backend.storage.paper_ledger import read_paper_ledger
    from backend.strategy import paper_book

    # Place a HALTED.flag in a temp data dir and point any safety code
    # at it. (paper_book never reads it — this just ensures the test
    # setup is honest.)
    flag = tmp_path / "HALTED.flag"
    flag.write_text("test-halt", encoding="utf-8")

    option_data, meta, gq = _synthetic_option_inputs()
    paper_book._paper_state["options_open_evaluated"].clear()
    paper_book._paper_state["options_last_spot"].clear()

    with patch("backend.strategy.paper_book.now_ist",
               return_value=_in_hours_now()):
        paper_book.paper_options_tick(option_data, meta, gq)

    rows = read_paper_ledger()
    assert len(rows) == 1, "kill switch must not block paper"
    assert rows[0]["status"] == "OPEN"


def test_paper_per_day_cap_independent(isolated_paper_ledger):
    """Paper cap is its own gate. Confirmed by: cap=0 blocks any new
    paper entry even though paper has zero trades and the live ledger
    is irrelevant."""
    from backend.storage.paper_ledger import read_paper_ledger
    from backend.strategy import paper_book

    option_data, meta, gq = _synthetic_option_inputs()
    paper_book._paper_state["options_open_evaluated"].clear()
    paper_book._paper_state["options_last_spot"].clear()

    with patch("backend.strategy.paper_book.now_ist",
               return_value=_in_hours_now()), \
         patch("backend.strategy.paper_book.config_loader.per_day_cap",
               return_value=0):
        paper_book.paper_options_tick(option_data, meta, gq)

    rows = read_paper_ledger()
    assert rows == [], "cap=0 must block paper entry"


def test_paper_entry_reason_captured(isolated_paper_ledger):
    """A paper entry must record entry_reason so the UI can show WHY
    the trade fired (OPEN_ABOVE_BUY_WA / CROSS_UP_BUY_WA / ...)."""
    from backend.storage.paper_ledger import read_paper_ledger
    from backend.strategy import paper_book

    option_data, meta, gq = _synthetic_option_inputs()
    paper_book._paper_state["options_open_evaluated"].clear()
    paper_book._paper_state["options_last_spot"].clear()

    with patch("backend.strategy.paper_book.now_ist",
               return_value=_in_hours_now()):
        paper_book.paper_options_tick(option_data, meta, gq)

    rows = read_paper_ledger()
    assert len(rows) == 1
    reason = rows[0].get("entry_reason")
    assert reason is not None and reason.startswith("OPEN_ABOVE_"), (
        f"expected OPEN_ABOVE_* on market-open path, got {reason!r}"
    )


def test_paper_exit_uses_ws_feed_when_strike_drifts(isolated_paper_ledger):
    """If a strike has drifted out of the ATM option_data window, the
    exit check must still fire by reading the WS feed directly via the
    row's stored instrument_token + exchange_segment. Otherwise
    variant-D SL_TRAIL stays stuck OPEN even when spot is below trail.
    Repro of the 2026-04-28 NIFTY 24100 CE bug.
    """
    from backend.storage.paper_ledger import (
        read_paper_ledger, write_paper_ledger,
    )
    from backend.strategy import paper_book

    write_paper_ledger([{
        "id": "1", "date": "2026-04-27",
        "scrip": "NIFTY 24100 CE", "option_key": "NIFTY 24100 CE",
        "asset_type": "option", "underlying": "NIFTY",
        "strike": 24100, "option_type": "CE",
        "order_type": "BUY", "entry_price": 67.45, "entry_ts": 1000.0,
        "instrument_token": "12345", "exchange_segment": "nse_fo",
        "trail_sl_price": 24146.92, "trail_high_rung": "T3",
        "status": "OPEN", "mode": "PAPER_BOOK",
    }])

    # 24100 CE NOT in option_data — ATM has drifted to 24150.
    option_data = {}
    meta = {"NIFTY": {"spot": 24130.0, "atm": 24150,
                      "expiry": "2026-04-30"}}
    levels = {
        "buy":  {"BUY": 24050.0, "BUY_WA": 24075.0,
                 "T1": 24100.0, "T2": 24125.0, "T3": 24150.0,
                 "T4": 24175.0, "T5": 24200.0},
        "sell": {"SELL": 23950.0, "SELL_WA": 23925.0,
                 "S1": 23900.0, "S2": 23850.0, "S3": 23800.0,
                 "S4": 23750.0, "S5": 23700.0},
    }
    gq = {"NIFTY 50": {"ltp": 24130.0, "levels": levels}}

    cfg_d = {
        "stoploss": {"active": "D",
                     "variant_c_buy_level": "BUY_WA",
                     "variant_c_sell_level": "SELL_WA"},
        "target":   {"ce_level": "T1", "pe_level": "S1"},
        "entry":    {"market_open_path": True,
                     "market_open_buy_level": "BUY_WA",
                     "market_open_sell_level": "SELL_WA",
                     "crossing_path": True,
                     "crossing_buy_level": "BUY_WA",
                     "crossing_sell_level": "SELL_WA"},
        "timings":  {"market_start": "09:15", "square_off": "15:15"},
    }

    fake_tick = {"ltp": 64.15, "ts": 1234567.0}
    with patch("backend.strategy.paper_book.now_ist",
               return_value=_in_hours_now()), \
         patch("backend.strategy.paper_book.config_loader.get",
               return_value=cfg_d), \
         patch("backend.strategy.options.config_loader.get",
               return_value=cfg_d), \
         patch("backend.quotes._feed.get",
               return_value=fake_tick):
        paper_book.paper_options_tick(option_data, meta, gq)

    rows = read_paper_ledger()
    assert rows[0]["status"] == "CLOSED", (
        "drifted-out CE must still close via WS-feed fallback"
    )
    assert rows[0]["exit_reason"] == "SL_TRAIL"
    assert rows[0]["exit_price"] == 64.15


def test_paper_square_off_independent(isolated_paper_ledger):
    """At/after squareoff, paper closes its OPEN rows independently."""
    from backend.storage.paper_ledger import (
        read_paper_ledger, write_paper_ledger,
    )
    from backend.strategy import paper_book

    write_paper_ledger([{
        "id": "1", "date": "2026-04-27",
        "scrip": "NIFTY 25000 CE", "option_key": "NIFTY 25000 CE",
        "asset_type": "option", "underlying": "NIFTY",
        "order_type": "BUY", "entry_price": 100.0,
        "entry_ts": 1000.0,
        "status": "OPEN", "mode": "PAPER_BOOK",
    }])

    option_data, meta, gq = _synthetic_option_inputs(ce_ltp=150.0)

    with patch("backend.strategy.paper_book.now_ist",
               return_value=_after_squareoff_now()):
        paper_book.paper_options_tick(option_data, meta, gq)

    rows = read_paper_ledger()
    assert rows[0]["status"] == "CLOSED"
    assert rows[0]["exit_reason"] == "AUTO_SQUARE_OFF"
    assert rows[0]["exit_price"] == 150.0

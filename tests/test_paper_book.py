"""Tests for the paper book — verifying it operates independently
of the live ledger. (Phase 2 of the trailing-paper-l5 spec.)"""
import pytest


@pytest.fixture
def isolated_paper_ledger(tmp_path, monkeypatch):
    """Point paper_ledger.LEDGER_FILE at a temp file for the test."""
    from backend.storage import paper_ledger as pl
    fake = tmp_path / "paper_ledger.json"
    monkeypatch.setattr(pl, "LEDGER_FILE", str(fake))
    return fake


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
    """If place_order_safe returns BLOCKED, paper still gets an OPEN row.
    This is the user's stated requirement — paper buys even when live
    has zero margin."""
    pytest.xfail("written ahead of paper_options_tick")


def test_paper_skips_kill_switch_freeze(isolated_paper_ledger):
    """Kill switch must NOT freeze paper. Paper continues to trade."""
    pytest.xfail("written ahead of paper_options_tick")


def test_paper_per_day_cap_independent(isolated_paper_ledger):
    """Paper count is independent of live count."""
    pytest.xfail("written ahead of paper_options_tick")


def test_paper_square_off_independent(isolated_paper_ledger):
    """End-of-day square-off closes paper OPENs even if live has none."""
    pytest.xfail("written ahead of paper_options_tick")

"""Smoke tests — prove the app and its modules can be imported.

These run fast (no Kotak network calls) and catch the most common breakage
during refactor: an import that no longer resolves after a file move.

Run with:  python -m pytest tests/ -v
"""


def test_import_app():
    """app.py must import cleanly (Flask app + module-level setup must not crash)."""
    import app  # noqa: F401


def test_import_quote_feed():
    """quote_feed.py must import standalone (no Flask dependency)."""
    from backend.kotak.quote_feed import QuoteFeed  # noqa: F401


def test_import_backend_kotak():
    """All backend.kotak modules must import cleanly."""
    from backend.utils import IST, now_ist  # noqa: F401
    from backend.kotak.client import (  # noqa: F401
        login, ensure_client, safe_call, append_history, read_history,
    )
    from backend.kotak.instruments import (  # noqa: F401
        SCRIPS, find_scrip, INDEX_OPTIONS_CONFIG,
        _fetch_index_fo_universe, _parse_item_strike, _parse_item_expiry_date,
    )


def test_find_scrip():
    """find_scrip should return a SCRIPS entry by symbol or None.
    SCRIPS was trimmed to the three indices that drive the strategy —
    RELIANCE/TCS/etc. are no longer tracked, so we exercise lookup +
    miss against what's actually in the list."""
    from backend.kotak.instruments import find_scrip
    assert find_scrip("NIFTY 50")["exchange"] == "nse_cm"
    assert find_scrip("BANKNIFTY")["token"] == "Nifty Bank"
    assert find_scrip("SENSEX")["exchange"] == "bse_cm"
    assert find_scrip("DOES_NOT_EXIST") is None
    # Equities removed at Ganesh's request — confirm they're absent.
    assert find_scrip("RELIANCE") is None


def test_safe_call_empty_marker():
    """safe_call should treat 'no holdings found' as empty, not error."""
    from backend.kotak.client import safe_call
    fake_no_holdings = lambda: {"error": [{"message": "No Holdings Found"}]}
    data, err = safe_call(fake_no_holdings)
    assert data == []
    assert err is None


def test_safe_call_real_error():
    """safe_call should surface real errors as strings."""
    from backend.kotak.client import safe_call
    fake_err = lambda: {"error": [{"message": "Invalid token"}]}
    data, err = safe_call(fake_err)
    assert data is None
    assert "Invalid token" in err


def test_safe_call_exception():
    """safe_call should catch exceptions and return as error."""
    from backend.kotak.client import safe_call
    def raises():
        raise ValueError("boom")
    data, err = safe_call(raises)
    assert data is None
    assert "ValueError" in err and "boom" in err


def test_quote_feed_constructs():
    """QuoteFeed must instantiate without a real client."""
    from backend.kotak.quote_feed import QuoteFeed
    feed = QuoteFeed(client_provider=lambda: None)
    status = feed.status()
    assert status["connected"] is False
    assert status["subs_index"] == 0
    assert status["cached_keys"] == 0


def test_import_quotes_module():
    """backend.quotes must import standalone (no Flask dependency)."""
    from backend.quotes import (  # noqa: F401
        fetch_quotes, fetch_option_quotes, build_option_chain,
        build_all_option_tokens, _feed,
    )


def test_import_storage_modules():
    """All backend.storage modules must import cleanly."""
    from backend.storage.trades import (  # noqa: F401
        LEDGER_FILE, read_trade_ledger, write_trade_ledger, next_trade_id,
    )
    from backend.storage.paper_ledger import (  # noqa: F401
        LEDGER_FILE as PAPER_LEDGER_FILE,
        read_paper_ledger, write_paper_ledger, next_paper_id,
    )
    from backend.storage.orders import (  # noqa: F401
        ORDERS_FILE, append_order, read_orders,
    )
    from backend.storage.history import (  # noqa: F401
        HISTORY_FILE, append_history, read_history,
    )


def test_next_paper_id_empty_and_increment():
    """next_paper_id mirrors next_trade_id semantics."""
    from backend.storage.paper_ledger import next_paper_id
    assert next_paper_id([]) == "1"
    assert next_paper_id([{"id": "5"}, {"id": "3"}]) == "6"
    assert next_paper_id([{"id": "abc"}, {"id": "2"}]) == "3"


def test_import_strategy_modules():
    """All backend.strategy modules must import cleanly."""
    from backend.strategy.gann import (  # noqa: F401
        GANN_STEP, SELL_LEVELS, BUY_LEVELS, LEVEL_COLORS,
        BUY_LEVEL_ORDER, SELL_LEVEL_ORDER,
        gann_levels, nearest_gann_level, compute_target_level_reached,
    )
    from backend.strategy.common import (  # noqa: F401
        AUTO_HOURS_START, AUTO_HOURS_END,
        _auto_in_hours, _auto_at_or_after_squareoff, _auto_close,
        update_open_trades_mfe,
    )
    from backend.strategy.options import (  # noqa: F401
        AUTO_OPTION_STRATEGY_ENABLED, option_auto_strategy_tick,
    )


def test_gann_levels_math():
    """gann_levels should produce 11 sell + 11 buy levels symmetric around sqrt-space."""
    from backend.strategy.gann import gann_levels
    lv = gann_levels(100.0)
    assert set(lv["sell"].keys()) == {
        "S9", "S8", "S7", "S6", "S5", "S4", "S3", "S2", "S1",
        "SELL_WA", "SELL"}
    assert set(lv["buy"].keys()) == {
        "BUY", "BUY_WA",
        "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"}
    # All sell levels should be <100, all buy levels >100.
    for v in lv["sell"].values():
        assert v < 100
    for v in lv["buy"].values():
        assert v > 100
    # BUY level above SELL level
    assert lv["buy"]["BUY"] > lv["sell"]["SELL"]


def test_gann_levels_zero():
    """gann_levels(0) should return all-None levels (no sqrt of 0)."""
    from backend.strategy.gann import gann_levels
    lv = gann_levels(0)
    assert all(v is None for v in lv["sell"].values())
    assert all(v is None for v in lv["buy"].values())


def test_compute_target_level_reached_buy():
    """For BUY: returns deepest level reached as price climbs."""
    from backend.strategy.gann import gann_levels, compute_target_level_reached
    lv = gann_levels(100.0)
    # Price hasn't reached BUY → None
    assert compute_target_level_reached("B", 100.0, 100.5, lv) is None
    # Reached T1 but not T2
    t1 = lv["buy"]["T1"]; t2 = lv["buy"]["T2"]
    mid = (t1 + t2) / 2
    assert compute_target_level_reached("B", 100.0, mid, lv) == "T1"
    # Beyond T9 (top of the extended ladder, post S9..T9 expansion)
    beyond = lv["buy"]["T9"] + 10
    assert compute_target_level_reached("B", 100.0, beyond, lv) == "Beyond T9"


def test_next_trade_id_empty_and_increment():
    """next_trade_id starts at '1' and increments past max."""
    from backend.storage.trades import next_trade_id
    assert next_trade_id([]) == "1"
    assert next_trade_id([{"id": "5"}, {"id": "3"}]) == "6"
    # Garbled IDs are skipped
    assert next_trade_id([{"id": "abc"}, {"id": "2"}]) == "3"


def test_login_free_pages_render():
    """Pages that don't require Kotak login should render via the file-system
    templates moved out of app.py — proves frontend/templates/ resolves."""
    import app
    client = app.app.test_client()
    for url in ["/history", "/orderlog", "/gann", "/options"]:
        r = client.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code}"
        assert len(r.data) > 100, f"{url} returned only {len(r.data)} bytes"

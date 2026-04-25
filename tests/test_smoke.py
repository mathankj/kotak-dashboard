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
    """find_scrip should return a SCRIPS entry by symbol or None."""
    from backend.kotak.instruments import find_scrip
    assert find_scrip("RELIANCE")["token"] == "2885"
    assert find_scrip("NIFTY 50")["exchange"] == "nse_cm"
    assert find_scrip("DOES_NOT_EXIST") is None


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

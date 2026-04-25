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
    from quote_feed import QuoteFeed  # noqa: F401


def test_quote_feed_constructs():
    """QuoteFeed must instantiate without a real client."""
    from quote_feed import QuoteFeed
    feed = QuoteFeed(client_provider=lambda: None)
    status = feed.status()
    assert status["connected"] is False
    assert status["subs_index"] == 0
    assert status["cached_keys"] == 0

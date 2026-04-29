"""I.1 — entry decision tree unit tests for backend/strategy/options.

Covers `_compute_entry_signal()`, the pure function that decides which
side (CE/PE/None) an open tick should take. Two paths:

  1. market_open_path  — single-shot at start of session: if spot is
     already above BUY → CE, below SELL → PE, in-channel → defer + stamp.
  2. crossing_path     — every subsequent tick: prev_spot crosses BUY
     upward → CE, prev_spot crosses SELL downward → PE.

Plus the same-tick block: once `open_evaluated[idx]` is stamped today,
the function must NEVER take the market-open branch again.

These tests pin the public contract of the decision tree so future
edits to options.py don't silently change which side the bot picks.
"""
from backend.strategy.options import _compute_entry_signal


def _levels():
    """Symmetric Gann ladder around 25000 used by all entry tests."""
    return {
        "buy":  {"BUY": 25050.0, "BUY_WA": 25075.0,
                 "T1": 25100.0, "T2": 25150.0, "T3": 25200.0},
        "sell": {"SELL": 24950.0, "SELL_WA": 24925.0,
                 "S1": 24900.0, "S2": 24850.0, "S3": 24800.0},
    }


def _cfg(market_open=True, crossing=True,
         mo_buy="BUY", mo_sell="SELL",
         cr_buy="BUY", cr_sell="SELL"):
    return {"entry": {
        "market_open_path":        market_open,
        "market_open_buy_level":   mo_buy,
        "market_open_sell_level":  mo_sell,
        "crossing_path":           crossing,
        "crossing_buy_level":      cr_buy,
        "crossing_sell_level":     cr_sell,
    }}


# ---- 1. market_open_path bullish ----

def test_market_open_above_buy_picks_ce():
    """Spot above BUY at session start → CE side, stamp deferred so the
    caller waits for opt_ltp before burning the open-evaluation slot."""
    side, stamp = _compute_entry_signal(
        "NIFTY", spot=25080.0, prev_spot=None, levels=_levels(),
        cfg=_cfg(), already_evaluated_open=False)
    assert side == "CE"
    assert stamp is False  # defer — wait for option quote


# ---- 2. market_open_path bearish ----

def test_market_open_below_sell_picks_pe():
    side, stamp = _compute_entry_signal(
        "NIFTY", spot=24920.0, prev_spot=None, levels=_levels(),
        cfg=_cfg(), already_evaluated_open=False)
    assert side == "PE"
    assert stamp is False


# ---- 3. market_open_path in-channel: no side, stamp NOW ----

def test_market_open_in_channel_stamps_now():
    """In-channel at open → no entry, but stamp now so subsequent ticks
    fall through to the crossing branch (the alternative would re-evaluate
    the open path forever, never letting crossings fire)."""
    side, stamp = _compute_entry_signal(
        "NIFTY", spot=25000.0, prev_spot=None, levels=_levels(),
        cfg=_cfg(), already_evaluated_open=False)
    assert side is None
    assert stamp is True


# ---- 4. market_open_path disabled: stamp NOW so crossings can fire ----

def test_market_open_disabled_stamps_now():
    side, stamp = _compute_entry_signal(
        "NIFTY", spot=25080.0, prev_spot=None, levels=_levels(),
        cfg=_cfg(market_open=False), already_evaluated_open=False)
    assert side is None
    assert stamp is True


# ---- 5. crossing path UP: prev below BUY, current above ----

def test_crossing_up_picks_ce():
    """prev_spot 25040 (below BUY 25050), spot 25060 — up-crossing → CE."""
    side, stamp = _compute_entry_signal(
        "NIFTY", spot=25060.0, prev_spot=25040.0, levels=_levels(),
        cfg=_cfg(), already_evaluated_open=True)
    assert side == "CE"
    assert stamp is False


# ---- 6. crossing path DOWN: prev above SELL, current below ----

def test_crossing_down_picks_pe():
    side, stamp = _compute_entry_signal(
        "NIFTY", spot=24940.0, prev_spot=24960.0, levels=_levels(),
        cfg=_cfg(), already_evaluated_open=True)
    assert side == "PE"
    assert stamp is False


# ---- 7. same-side stay (no crossing) → no signal ----

def test_no_crossing_no_signal():
    """Spot moves but does NOT cross either level → no entry."""
    # Both above BUY — no fresh crossing.
    side, _ = _compute_entry_signal(
        "NIFTY", spot=25090.0, prev_spot=25080.0, levels=_levels(),
        cfg=_cfg(), already_evaluated_open=True)
    assert side is None
    # Both below SELL — no fresh crossing.
    side, _ = _compute_entry_signal(
        "NIFTY", spot=24910.0, prev_spot=24920.0, levels=_levels(),
        cfg=_cfg(), already_evaluated_open=True)
    assert side is None
    # Both in-channel.
    side, _ = _compute_entry_signal(
        "NIFTY", spot=25010.0, prev_spot=24990.0, levels=_levels(),
        cfg=_cfg(), already_evaluated_open=True)
    assert side is None


# ---- 8. crossing_path disabled: no fire even on a real crossing ----

def test_crossing_path_disabled_blocks_entry():
    side, _ = _compute_entry_signal(
        "NIFTY", spot=25060.0, prev_spot=25040.0, levels=_levels(),
        cfg=_cfg(crossing=False), already_evaluated_open=True)
    assert side is None


# ---- 9. same-tick block: already-evaluated state suppresses market-open path ----

def test_already_evaluated_skips_market_open_path():
    """The market-open path must run AT MOST ONCE per session per index.
    Once `open_evaluated` is stamped, even a clean above-BUY spot reading
    must NOT re-fire CE through the market-open branch — only crossings
    can produce new entries from here on."""
    # Above BUY but already evaluated → fall through to crossing branch.
    # No prev_spot supplied, so crossing branch can't fire either.
    side, stamp = _compute_entry_signal(
        "NIFTY", spot=25080.0, prev_spot=None, levels=_levels(),
        cfg=_cfg(), already_evaluated_open=True)
    assert side is None
    assert stamp is False  # not the market-open branch — no re-stamp


# ---- 10. WA variant resolution ----

def test_wa_variants_resolve_through_levels():
    """When config asks for BUY_WA / SELL_WA, the resolver pulls the
    weighted-average rung instead of the regular one."""
    # spot 25060 — between BUY (25050) and BUY_WA (25075).
    # With mo_buy=BUY → spot > 25050 → CE.
    side, _ = _compute_entry_signal(
        "NIFTY", spot=25060.0, prev_spot=None, levels=_levels(),
        cfg=_cfg(mo_buy="BUY"), already_evaluated_open=False)
    assert side == "CE"
    # With mo_buy=BUY_WA → spot 25060 < 25075 → no CE on market-open
    # branch, in-channel → stamp now.
    side, stamp = _compute_entry_signal(
        "NIFTY", spot=25060.0, prev_spot=None, levels=_levels(),
        cfg=_cfg(mo_buy="BUY_WA"), already_evaluated_open=False)
    assert side is None
    assert stamp is True

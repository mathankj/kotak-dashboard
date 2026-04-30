"""Gann Square of 9 levels and helpers.

Pure math + small lookup helpers. No I/O, no network — safe to unit-test.

Levels are computed in sqrt-space stepping by 22.5° = 0.0625:
  S9..S1, SELL_WA, SELL  =  -12..-2  (below open)
  BUY, BUY_WA, T1..T9    =  +2..+12  (above open)

The ladder was extended from 5 to 9 target rungs each side at Ganesh's
request — gives strategy + UI room to stretch farther on trending days
without changing the underlying 22.5° step.
"""
import math


GANN_STEP = 0.0625

SELL_LEVELS = ["S9", "S8", "S7", "S6", "S5", "S4", "S3", "S2", "S1",
               "SELL_WA", "SELL"]
BUY_LEVELS  = ["BUY", "BUY_WA",
               "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"]

# UI presentation only — colors used by the dashboard for level highlighting.
# S6..S9 deepen the existing red ramp; T6..T9 deepen the green ramp.
LEVEL_COLORS = {
    "S9": "#3B0000", "S8": "#580000", "S7": "#6A0000", "S6": "#750000",
    "S5": "#7F0000", "S4": "#8E1818",
    "S3": "#B71C1C", "S2": "#C62828", "S1": "#D32F2F",
    "SELL_WA": "#FF9800", "SELL": "#EF9A9A",
    "BUY": "#A5D6A7", "BUY_WA": "#FF9800",
    "T1": "#81C784", "T2": "#66BB6A", "T3": "#388E3C",
    "T4": "#2E7D32", "T5": "#1B5E20",
    "T6": "#154A18", "T7": "#103810", "T8": "#0B2A0B", "T9": "#061C06",
}

# Order used when computing how deep a trade went in its favoured direction.
BUY_LEVEL_ORDER  = ["BUY", "BUY_WA",
                    "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9"]
SELL_LEVEL_ORDER = ["SELL", "SELL_WA",
                    "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"]


def gann_levels(open_price):
    """Compute Gann Square of 9 levels from the opening price.
    Returns dict {sell: {S9..SELL}, buy: {BUY..T9}}."""
    if not open_price or open_price <= 0:
        return {"sell": {k: None for k in SELL_LEVELS},
                "buy":  {k: None for k in BUY_LEVELS}}
    sq = math.sqrt(open_price)
    sell = {}
    # SELL_LEVELS is ordered S9, S8, ..., S1, SELL_WA, SELL → n = -12..-2.
    for i, name in enumerate(SELL_LEVELS):
        n = -(12 - i)
        sell[name] = round((sq + n * GANN_STEP) ** 2, 2)
    buy = {}
    # BUY_LEVELS is ordered BUY, BUY_WA, T1..T9 → n = +2..+12.
    for i, name in enumerate(BUY_LEVELS):
        n = i + 2
        buy[name] = round((sq + n * GANN_STEP) ** 2, 2)
    return {"sell": sell, "buy": buy}


# ---- Reverse Gann ladders (Phase 1) ----
# Same formula and step as gann_levels(), but anchored to today's intraday
# extremes instead of the opening price. Source of truth: Ganesh's
# Gann.xlsm Strategy sheet — "Rev Buy" block (cols AB-AH) anchored at
# SQRT(low), "Rev Sell" block (cols AJ-AP) anchored at SQRT(high). We reuse
# the existing 0.0625 sqrt-step and BUY/SELL level names so downstream
# config/UI/strategy code that already understands BUY_WA, T1..T5, etc.
# can layer on top without renaming.
def reverse_buy_levels(low_of_day):
    """Buy-side Gann ladder anchored to today's running intraday LOW.
    Returns dict {BUY..T5}. Used as a bounce-target ladder once price has
    dipped below the open. Anchor only moves when a NEW lower low is set
    (stepped — see backend/quotes.py for the running-low tracker)."""
    if not low_of_day or low_of_day <= 0:
        return {k: None for k in BUY_LEVELS}
    sq = math.sqrt(low_of_day)
    out = {}
    for i, name in enumerate(BUY_LEVELS):
        n = i + 2
        out[name] = round((sq + n * GANN_STEP) ** 2, 2)
    return out


def reverse_sell_levels(high_of_day):
    """Sell-side Gann ladder anchored to today's running intraday HIGH.
    Returns dict {S5..SELL}. Used as a rejection-target ladder once price
    has rallied above the open. Anchor only moves when a NEW higher high
    is set (stepped — tracker lives in backend/quotes.py)."""
    if not high_of_day or high_of_day <= 0:
        return {k: None for k in SELL_LEVELS}
    sq = math.sqrt(high_of_day)
    out = {}
    for i, name in enumerate(SELL_LEVELS):
        n = -(8 - i)
        out[name] = round((sq + n * GANN_STEP) ** 2, 2)
    return out


def nearest_gann_level(symbol_data):
    """Return (level_name, distance_pct) of the gann level nearest to LTP.
    Used for LTP box colouring."""
    ltp = symbol_data.get("ltp")
    if not ltp:
        return None, None
    levels = symbol_data.get("levels") or {}
    all_levels = {}
    for k, v in (levels.get("sell") or {}).items():
        if v is not None:
            all_levels[k] = v
    for k, v in (levels.get("buy") or {}).items():
        if v is not None:
            all_levels[k] = v
    if not all_levels:
        return None, None
    best, best_dist = None, None
    for name, px in all_levels.items():
        d = abs(ltp - px) / ltp
        if best_dist is None or d < best_dist:
            best_dist = d
            best = name
    return best, best_dist


def compute_target_level_reached(side, entry_price, max_min_price, levels):
    """Given the best price reached since entry, determine the deepest
    Gann level touched in favour of the position.
    side: 'B' or 'S'; levels: {sell:{}, buy:{}}."""
    buy = (levels or {}).get("buy") or {}
    sell = (levels or {}).get("sell") or {}
    if side == "B":
        reached = None
        for name in BUY_LEVEL_ORDER:
            px = buy.get(name)
            if px is not None and max_min_price >= px:
                reached = name
        t9 = buy.get("T9")
        if t9 is not None and max_min_price > t9:
            reached = "Beyond T9"
        return reached
    else:
        reached = None
        for name in SELL_LEVEL_ORDER:
            px = sell.get(name)
            if px is not None and max_min_price <= px:
                reached = name
        s9 = sell.get("S9")
        if s9 is not None and max_min_price < s9:
            reached = "Beyond S9"
        return reached

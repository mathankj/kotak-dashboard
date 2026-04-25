"""Gann Square of 9 levels and helpers.

Pure math + small lookup helpers. No I/O, no network — safe to unit-test.

Levels are computed in sqrt-space stepping by 22.5° = 0.0625:
  S3=-6, S2=-5, S1=-4, SELL_WA=-3, SELL=-2  (below open)
  BUY=+2, BUY_WA=+3, T1=+4, T2=+5, T3=+6   (above open)
"""
import math


GANN_STEP = 0.0625

SELL_LEVELS = ["S3", "S2", "S1", "SELL_WA", "SELL"]
BUY_LEVELS  = ["BUY", "BUY_WA", "T1", "T2", "T3"]

# UI presentation only — colors used by the dashboard for level highlighting.
LEVEL_COLORS = {
    "S3": "#B71C1C", "S2": "#C62828", "S1": "#D32F2F",
    "SELL_WA": "#FF9800", "SELL": "#EF9A9A",
    "BUY": "#A5D6A7", "BUY_WA": "#FF9800",
    "T1": "#81C784", "T2": "#66BB6A", "T3": "#388E3C",
}

# Order used when computing how deep a trade went in its favoured direction.
BUY_LEVEL_ORDER  = ["BUY", "BUY_WA", "T1", "T2", "T3"]
SELL_LEVEL_ORDER = ["SELL", "SELL_WA", "S1", "S2", "S3"]


def gann_levels(open_price):
    """Compute Gann Square of 9 levels from the opening price.
    Returns dict {sell: {S3..SELL}, buy: {BUY..T3}}."""
    if not open_price or open_price <= 0:
        return {"sell": {k: None for k in SELL_LEVELS},
                "buy":  {k: None for k in BUY_LEVELS}}
    sq = math.sqrt(open_price)
    sell = {}
    for i, name in enumerate(SELL_LEVELS):
        n = -(6 - i)
        sell[name] = round((sq + n * GANN_STEP) ** 2, 2)
    buy = {}
    for i, name in enumerate(BUY_LEVELS):
        n = i + 2
        buy[name] = round((sq + n * GANN_STEP) ** 2, 2)
    return {"sell": sell, "buy": buy}


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
        t3 = buy.get("T3")
        if t3 is not None and max_min_price > t3:
            reached = "Beyond T3"
        return reached
    else:
        reached = None
        for name in SELL_LEVEL_ORDER:
            px = sell.get(name)
            if px is not None and max_min_price <= px:
                reached = name
        s3 = sell.get("S3")
        if s3 is not None and max_min_price < s3:
            reached = "Beyond S3"
        return reached

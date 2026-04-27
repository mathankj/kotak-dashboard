"""Kotak instrument metadata: scrip master, F&O universe, option chain builder.

SCRIPS is the static list of equities + indices the dashboard tracks.
INDEX_OPTIONS_CONFIG drives the option-chain pages.

_option_universe is a per-day cache of all OPTIDX records per index, populated
on first /options page load (or by the preload thread in app.py). It's also
mutated by the /api/option-prices route to surface "loading" / "preload_status"
to the UI — kept module-level on purpose so multiple call sites can read/write.
"""
from datetime import datetime

from backend.kotak.client import ensure_client
from backend.utils import now_ist


SCRIPS = [
    # Indices: not tradeable as cash; buy/sell buttons hidden in UI.
    {"symbol": "NIFTY 50",  "token": "Nifty 50",   "exchange": "nse_cm", "trading_symbol": None,           "tradeable": False, "lot": 1},
    {"symbol": "BANKNIFTY", "token": "Nifty Bank", "exchange": "nse_cm", "trading_symbol": None,           "tradeable": False, "lot": 1},
    {"symbol": "SENSEX",    "token": "SENSEX",     "exchange": "bse_cm", "trading_symbol": None,           "tradeable": False, "lot": 1},
    # Equities: tradeable. trading_symbol is what Kotak place_order needs.
    {"symbol": "RELIANCE",  "token": "2885",       "exchange": "nse_cm", "trading_symbol": "RELIANCE-EQ",  "tradeable": True,  "lot": 1},
    {"symbol": "TCS",       "token": "11536",      "exchange": "nse_cm", "trading_symbol": "TCS-EQ",       "tradeable": True,  "lot": 1},
    {"symbol": "INFOSYS",   "token": "1594",       "exchange": "nse_cm", "trading_symbol": "INFY-EQ",      "tradeable": True,  "lot": 1},
    {"symbol": "HDFCBANK",  "token": "1333",       "exchange": "nse_cm", "trading_symbol": "HDFCBANK-EQ",  "tradeable": True,  "lot": 1},
    {"symbol": "ICICIBANK", "token": "4963",       "exchange": "nse_cm", "trading_symbol": "ICICIBANK-EQ", "tradeable": True,  "lot": 1},
    {"symbol": "SBIN",      "token": "3045",       "exchange": "nse_cm", "trading_symbol": "SBIN-EQ",      "tradeable": True,  "lot": 1},
]


def find_scrip(symbol):
    for s in SCRIPS:
        if s["symbol"] == symbol:
            return s
    return None


# Index option chains. Each renders an ATM ± window chain (CE+Strike+PE) and
# resolves the nearest future expiry dynamically via search_scrip.
# lot_size = number of shares in 1 lot for each index option contract.
# These are SEBI/exchange-set values that change occasionally (NIFTY moved
# 50 -> 75 in late 2024; BANKNIFTY 15 -> 35). VERIFY before flipping
# LIVE_MODE — wrong lot size = wrong qty = very wrong order. Source of
# truth is the Kotak instrument master ("lLotSize" field) but we hardcode
# here so the bot fails-safe if the master fetch is slow/down.
INDEX_OPTIONS_CONFIG = {
    "NIFTY": {
        "label": "NIFTY 50",
        "spot_symbol_key": "NIFTY 50",
        "exchange_segment": "nse_fo",
        "strike_step": 50,
        "atm_window": 5,
        "lot_size": 75,           # NIFTY weekly/monthly, current as of 2025
    },
    "BANKNIFTY": {
        "label": "BANK NIFTY",
        "spot_symbol_key": "BANKNIFTY",
        "exchange_segment": "nse_fo",
        "strike_step": 100,
        "atm_window": 5,
        "lot_size": 35,           # BANKNIFTY monthly, current as of 2025
    },
    "SENSEX": {
        "label": "SENSEX",
        "spot_symbol_key": "SENSEX",
        "exchange_segment": "bse_fo",
        "strike_step": 100,
        "atm_window": 5,
        "lot_size": 20,           # SENSEX weekly, current as of 2025
    },
}


# Per-day cache of F&O universe per index. Mutated by _fetch_index_fo_universe
# below AND by app.py routes (loading flag, preload_status). Keeping this
# module-level so all callers share the same view.
_option_universe = {"date": None, "by_index": {}, "error": None}


def _fetch_index_fo_universe(index_name):
    """Returns (items, err) of all OPTIDX records for an index, cached per day."""
    cfg = INDEX_OPTIONS_CONFIG[index_name]
    today = now_ist().strftime("%Y-%m-%d")
    if (_option_universe["date"] == today
            and index_name in _option_universe["by_index"]):
        return _option_universe["by_index"][index_name], None
    try:
        client = ensure_client()
    except Exception as e:
        return [], f"login: {e}"
    try:
        r = client.search_scrip(
            exchange_segment=cfg["exchange_segment"],
            symbol=index_name,
        )
    except Exception as e:
        return [], f"search_scrip {index_name}: {type(e).__name__}: {e}"
    # NSE uses pInstType="OPTIDX", BSE uses "IO" — accept both
    items = [
        x for x in (r or []) if isinstance(x, dict)
        and str(x.get("pSymbolName", "")).strip().upper() == index_name.upper()
        and str(x.get("pInstType", "")).strip().upper() in ("OPTIDX", "IO")
    ]
    if _option_universe["date"] != today:
        _option_universe["date"] = today
        _option_universe["by_index"] = {}
    _option_universe["by_index"][index_name] = items
    return items, None


def _parse_item_strike(item):
    """dStrikePrice is scaled x100; key has a trailing semicolon in the SDK response."""
    raw = item.get("dStrikePrice;", item.get("dStrikePrice"))
    try:
        return int(round(float(raw) / 100.0))
    except (TypeError, ValueError):
        return None


def _parse_item_expiry_date(item):
    """Parse pExpiryDate ('28Apr2026') -> date object, or None."""
    s = str(item.get("pExpiryDate", "")).strip()
    try:
        return datetime.strptime(s, "%d%b%Y").date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------
# INDEX FUTURES — nearest-expiry lookup per index, cached per day.
# Live SDK returns FUTIDX (NSE) and IF (BSE) records via search_scrip.
# Used by the futures auto-strategy in backend/strategy/futures.py.
# ---------------------------------------------------------------------
_future_universe = {"date": None, "by_index": {}, "error": None}


def _fetch_nearest_index_future(index_name):
    """Return (record_dict, err) for the nearest-expiry FUT contract of `index_name`.

    record_dict has the SDK fields we need: pTrdSymbol, pSymbol (token),
    pExchSeg, pExpiryDate, lLotSize. Cached per trading day so we only
    hit search_scrip once per index per session.
    """
    cfg = INDEX_OPTIONS_CONFIG.get(index_name)
    if not cfg:
        return None, f"unknown index {index_name}"
    today = now_ist().strftime("%Y-%m-%d")
    if (_future_universe["date"] == today
            and index_name in _future_universe["by_index"]):
        return _future_universe["by_index"][index_name], None
    try:
        client = ensure_client()
    except Exception as e:
        return None, f"login: {e}"
    try:
        r = client.search_scrip(
            exchange_segment=cfg["exchange_segment"],
            symbol=index_name,
        )
    except Exception as e:
        return None, f"search_scrip {index_name}: {type(e).__name__}: {e}"
    # NSE uses FUTIDX, BSE uses IF for index futures.
    futs = [
        x for x in (r or []) if isinstance(x, dict)
        and str(x.get("pSymbolName", "")).strip().upper() == index_name.upper()
        and str(x.get("pInstType", "")).strip().upper() in ("FUTIDX", "IF")
    ]
    today_dt = now_ist().date()
    parsed = []
    for it in futs:
        d = _parse_item_expiry_date(it)
        if d and d >= today_dt:
            parsed.append((d, it))
    if not parsed:
        return None, f"no future expiry for {index_name}"
    parsed.sort(key=lambda p: p[0])
    nearest = parsed[0][1]
    if _future_universe["date"] != today:
        _future_universe["date"] = today
        _future_universe["by_index"] = {}
    _future_universe["by_index"][index_name] = nearest
    return nearest, None

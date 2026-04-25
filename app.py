"""
Kotak Neo Dashboard - Web app showing holdings, positions, orders, trades, limits.

Run:
    pip install flask pyotp python-dotenv
    python app.py
Open: http://localhost:5000
"""
import os
import json
import math
import time
import threading
import traceback
import pyotp
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template_string, jsonify, redirect, url_for, request, Response
from dotenv import load_dotenv
from neo_api_client import NeoAPI
from quote_feed import QuoteFeed

load_dotenv()

app = Flask(__name__)
_state = {"client": None, "login_time": None, "greeting": None, "error": None}

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login_history.json")


def append_history(status, detail):
    """Append a login attempt to the history file (JSONL)."""
    entry = {
        "timestamp": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "status": status,  # "success" or "failed"
        "detail": detail,
    }
    try:
        existing = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                existing = json.load(f)
        existing.insert(0, entry)  # newest first
        existing = existing[:30]   # keep last 30
        with open(HISTORY_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass  # don't let history errors break login


def read_history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def login():
    """Fresh login using TOTP. Returns NeoAPI client or raises."""
    client = NeoAPI(
        environment="prod",
        access_token=None,
        neo_fin_key=None,
        consumer_key=os.getenv("KOTAK_CONSUMER_KEY"),
    )
    totp_code = pyotp.TOTP(os.getenv("KOTAK_TOTP_SECRET")).now()
    login_resp = client.totp_login(
        mobile_number=os.getenv("KOTAK_MOBILE"),
        ucc=os.getenv("KOTAK_UCC"),
        totp=totp_code,
    )
    if "error" in login_resp:
        raise RuntimeError(f"totp_login failed: {login_resp['error']}")

    validate_resp = client.totp_validate(mpin=os.getenv("KOTAK_MPIN"))
    if "error" in validate_resp:
        raise RuntimeError(f"totp_validate failed: {validate_resp['error']}")

    greeting = validate_resp.get("data", {}).get("greetingName", "Trader")
    return client, greeting


def ensure_client():
    """Login if not already logged in, or return existing client."""
    if _state["client"] is None:
        try:
            client, greeting = login()
            _state["client"] = client
            _state["greeting"] = greeting
            _state["login_time"] = now_ist()
            _state["error"] = None
            append_history("success", f"Logged in as {greeting}")
        except Exception as e:
            _state["error"] = str(e)
            _state["client"] = None
            append_history("failed", str(e))
            raise
    return _state["client"]


def safe_call(fn, *args, **kwargs):
    """Call API method with try/catch. Returns (data, error_str).
    Treats 'no data found' responses as empty, not errors."""
    try:
        resp = fn(*args, **kwargs)
        if isinstance(resp, dict) and "error" in resp:
            err = resp["error"]
            # Normalise to list of dicts
            err_list = err if isinstance(err, list) else [err]
            # 'No <X> found' messages are empty state, not errors
            empty_markers = ["no holdings found", "no positions", "no orders",
                             "no trades", "no data", "not found"]
            for e in err_list:
                msg = (e.get("message") if isinstance(e, dict) else str(e)).lower()
                if any(m in msg for m in empty_markers):
                    return [], None  # empty result, no error
            return None, str(err)
        # SDK wraps success payload under "data"
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"], None
        return resp, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ---------- Gann Trader: scrips, levels, quote cache ----------
SCRIPS = [
    # Indices: not tradeable as cash; buy/sell buttons hidden
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

# Gann Square of 9 step = 22.5° in sqrt-space
GANN_STEP = 0.0625

SELL_LEVELS = ["S3", "S2", "S1", "SELL_WA", "SELL"]   # n = -5..-1
BUY_LEVELS  = ["BUY", "BUY_WA", "T1", "T2", "T3"]      # n = +1..+5

LEVEL_COLORS = {
    "S3": "#B71C1C", "S2": "#C62828", "S1": "#D32F2F",
    "SELL_WA": "#FF9800", "SELL": "#EF9A9A",
    "BUY": "#A5D6A7", "BUY_WA": "#FF9800",
    "T1": "#81C784", "T2": "#66BB6A", "T3": "#388E3C",
}


def gann_levels(open_price):
    """Compute Gann Square of 9 levels from the opening price.
    Returns dict {sell: {S3..SELL}, buy: {BUY..T3}}.
    Sell levels are 5..1 steps below open, buy levels are 1..5 steps above."""
    if not open_price or open_price <= 0:
        return {"sell": {k: None for k in SELL_LEVELS},
                "buy":  {k: None for k in BUY_LEVELS}}
    sq = math.sqrt(open_price)
    sell = {}
    for i, name in enumerate(SELL_LEVELS):
        # S3=-6, S2=-5, S1=-4, SELL_WA=-3, SELL=-2 (matches Square-of-9 reference)
        n = -(6 - i)
        sell[name] = round((sq + n * GANN_STEP) ** 2, 2)
    buy = {}
    for i, name in enumerate(BUY_LEVELS):
        # BUY=+2, BUY_WA=+3, T1=+4, T2=+5, T3=+6
        n = i + 2
        buy[name] = round((sq + n * GANN_STEP) ** 2, 2)
    return {"sell": sell, "buy": buy}


# ---- quote cache (refresh on demand, TTL 2 sec) ----
_quote_cache = {"data": {}, "ts": 0, "error": None}
QUOTE_TTL = 2.0  # seconds

# ---- WebSocket QuoteFeed (Phase 1 of Super Duper Engine) ----
# REST polling stays as fallback; WS overlays fresher LTP when available.
_feed = QuoteFeed(client_provider=ensure_client)
_feed_started = {"flag": False, "lock": threading.Lock()}
WS_FRESH_SECONDS = 5.0   # WS tick considered fresh if newer than this


def _ensure_feed_started():
    """Start the WS feed once; safe to call from any request thread."""
    if _feed_started["flag"]:
        return
    with _feed_started["lock"]:
        if _feed_started["flag"]:
            return
        # Subscribe to all SCRIPS at startup. Indices vs equities split by
        # exchange segment + symbol shape: index tokens are non-numeric.
        idx_subs, scrip_subs = [], []
        for s in SCRIPS:
            entry = {"instrument_token": s["token"],
                     "exchange_segment": s["exchange"]}
            if s.get("tradeable"):
                scrip_subs.append(entry)
            else:
                idx_subs.append(entry)
        _feed.set_index_subs(idx_subs)
        _feed.set_scrip_subs(scrip_subs)
        _feed.start()
        _feed_started["flag"] = True
        print(f"[quote_feed] started: {len(idx_subs)} indices, "
              f"{len(scrip_subs)} scrips")


def _ws_overlay(out_dict, key_to_token_exch):
    """Overlay WS LTP onto out_dict in place.
    Rules:
      - WS LTP fresher than WS_FRESH_SECONDS → always wins over REST
      - WS LTP stale but REST returned None → still use WS (better than nothing,
        e.g., off-hours snapshot)
      - WS LTP stale and REST has its own value → keep REST
    `ws_age` field is added for diagnostics.
    Returns count of overlays applied."""
    overlaid = 0
    now = time.time()
    for key, (exch, token) in key_to_token_exch.items():
        tick = _feed.get(exch, token)
        if not tick or tick.get("ltp") is None:
            continue
        age = now - tick.get("ts", 0)
        rec = out_dict.get(key)
        if not rec:
            continue
        rest_ltp = rec.get("ltp")
        is_fresh = age <= WS_FRESH_SECONDS
        if is_fresh or rest_ltp is None:
            rec["ltp"] = tick["ltp"]
            rec["ws_age"] = round(age, 2)
            # Backfill OHLC if REST didn't have it (off-hours, etc.)
            # Treat 0.0 as missing — Kotak sometimes returns 0 for closed mkt.
            for src, dst in (("op","open"), ("lo","low"), ("h","high")):
                if (rec.get(dst) in (None, 0, 0.0)
                        and tick.get(src) is not None):
                    rec[dst] = tick[src]
            # Recompute Gann levels if we just filled in 'open'
            if rec.get("open") and not rec.get("levels", {}).get("buy"):
                rec["levels"] = gann_levels(rec["open"])
            overlaid += 1
    return overlaid


def fetch_quotes(force=False):
    """Fetch quotes for all SCRIPS via Kotak. Returns dict {symbol: {...}}.
    Uses TTL cache."""
    now = time.time()
    if not force and (now - _quote_cache["ts"]) < QUOTE_TTL and _quote_cache["data"]:
        return _quote_cache["data"], _quote_cache["error"]

    try:
        client = ensure_client()
    except Exception as e:
        _quote_cache["error"] = f"login: {e}"
        return _quote_cache["data"], _quote_cache["error"]

    out = {}
    last_err = None
    tokens = [{"instrument_token": s["token"], "exchange_segment": s["exchange"]} for s in SCRIPS]

    def _call(qt):
        try:
            r = client.quotes(instrument_tokens=tokens, quote_type=qt)
        except Exception as e:
            return None, f"{qt}: {type(e).__name__}: {e}"
        if isinstance(r, dict) and "fault" in r:
            return None, f"{qt}: {r['fault'].get('message','fault')}"
        if isinstance(r, list):
            return r, None
        return [], f"{qt}: unexpected response shape"

    ohlc_items, e1 = _call("ohlc")
    ltp_items, e2  = _call("ltp")
    last_err = e1 or e2

    # Index responses by (exchange, exchange_token) — Kotak echoes these back
    def index_by_key(items):
        idx = {}
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            key = (str(it.get("exchange","")).strip().lower(),
                   str(it.get("exchange_token","")).strip().lower())
            idx[key] = it
        return idx

    ohlc_idx = index_by_key(ohlc_items)
    ltp_idx  = index_by_key(ltp_items)

    for s in SCRIPS:
        key = (s["exchange"].lower(), str(s["token"]).lower())
        ohlc_it = ohlc_idx.get(key, {})
        ltp_it  = ltp_idx.get(key, {})
        ohlc    = ohlc_it.get("ohlc") if isinstance(ohlc_it.get("ohlc"), dict) else {}
        ltp_v   = None
        try:
            ltp_v = float(ltp_it.get("ltp")) if ltp_it.get("ltp") not in (None, "", "0") else None
        except (TypeError, ValueError):
            pass
        op   = float(ohlc.get("open"))  if ohlc.get("open")  not in (None, "", "0") else None
        low  = float(ohlc.get("low"))   if ohlc.get("low")   not in (None, "", "0") else None
        high = float(ohlc.get("high"))  if ohlc.get("high")  not in (None, "", "0") else None
        # If LTP missing (market closed), fall back to ohlc.close
        if ltp_v is None:
            try:
                ltp_v = float(ohlc.get("close")) if ohlc.get("close") not in (None, "", "0") else None
            except (TypeError, ValueError):
                pass
        out[s["symbol"]] = {
            "symbol": s["symbol"],
            "token": s["token"],
            "ltp": ltp_v,
            "open": op,
            "low": low,
            "high": high,
            "levels": gann_levels(op) if op else {"sell": {}, "buy": {}},
        }

    # Overlay fresh WS LTPs (Phase 1 — WebSocket QuoteFeed)
    try:
        _ensure_feed_started()
        _ws_overlay(out, {s["symbol"]: (s["exchange"], s["token"]) for s in SCRIPS})
    except Exception as e:
        print(f"[quote_feed] overlay (stocks) failed: {type(e).__name__}: {e}")

    _quote_cache["data"] = out
    _quote_cache["ts"] = now
    _quote_cache["error"] = last_err
    return out, last_err


# ---------- Options (F&O) ----------
# Index option chains. Each index renders an ATM ± window chain (CE+Strike+PE)
# and resolves the nearest future expiry dynamically via search_scrip.
INDEX_OPTIONS_CONFIG = {
    "NIFTY": {
        "label": "NIFTY 50",
        "spot_symbol_key": "NIFTY 50",       # key in SCRIPS / fetch_quotes out
        "exchange_segment": "nse_fo",
        "strike_step": 50,
        "atm_window": 5,                     # ± strikes (11 total incl ATM)
    },
    "BANKNIFTY": {
        "label": "BANK NIFTY",
        "spot_symbol_key": "BANKNIFTY",
        "exchange_segment": "nse_fo",
        "strike_step": 100,
        "atm_window": 5,
    },
    "SENSEX": {
        "label": "SENSEX",
        "spot_symbol_key": "SENSEX",
        "exchange_segment": "bse_fo",
        "strike_step": 100,
        "atm_window": 5,
    },
}

# Per-day cache of F&O universe per index (list of all option contract records)
_option_universe = {"date": None, "by_index": {}, "error": None}
# TTL cache of live quotes
_option_quote_cache = {"ts": 0.0, "data": {}, "error": None, "meta": {}}


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
    """dStrikePrice; field is scaled x100 and has a trailing semicolon in key."""
    raw = item.get("dStrikePrice;", item.get("dStrikePrice"))
    try:
        return int(round(float(raw) / 100.0))
    except (TypeError, ValueError):
        return None


def _parse_item_expiry_date(item):
    """Returns a date object parsed from pExpiryDate (e.g. '28Apr2026'), or None."""
    s = str(item.get("pExpiryDate", "")).strip()
    try:
        return datetime.strptime(s, "%d%b%Y").date()
    except (ValueError, TypeError):
        return None


def build_option_chain(index_name):
    """Returns (rows, meta) for one index.
    rows: [{strike, ce: item_or_None, pe: item_or_None, is_atm}]
    meta: {atm, expiry, spot, error?}"""
    cfg = INDEX_OPTIONS_CONFIG[index_name]
    meta = {"atm": None, "expiry": None, "spot": None, "error": None}
    # 1. Spot
    spot_quotes, _ = fetch_quotes()
    spot_row = spot_quotes.get(cfg["spot_symbol_key"])
    if not spot_row or spot_row.get("ltp") is None:
        meta["error"] = f"no spot for {cfg['spot_symbol_key']}"
        return [], meta
    spot = float(spot_row["ltp"])
    meta["spot"] = spot
    # 2. Universe
    items, err = _fetch_index_fo_universe(index_name)
    if err:
        meta["error"] = err
    if not items:
        return [], meta
    # 3. Nearest future expiry
    today = now_ist().date()
    parsed = []
    for it in items:
        d = _parse_item_expiry_date(it)
        if d and d >= today:
            parsed.append((d, str(it.get("pExpiryDate", "")).strip()))
    if not parsed:
        meta["error"] = "no future expiry"
        return [], meta
    nearest_date, nearest_str = min(parsed, key=lambda p: p[0])
    meta["expiry"] = nearest_str
    # 4. ATM strike + window
    step = cfg["strike_step"]
    atm = int(round(spot / step) * step)
    meta["atm"] = atm
    wanted = [atm + i * step for i in range(-cfg["atm_window"], cfg["atm_window"] + 1)]
    # 5. Lookup
    look = {}
    for it in items:
        if str(it.get("pExpiryDate", "")).strip() != nearest_str:
            continue
        s = _parse_item_strike(it)
        if s is None or s not in wanted:
            continue
        t = str(it.get("pOptionType", "")).strip().upper()
        if t in ("CE", "PE"):
            look[(s, t)] = it
    rows = []
    for s in wanted:
        rows.append({
            "strike": s,
            "ce": look.get((s, "CE")),
            "pe": look.get((s, "PE")),
            "is_atm": s == atm,
        })
    return rows, meta


def build_all_option_tokens():
    """Flat list of {key, token, exchange, ...} for all configured index chains.
    Also returns per-index meta {atm, expiry, spot, error}."""
    all_resolved = []
    meta = {}
    for idx_name, cfg in INDEX_OPTIONS_CONFIG.items():
        rows, m = build_option_chain(idx_name)
        meta[idx_name] = m
        for row in rows:
            for t, item in (("CE", row.get("ce")), ("PE", row.get("pe"))):
                if not item:
                    continue
                token = item.get("pSymbol") or item.get("instrument_token")
                if not token:
                    continue
                all_resolved.append({
                    "key": f"{idx_name} {row['strike']} {t}",
                    "index": idx_name,
                    "strike": row["strike"],
                    "option_type": t,
                    "token": str(token),
                    "exchange": cfg["exchange_segment"],
                    "trading_symbol": item.get("pTrdSymbol", ""),
                    "expiry": m.get("expiry", ""),
                    "is_atm": row.get("is_atm", False),
                    "lot_size": item.get("lLotSize"),
                })
    return all_resolved, meta


def fetch_option_quotes(force=False):
    """Live quotes for all configured index option chains. TTL-cached."""
    now = time.time()
    if (not force
            and (now - _option_quote_cache["ts"]) < QUOTE_TTL
            and _option_quote_cache["data"]):
        return (_option_quote_cache["data"],
                _option_quote_cache["meta"],
                _option_quote_cache["error"])
    insts, idx_meta = build_all_option_tokens()
    if not insts:
        err = next((m.get("error") for m in idx_meta.values() if m.get("error")), None)
        return {}, idx_meta, err or "no option instruments resolved"
    try:
        client = ensure_client()
    except Exception as e:
        return {}, idx_meta, f"login: {e}"
    tokens = [{"instrument_token": i["token"], "exchange_segment": i["exchange"]}
              for i in insts]

    def _call(qt):
        try:
            r = client.quotes(instrument_tokens=tokens, quote_type=qt)
        except Exception as e:
            return None, f"{qt}: {type(e).__name__}: {e}"
        if isinstance(r, dict) and "fault" in r:
            return None, f"{qt}: {r['fault'].get('message', 'fault')}"
        if isinstance(r, list):
            return r, None
        return [], f"{qt}: unexpected response shape"

    ohlc_items, e1 = _call("ohlc")
    ltp_items,  e2 = _call("ltp")
    last_err = e1 or e2

    def index_by_key(items):
        idx = {}
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            k = (str(it.get("exchange", "")).strip().lower(),
                 str(it.get("exchange_token", "")).strip().lower())
            idx[k] = it
        return idx

    ohlc_idx = index_by_key(ohlc_items)
    ltp_idx  = index_by_key(ltp_items)
    out = {}
    for i in insts:
        k = (i["exchange"].lower(), str(i["token"]).lower())
        ohlc_it = ohlc_idx.get(k, {})
        ltp_it  = ltp_idx.get(k, {})
        ohlc    = ohlc_it.get("ohlc") if isinstance(ohlc_it.get("ohlc"), dict) else {}
        ltp_v = None
        try:
            ltp_v = float(ltp_it.get("ltp")) if ltp_it.get("ltp") not in (None, "", "0") else None
        except (TypeError, ValueError):
            pass
        close = float(ohlc.get("close")) if ohlc.get("close") not in (None, "", "0") else None
        if ltp_v is None and close is not None:
            ltp_v = close
        change_pct = None
        if ltp_v is not None and close not in (None, 0):
            try:
                change_pct = round(((ltp_v - close) / close) * 100, 2)
            except ZeroDivisionError:
                pass
        out[i["key"]] = {
            "key": i["key"],
            "index": i["index"],
            "strike": i["strike"],
            "option_type": i["option_type"],
            "trading_symbol": i["trading_symbol"],
            "expiry": i["expiry"],
            "is_atm": i["is_atm"],
            "ltp": ltp_v,
            "close": close,
            "change_pct": change_pct,
        }
    # Overlay fresh WS LTPs and refresh option subs if ATM drifted
    try:
        _ensure_feed_started()
        # Update option subscription set so the WS streams the active ATM±N strikes
        opt_subs = [{"instrument_token": i["token"],
                     "exchange_segment": i["exchange"]} for i in insts]
        if _feed.set_option_subs(opt_subs):
            print(f"[quote_feed] option subs updated: {len(opt_subs)} contracts")
        _ws_overlay(out, {i["key"]: (i["exchange"], i["token"]) for i in insts})
    except Exception as e:
        print(f"[quote_feed] overlay (options) failed: {type(e).__name__}: {e}")

    _option_quote_cache["data"] = out
    _option_quote_cache["ts"] = now
    _option_quote_cache["meta"] = idx_meta
    _option_quote_cache["error"] = last_err
    return out, idx_meta, last_err


# ---------- Order placement + audit log ----------
ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders_log.json")


def append_order(entry):
    """Append an order attempt to orders_log.json. Newest first, keep last 200."""
    try:
        existing = []
        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, "r") as f:
                existing = json.load(f)
        existing.insert(0, entry)
        existing = existing[:200]
        with open(ORDERS_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception:
        pass


def read_orders():
    try:
        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def find_scrip(symbol):
    for s in SCRIPS:
        if s["symbol"] == symbol:
            return s
    return None


# ---------- Paper trading storage ----------
PAPER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades.json")


def read_paper_trades():
    try:
        if os.path.exists(PAPER_FILE):
            with open(PAPER_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def write_paper_trades(trades):
    try:
        with open(PAPER_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception:
        pass


def _next_paper_id(trades):
    mx = 0
    for t in trades:
        try:
            n = int(t.get("id", "0"))
            if n > mx:
                mx = n
        except (TypeError, ValueError):
            pass
    return str(mx + 1)


def nearest_gann_level(symbol_data):
    """Return (level_name, distance_pct) of the gann level nearest to current LTP.
    Used for LTP box coloring. symbol_data = {ltp, levels: {sell: {...}, buy: {...}}}."""
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


# Ordering used when figuring out "how far in favour did the trade go"
BUY_LEVEL_ORDER  = ["BUY", "BUY_WA", "T1", "T2", "T3"]
SELL_LEVEL_ORDER = ["SELL", "SELL_WA", "S1", "S2", "S3"]


def compute_target_level_reached(side, entry_price, max_min_price, levels):
    """Given the best price reached since entry, determine the deepest
    gann level touched in favour of the position.
    side: 'B' or 'S'; levels: {sell:{}, buy:{}}."""
    buy = (levels or {}).get("buy") or {}
    sell = (levels or {}).get("sell") or {}
    if side == "B":
        # For BUY: price goes UP. Find highest level whose price <= max_min_price.
        reached = None
        for name in BUY_LEVEL_ORDER:
            px = buy.get(name)
            if px is not None and max_min_price >= px:
                reached = name
        # Beyond T3
        t3 = buy.get("T3")
        if t3 is not None and max_min_price > t3:
            reached = "Beyond T3"
        return reached
    else:  # SELL: price goes DOWN
        reached = None
        for name in SELL_LEVEL_ORDER:
            px = sell.get(name)
            if px is not None and max_min_price <= px:
                reached = name
        s3 = sell.get("S3")
        if s3 is not None and max_min_price < s3:
            reached = "Beyond S3"
        return reached


def update_open_trades_mfe(quotes_by_symbol):
    """Called each time quotes refresh. For every OPEN trade, update
    max_min_target_price and target_level_reached based on the current LTP."""
    trades = read_paper_trades()
    changed = False
    for t in trades:
        if t.get("status") != "OPEN":
            continue
        q = quotes_by_symbol.get(t["scrip"])
        if not q:
            continue
        ltp = q.get("ltp")
        if ltp is None:
            continue
        prev_mfe = t.get("max_min_target_price")
        if t["order_type"] == "BUY":
            new_mfe = ltp if prev_mfe is None else max(prev_mfe, ltp)
        else:
            new_mfe = ltp if prev_mfe is None else min(prev_mfe, ltp)
        if new_mfe != prev_mfe:
            t["max_min_target_price"] = round(new_mfe, 2)
            changed = True
        side = "B" if t["order_type"] == "BUY" else "S"
        reached = compute_target_level_reached(
            side, t["entry_price"], new_mfe, q.get("levels"))
        if reached and reached != t.get("target_level_reached"):
            t["target_level_reached"] = reached
            changed = True
    if changed:
        write_paper_trades(trades)


def compute_stats(trades):
    active = sum(1 for t in trades if t.get("status") == "OPEN")
    closed = sum(1 for t in trades if t.get("status") == "CLOSED")
    total_pnl = 0.0
    for t in trades:
        if t.get("status") == "CLOSED" and t.get("pnl_points") is not None:
            try:
                total_pnl += float(t["pnl_points"]) * int(t.get("qty", 1))
            except (TypeError, ValueError):
                pass
    return {"active": active, "closed": closed, "pnl": round(total_pnl, 2)}


def fmt_duration(seconds):
    if seconds is None or seconds < 0:
        return ""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------- Auto-strategy engine (paper trading only) ----------
# Rules (v1):
#   Hours:        09:15 - 15:15 IST (square off all OPEN at 15:15)
#   Entry BUY:    LTP crosses above BUY level (from <=BUY to >BUY)
#   Entry SELL:   LTP crosses below SELL level (from >=SELL to <SELL)
#   Target BUY:   exit when LTP >= T1
#   Target SELL:  exit when LTP <= S1
#   SL BUY:       exit when LTP < SELL level
#   SL SELL:      exit when LTP > BUY level
#   Qty:          AUTO_QTY per trade
#   Max/scrip/day: AUTO_MAX_TRADES_PER_SCRIP
#   Side effect:  writes to paper_trades.json only (no Kotak order)

AUTO_STRATEGY_ENABLED = True
AUTO_HOURS_START = (9, 15)
AUTO_HOURS_END   = (15, 15)
AUTO_MAX_TRADES_PER_SCRIP = 2
AUTO_QTY = 1

_auto_state = {
    "last_ltp": {},  # symbol -> last LTP seen (to detect crossings)
    "lock": threading.Lock(),
}


def _auto_in_hours(now):
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return AUTO_HOURS_START <= hm < AUTO_HOURS_END


def _auto_at_or_after_squareoff(now):
    if now.weekday() >= 5:
        return False  # weekends: don't force square-off, just idle
    return (now.hour, now.minute) >= AUTO_HOURS_END


def _auto_close(trade, ltp, now, reason):
    trade["exit_time"] = now.strftime("%H:%M:%S")
    trade["exit_ts"]   = now.timestamp()
    trade["exit_price"] = round(ltp, 2)
    trade["exit_reason"] = reason
    if trade["order_type"] == "BUY":
        pnl = ltp - trade["entry_price"]
    else:
        pnl = trade["entry_price"] - ltp
    trade["pnl_points"] = round(pnl, 2)
    trade["pnl_pct"]    = round((pnl / trade["entry_price"]) * 100, 2) if trade["entry_price"] else 0.0
    trade["duration_seconds"] = round(now.timestamp() - trade.get("entry_ts", now.timestamp()), 1)
    trade["status"] = "CLOSED"


def _auto_open(sym, side, qty, ltp, now, trades):
    t = {
        "id": _next_paper_id(trades),
        "date": now.strftime("%Y-%m-%d"),
        "scrip": sym,
        "order_type": "BUY" if side == "B" else "SELL",
        "entry_time": now.strftime("%H:%M:%S"),
        "entry_ts": now.timestamp(),
        "entry_price": round(ltp, 2),
        "qty": qty,
        "max_min_target_price": round(ltp, 2),
        "target_level_reached": None,
        "exit_time": None, "exit_ts": None, "exit_price": None,
        "exit_reason": None, "pnl_points": None, "pnl_pct": None,
        "duration_seconds": None,
        "status": "OPEN",
        "auto": True,
    }
    trades.insert(0, t)


def _auto_check_entry(q, prev_ltp, cur_ltp):
    """Return 'B' or 'S' on a level crossing, else None."""
    levels = q.get("levels") or {}
    buy_px  = (levels.get("buy")  or {}).get("BUY")
    sell_px = (levels.get("sell") or {}).get("SELL")
    if buy_px is not None and prev_ltp <= buy_px < cur_ltp:
        return "B"
    if sell_px is not None and prev_ltp >= sell_px > cur_ltp:
        return "S"
    return None


def _auto_check_exit(trade, q, ltp):
    """Return exit reason string if exit conditions met, else None."""
    levels = q.get("levels") or {}
    buy    = levels.get("buy")  or {}
    sell   = levels.get("sell") or {}
    if trade["order_type"] == "BUY":
        t1 = buy.get("T1")
        sl = sell.get("SELL")
        if t1 is not None and ltp >= t1:
            return "TARGET_T1"
        if sl is not None and ltp < sl:
            return "SL_SELL_LVL"
    else:
        s1 = sell.get("S1")
        sl = buy.get("BUY")
        if s1 is not None and ltp <= s1:
            return "TARGET_S1"
        if sl is not None and ltp > sl:
            return "SL_BUY_LVL"
    return None


def auto_strategy_tick(quotes):
    """One tick of the paper auto-strategy. Called after each fetch_quotes."""
    if not AUTO_STRATEGY_ENABLED or not quotes:
        return
    now = now_ist()
    with _auto_state["lock"]:
        trades = read_paper_trades()
        modified = False

        # 1. Square off at/after 15:15
        if _auto_at_or_after_squareoff(now):
            for t in trades:
                if t.get("status") != "OPEN":
                    continue
                q = quotes.get(t["scrip"])
                if not q or q.get("ltp") is None:
                    continue
                _auto_close(t, float(q["ltp"]), now, "AUTO_SQUARE_OFF")
                modified = True
            if modified:
                write_paper_trades(trades)
            return

        # 2. Only run during market hours
        if not _auto_in_hours(now):
            return

        today = now.strftime("%Y-%m-%d")
        counts = {}
        for t in trades:
            if t.get("date") == today:
                counts[t["scrip"]] = counts.get(t["scrip"], 0) + 1
        open_by_sym = {t["scrip"]: t for t in trades if t.get("status") == "OPEN"}

        for scrip in SCRIPS:
            if not scrip.get("tradeable"):
                continue
            sym = scrip["symbol"]
            q = quotes.get(sym)
            if not q or q.get("ltp") is None:
                continue
            ltp = float(q["ltp"])
            prev = _auto_state["last_ltp"].get(sym)

            # Exit check first
            open_t = open_by_sym.get(sym)
            if open_t:
                reason = _auto_check_exit(open_t, q, ltp)
                if reason:
                    _auto_close(open_t, ltp, now, reason)
                    modified = True
                    open_by_sym.pop(sym, None)
                    _auto_state["last_ltp"][sym] = ltp
                    continue  # wait next tick before re-entering

            # Entry check
            if sym not in open_by_sym and counts.get(sym, 0) < AUTO_MAX_TRADES_PER_SCRIP:
                if prev is not None:
                    side = _auto_check_entry(q, prev, ltp)
                    if side:
                        _auto_open(sym, side, AUTO_QTY, ltp, now, trades)
                        modified = True
                        counts[sym] = counts.get(sym, 0) + 1

            _auto_state["last_ltp"][sym] = ltp

        if modified:
            write_paper_trades(trades)


# ---------- Option auto-strategy (paper) ----------
# Same Gann-level crossing concept as stock strategy, applied to index options:
#   - Index spot crosses BUY level UP    -> paper BUY 1 lot ATM CE
#   - Index spot crosses SELL level DOWN -> paper BUY 1 lot ATM PE
# Exits:
#   - CE: spot >= T1 (target) OR spot < SELL level (SL) OR 15:15 square-off
#   - PE: spot <= S1 (target) OR spot > BUY level  (SL) OR 15:15 square-off
# Entry price / exit price = option LTP at that moment (paper).
AUTO_OPTION_STRATEGY_ENABLED = True

_option_auto_state = {
    "last_spot": {},  # index_name -> last seen spot
    "lock": threading.Lock(),
}


def option_auto_strategy_tick(option_data, option_index_meta):
    """Called after each /api/option-prices fetch.
    option_data: {key: {index, strike, option_type, ltp, ...}}
    option_index_meta: {index_name: {spot, atm, expiry, ...}}"""
    if not AUTO_OPTION_STRATEGY_ENABLED or not option_index_meta:
        return
    now = now_ist()
    gann_quotes, _ = fetch_quotes()

    with _option_auto_state["lock"]:
        trades = read_paper_trades()
        modified = False

        # 1. Square off option positions at/after 15:15
        if _auto_at_or_after_squareoff(now):
            for t in trades:
                if t.get("status") != "OPEN" or t.get("asset_type") != "option":
                    continue
                q = option_data.get(t.get("option_key"))
                if not q or q.get("ltp") is None:
                    continue
                _auto_close(t, float(q["ltp"]), now, "AUTO_SQUARE_OFF")
                modified = True
            if modified:
                write_paper_trades(trades)
            return

        if not _auto_in_hours(now):
            return

        today = now.strftime("%Y-%m-%d")
        counts = {}
        for t in trades:
            if t.get("date") == today and t.get("asset_type") == "option":
                u = t.get("underlying", "")
                counts[u] = counts.get(u, 0) + 1
        open_by_underlying = {
            t["underlying"]: t for t in trades
            if t.get("status") == "OPEN" and t.get("asset_type") == "option"
        }

        for idx_name, m in option_index_meta.items():
            spot = m.get("spot")
            atm  = m.get("atm")
            if spot is None or atm is None:
                continue
            gann_sym = INDEX_OPTIONS_CONFIG[idx_name]["spot_symbol_key"]
            gq = gann_quotes.get(gann_sym) or {}
            levels  = gq.get("levels") or {}
            buy_lvl = (levels.get("buy")  or {}).get("BUY")
            sell_lvl= (levels.get("sell") or {}).get("SELL")
            t1_lvl  = (levels.get("buy")  or {}).get("T1")
            s1_lvl  = (levels.get("sell") or {}).get("S1")
            prev_spot = _option_auto_state["last_spot"].get(idx_name)

            # Exit open position (based on spot level hits)
            open_t = open_by_underlying.get(idx_name)
            if open_t:
                opt_q = option_data.get(open_t.get("option_key"))
                opt_ltp = (opt_q or {}).get("ltp")
                reason = None
                if open_t.get("option_type") == "CE":
                    if t1_lvl is not None and spot >= t1_lvl:     reason = "TARGET_T1"
                    elif sell_lvl is not None and spot < sell_lvl: reason = "SL_SELL_LVL"
                else:  # PE
                    if s1_lvl is not None and spot <= s1_lvl:     reason = "TARGET_S1"
                    elif buy_lvl is not None and spot > buy_lvl:  reason = "SL_BUY_LVL"
                if reason and opt_ltp is not None:
                    _auto_close(open_t, float(opt_ltp), now, reason)
                    modified = True
                    open_by_underlying.pop(idx_name, None)
                    _option_auto_state["last_spot"][idx_name] = spot
                    continue

            # Entry check — spot crossing
            if (idx_name not in open_by_underlying
                    and counts.get(idx_name, 0) < AUTO_MAX_TRADES_PER_SCRIP
                    and prev_spot is not None):
                option_type = None
                if buy_lvl is not None and prev_spot <= buy_lvl < spot:
                    option_type = "CE"
                elif sell_lvl is not None and prev_spot >= sell_lvl > spot:
                    option_type = "PE"

                if option_type:
                    opt_key = f"{idx_name} {atm} {option_type}"
                    opt_q = option_data.get(opt_key)
                    opt_ltp = (opt_q or {}).get("ltp")
                    if opt_ltp is not None:
                        t = {
                            "id": _next_paper_id(trades),
                            "date": now.strftime("%Y-%m-%d"),
                            "scrip": opt_key,
                            "option_key": opt_key,
                            "asset_type": "option",
                            "underlying": idx_name,
                            "strike": atm,
                            "option_type": option_type,
                            "expiry": m.get("expiry"),
                            "order_type": "BUY",
                            "entry_time": now.strftime("%H:%M:%S"),
                            "entry_ts": now.timestamp(),
                            "entry_price": round(float(opt_ltp), 2),
                            "qty": 1,
                            "trigger_spot": round(float(spot), 2),
                            "trigger_level": "BUY" if option_type == "CE" else "SELL",
                            "max_min_target_price": round(float(opt_ltp), 2),
                            "target_level_reached": None,
                            "exit_time": None, "exit_ts": None, "exit_price": None,
                            "exit_reason": None, "pnl_points": None, "pnl_pct": None,
                            "duration_seconds": None,
                            "status": "OPEN",
                            "auto": True,
                        }
                        trades.insert(0, t)
                        modified = True
                        counts[idx_name] = counts.get(idx_name, 0) + 1

            _option_auto_state["last_spot"][idx_name] = spot

        if modified:
            write_paper_trades(trades)


# ---------- HTML template (single-file, dark theme) ----------
PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Kotak Neo Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, 'Segoe UI', sans-serif;
    background: #0f1419; color: #d4d4d8; margin: 0; padding: 20px;
  }
  header {
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 2px solid #27272a; padding-bottom: 12px; margin-bottom: 20px;
  }
  h1 { margin: 0; color: #fafafa; font-size: 24px; }
  .status { font-size: 12px; color: #71717a; }
  .status .ok { color: #4ade80; }
  .status .err { color: #f87171; }
  .tabs { display: flex; gap: 4px; margin-bottom: 16px; flex-wrap: wrap; }
  .tabs a {
    padding: 8px 16px; background: #18181b; color: #a1a1aa;
    text-decoration: none; border-radius: 6px; font-size: 14px;
    border: 1px solid #27272a;
  }
  .tabs a.active { background: #2563eb; color: white; border-color: #2563eb; }
  .tabs a:hover:not(.active) { background: #27272a; color: #fafafa; }
  .refresh {
    padding: 8px 16px; background: #16a34a; color: white; border: none;
    border-radius: 6px; cursor: pointer; font-size: 14px; margin-left: auto;
  }
  .refresh:hover { background: #15803d; }
  .card {
    background: #18181b; border: 1px solid #27272a;
    border-radius: 8px; padding: 20px; overflow-x: auto;
  }
  h2 { margin-top: 0; color: #fafafa; font-size: 18px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    text-align: left; padding: 10px 12px; background: #27272a;
    color: #a1a1aa; font-weight: 600; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.5px;
    border-bottom: 1px solid #3f3f46;
  }
  td {
    padding: 10px 12px; border-bottom: 1px solid #27272a;
    color: #d4d4d8;
  }
  tr:hover td { background: #1f1f23; }
  .empty { color: #71717a; font-style: italic; padding: 20px 0; }
  .error {
    background: #450a0a; border: 1px solid #7f1d1d; color: #fca5a5;
    padding: 12px; border-radius: 6px; font-family: monospace; font-size: 12px;
    white-space: pre-wrap; word-break: break-word;
  }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .pos { color: #4ade80; }
  .neg { color: #f87171; }
  .banner {
    padding: 14px 18px; border-radius: 8px; margin-bottom: 16px;
    font-size: 14px; display: flex; align-items: center; gap: 12px;
  }
  .banner.ok { background: #052e16; border: 1px solid #166534; color: #86efac; }
  .banner.bad { background: #450a0a; border: 1px solid #7f1d1d; color: #fca5a5; }
  .banner .big { font-size: 16px; font-weight: 600; }
  .badge-ok { color: #4ade80; font-weight: 600; }
  .badge-bad { color: #f87171; font-weight: 600; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Kotak Neo Dashboard</h1>
    <div class="status">
      {% if greeting %}Welcome, <span class="ok">{{ greeting }}</span> |{% endif %}
      {% if login_time %}Logged in at {{ login_time }}{% endif %}
      {% if error %}<span class="err">Login error: {{ error }}</span>{% endif %}
    </div>
  </div>
  <form method="post" action="/refresh" style="margin:0">
    <button class="refresh" type="submit">Refresh Login</button>
  </form>
</header>

<div class="tabs">
  {% for t in tabs %}
    <a href="{{ t.url }}" class="{% if t.key == active %}active{% endif %}">{{ t.label }}</a>
  {% endfor %}
</div>

{% if login_time %}
  <div class="banner ok">
    <div class="big">Auto-login successful</div>
    <div>Logged in at <strong>{{ login_time }}</strong> as <strong>{{ greeting }}</strong> — session active</div>
  </div>
{% elif error %}
  <div class="banner bad">
    <div class="big">Login failed</div>
    <div>{{ error }}</div>
  </div>
{% endif %}

<div class="card">
  <h2>{{ heading }}</h2>
  {% if view_error %}
    <div class="error">{{ view_error }}</div>
  {% elif rows and rows|length > 0 %}
    <table>
      <thead>
        <tr>{% for c in cols %}<th>{{ c }}</th>{% endfor %}</tr>
      </thead>
      <tbody>
        {% for r in rows %}
          <tr>{% for c in cols %}<td>{{ r.get(c, '') }}</td>{% endfor %}</tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <div class="empty">No records found.</div>
  {% endif %}
</div>

</body>
</html>
"""

TABS = [
    {"key": "gann", "url": "/gann", "label": "Gann Trader"},
    {"key": "options", "url": "/options", "label": "Options"},
    {"key": "holdings", "url": "/", "label": "Holdings"},
    {"key": "positions", "url": "/positions", "label": "Positions"},
    {"key": "orders", "url": "/orders", "label": "Orders"},
    {"key": "trades", "url": "/trades", "label": "Trades"},
    {"key": "limits", "url": "/limits", "label": "Limits"},
    {"key": "orderlog", "url": "/orderlog", "label": "Order Log"},
    {"key": "history", "url": "/history", "label": "Login History"},
]


GANN_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Gann Trader - Kotak Neo</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif;
         background: #f5f6f8; color: #1f2937; margin: 0; padding: 16px; }
  header { display: flex; justify-content: space-between; align-items: center;
           background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
           padding: 12px 16px; margin-bottom: 12px; }
  .title { font-weight: 700; font-size: 18px; }
  .live { color: #16a34a; font-size: 12px; margin-left: 8px; }
  .stale { color: #dc2626; font-size: 12px; margin-left: 8px; }
  .clock { font-family: monospace; background: #f3f4f6; padding: 6px 10px;
           border-radius: 6px; font-size: 14px; }
  .ucc { background: #f3f4f6; padding: 6px 10px; border-radius: 6px;
         font-size: 13px; color: #374151; }
  .tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
  .tabs a { padding: 6px 12px; background: #fff; color: #6b7280;
            text-decoration: none; border-radius: 6px; font-size: 13px;
            border: 1px solid #e5e7eb; }
  .tabs a.active { background: #2563eb; color: white; border-color: #2563eb; }
  .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
          overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; min-width: 1400px; }
  th { text-align: center; padding: 8px 6px; background: #f9fafb;
       color: #6b7280; font-weight: 600; font-size: 10px;
       text-transform: uppercase; border-bottom: 1px solid #e5e7eb;
       position: sticky; top: 0; }
  td { padding: 8px 6px; border-bottom: 1px solid #f3f4f6;
       color: #1f2937; text-align: center; font-variant-numeric: tabular-nums; }
  td.scrip { text-align: left; font-weight: 600; }
  td.pnl { background: #dcfce7; color: #166534; font-weight: 600; }
  td.pnl.neg { background: #fee2e2; color: #991b1b; }
  th.group-sell { background: #6b7280; color: #fff; }
  th.group-buy  { background: #6b7280; color: #fff; }
  th.group-ltp  { background: #1f2937; color: #fff; }
  /* LTP cell gets coloured based on nearest gann level */
  td.ltp { font-weight: 700; background: #f9fafb; font-size: 13px;
           transition: background 0.3s, color 0.3s; }
  td.ltp.lvl-S3      { background: #B71C1C; color: #fff; }
  td.ltp.lvl-S2      { background: #C62828; color: #fff; }
  td.ltp.lvl-S1      { background: #D32F2F; color: #fff; }
  td.ltp.lvl-SELL_WA { background: #FF9800; color: #fff; }
  td.ltp.lvl-SELL    { background: #EF9A9A; color: #7f1d1d; }
  td.ltp.lvl-BUY     { background: #A5D6A7; color: #14532d; }
  td.ltp.lvl-BUY_WA  { background: #FF9800; color: #fff; }
  td.ltp.lvl-T1      { background: #81C784; color: #fff; }
  td.ltp.lvl-T2      { background: #66BB6A; color: #fff; }
  td.ltp.lvl-T3      { background: #388E3C; color: #fff; }
  /* Header counter + toggle */
  .counter { display: inline-flex; gap: 10px; align-items: center;
             background: #f3f4f6; padding: 6px 12px; border-radius: 6px;
             font-size: 12px; }
  .counter b { font-family: monospace; font-size: 13px; }
  .counter .c-active { color: #2563eb; }
  .counter .c-closed { color: #6b7280; }
  .counter .c-pnl-pos { color: #16a34a; }
  .counter .c-pnl-neg { color: #dc2626; }
  .paper-toggle { display: inline-flex; align-items: center; gap: 8px;
                  background: #16a34a; color: #fff; padding: 6px 10px;
                  border-radius: 16px; font-size: 12px; font-weight: 600; }
  .dot-on  { width:10px; height:10px; border-radius:50%; background:#fff; }
  .open-pos-bar { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
                  padding: 10px 12px; margin-bottom: 12px; font-size: 12px; }
  .open-pos-bar .head { font-weight: 600; color: #374151; margin-bottom: 6px; }
  .open-pos-row { display: grid;
                  grid-template-columns: 90px 50px 70px 100px 100px 100px 80px 80px;
                  gap: 8px; align-items: center; padding: 4px 0;
                  border-bottom: 1px solid #f3f4f6; }
  .open-pos-row:last-child { border-bottom: none; }
  .open-pos-row .scr { font-weight: 700; }
  .pill-sm { padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 700;
             display: inline-block; }
  .pill-sm.BUY  { background: #dcfce7; color: #166534; }
  .pill-sm.SELL { background: #fee2e2; color: #991b1b; }
  .pnl-pos { color: #16a34a; font-weight: 600; }
  .pnl-neg { color: #dc2626; font-weight: 600; }
  .btn-close { background: #1f2937; color: #fff; border: none; border-radius: 4px;
               padding: 4px 10px; font-size: 11px; cursor: pointer; }
  .btn-close:hover { background: #374151; }
  .err-banner { background: #fef3c7; border: 1px solid #fbbf24; color: #78350f;
                padding: 10px 14px; border-radius: 8px; margin-bottom: 12px;
                font-size: 13px; }
  .ok-banner  { background: #dcfce7; border: 1px solid #16a34a; color: #14532d;
                padding: 10px 14px; border-radius: 8px; margin-bottom: 12px;
                font-size: 13px; }
  .btn-buy  { background:#16a34a; color:#fff; border:none; border-radius:4px;
              padding:4px 10px; font-size:11px; font-weight:700;
              cursor:pointer; margin-right:4px; }
  .btn-buy:hover  { background:#15803d; }
  .btn-sell { background:#dc2626; color:#fff; border:none; border-radius:4px;
              padding:4px 10px; font-size:11px; font-weight:700;
              cursor:pointer; }
  .btn-sell:hover { background:#b91c1c; }
  /* modal */
  .modal-bg { position:fixed; inset:0; background:rgba(0,0,0,0.5);
              display:none; align-items:center; justify-content:center;
              z-index:1000; }
  .modal-bg.show { display:flex; }
  .modal { background:#fff; border-radius:10px; padding:0; width:420px;
           max-width:95vw; box-shadow:0 20px 50px rgba(0,0,0,0.3); }
  .modal-head { padding:14px 18px; border-bottom:1px solid #e5e7eb;
                display:flex; justify-content:space-between; align-items:center; }
  .modal-head .sym { font-weight:700; font-size:16px; }
  .modal-head .ltp { font-family:monospace; color:#6b7280; }
  .modal-head .pill { padding:3px 10px; border-radius:12px; color:#fff;
                      font-size:11px; font-weight:700; }
  .pill-B { background:#16a34a; } .pill-S { background:#dc2626; }
  .modal-body { padding:14px 18px; }
  .row { display:flex; gap:10px; margin-bottom:10px; align-items:center; }
  .row label { width:90px; font-size:12px; color:#374151; }
  .row input[type=number], .row input[type=password] {
    flex:1; padding:6px 10px; border:1px solid #d1d5db; border-radius:6px;
    font-size:14px; font-family:monospace; }
  .seg { display:flex; gap:4px; flex:1; }
  .seg label { width:auto; flex:1; padding:6px 10px; border:1px solid #d1d5db;
               border-radius:6px; text-align:center; font-size:12px;
               cursor:pointer; background:#fff; }
  .seg input { display:none; }
  .seg input:checked + span { font-weight:700; }
  .seg label:has(input:checked) { background:#dbeafe; border-color:#2563eb; color:#1e40af; }
  .summary { background:#f9fafb; padding:10px 14px; border-radius:6px;
             margin-top:8px; font-size:13px; }
  .summary .total { font-size:16px; font-weight:700; color:#1f2937; }
  .summary .avail { font-size:12px; color:#6b7280; margin-top:4px; }
  .summary .insuf { color:#dc2626; font-weight:600; }
  .modal-foot { padding:12px 18px; border-top:1px solid #e5e7eb;
                display:flex; gap:10px; justify-content:flex-end; }
  .btn-cancel { padding:8px 16px; background:#f3f4f6; color:#374151;
                border:none; border-radius:6px; cursor:pointer; font-size:13px; }
  .btn-confirm { padding:8px 18px; color:#fff; border:none; border-radius:6px;
                 cursor:pointer; font-size:13px; font-weight:700; }
  .btn-confirm.B { background:#16a34a; } .btn-confirm.B:hover { background:#15803d; }
  .btn-confirm.S { background:#dc2626; } .btn-confirm.S:hover { background:#b91c1c; }
  .modal-msg { padding:10px 14px; margin-top:8px; border-radius:6px;
               font-size:13px; display:none; }
  .modal-msg.ok  { background:#dcfce7; color:#14532d; display:block; }
  .modal-msg.err { background:#fee2e2; color:#7f1d1d; display:block; }
</style>
</head>
<body>
<header>
  <div>
    <span class="title">Gann Trader</span>
    <span id="livedot" class="live">● Live</span>
  </div>
  <div style="display:flex; gap:8px; align-items:center; flex-wrap: wrap;">
    <span class="counter">
      <span>Active: <b class="c-active" id="statActive">0</b></span>
      <span>Closed: <b class="c-closed" id="statClosed">0</b></span>
      <span>P&amp;L: <b id="statPnl">+0.00</b></span>
    </span>
    <span class="paper-toggle" title="Paper Trading mode (no real Kotak orders)">
      <span class="dot-on"></span> Paper Trading
    </span>
    <span class="clock" id="clock">--:--:--</span>
    <span class="ucc">UCC: {{ ucc }}</span>
    <form method="post" action="/refresh" style="margin:0">
      <button style="padding:6px 12px;background:#16a34a;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px;">Refresh Login</button>
    </form>
  </div>
</header>

<div class="tabs">
  {% for t in tabs %}
    <a href="{{ t.url }}" class="{% if t.key == active %}active{% endif %}">{{ t.label }}</a>
  {% endfor %}
</div>

<div id="errbox"></div>

<!-- Open paper positions -->
<div class="open-pos-bar" id="openPosBar" style="display:none;">
  <div class="head">Open Positions</div>
  <div id="openPosRows"></div>
</div>

<div class="card">
<table>
  <thead>
    <tr>
      <th rowspan="2">SCRIP</th>
      <th rowspan="2">P&amp;L</th>
      <th rowspan="2">LIVE P&amp;L</th>
      <th rowspan="2">QTY</th>
      <th rowspan="2">OPEN</th>
      <th rowspan="2">LOW</th>
      <th rowspan="2">HIGH</th>
      <th colspan="5" class="group-sell">SELL LEVELS</th>
      <th rowspan="2" class="group-ltp">LTP</th>
      <th colspan="5" class="group-buy">BUY LEVELS</th>
    </tr>
    <tr>
      <th>S3</th><th>S2</th><th>S1</th><th>WA</th><th>SELL</th>
      <th>BUY</th><th>WA</th><th>T1</th><th>T2</th><th>T3</th>
    </tr>
  </thead>
  <tbody id="rows">
    {% for s in scrips %}
    <tr data-symbol="{{ s.symbol }}" data-tradeable="{{ '1' if s.tradeable else '0' }}">
      <td class="scrip">{{ s.symbol }}</td>
      <td class="pnl" data-col="pnl">+0.00</td>
      <td class="pnl" data-col="livepnl">+0.00</td>
      <td data-col="qty">0</td>
      <td data-col="open">-</td>
      <td data-col="low">-</td>
      <td data-col="high">-</td>
      <td data-col="S3">-</td>
      <td data-col="S2">-</td>
      <td data-col="S1">-</td>
      <td data-col="SELL_WA">-</td>
      <td data-col="SELL">-</td>
      <td class="ltp" data-col="ltp">-</td>
      <td data-col="BUY">-</td>
      <td data-col="BUY_WA">-</td>
      <td data-col="T1">-</td>
      <td data-col="T2">-</td>
      <td data-col="T3">-</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
</div>

<!-- Order ticket modal -->
<div class="modal-bg" id="modalBg">
  <div class="modal">
    <div class="modal-head">
      <div>
        <span class="pill" id="mPill">B</span>
        <span class="sym" id="mSym">SYMBOL</span>
      </div>
      <div class="ltp">LTP <span id="mLtp">-</span></div>
    </div>
    <div class="modal-body">
      <div class="row">
        <label>Quantity</label>
        <input type="number" id="mQty" value="1" min="1" step="1">
      </div>
      <div class="summary">
        <div>Paper trade — fills instantly at current LTP</div>
        <div class="avail">Entry price <span id="mEntryPx">-</span> &nbsp;·&nbsp; Est. value <span id="mTotal">₹0.00</span></div>
      </div>
      <div class="modal-msg" id="mMsg"></div>
    </div>
    <div class="modal-foot">
      <button class="btn-cancel" onclick="closeTicket()">Cancel</button>
      <button class="btn-confirm B" id="mConfirm" onclick="submitTicket()">Confirm BUY</button>
    </div>
  </div>
</div>

<script>
const LEVEL_COLORS = {{ level_colors|tojson }};
const SELL_LVLS = ["S3","S2","S1","SELL_WA","SELL"];
const BUY_LVLS  = ["BUY","BUY_WA","T1","T2","T3"];

function fmt(v) {
  if (v === null || v === undefined || v === "") return "-";
  const n = Number(v);
  if (!isFinite(n)) return "-";
  return n.toFixed(2);
}

// Paint a level cell as plain text (no colouring — only LTP cell gets coloured)
function paintCell(td, level, value, ltp) {
  if (!td) return;
  if (value === null || value === undefined) {
    td.textContent = "-";
  } else {
    td.textContent = fmt(value);
  }
}

const LTP_LEVEL_CLASSES = [
  "lvl-S3","lvl-S2","lvl-S1","lvl-SELL_WA","lvl-SELL",
  "lvl-BUY","lvl-BUY_WA","lvl-T1","lvl-T2","lvl-T3",
];

function paintLtpCell(td, nearest) {
  if (!td) return;
  td.classList.remove(...LTP_LEVEL_CLASSES);
  if (nearest) td.classList.add("lvl-" + nearest);
}

function updateStats(stats) {
  if (!stats) return;
  document.getElementById("statActive").textContent = stats.active || 0;
  document.getElementById("statClosed").textContent = stats.closed || 0;
  const pnlEl = document.getElementById("statPnl");
  const p = Number(stats.pnl || 0);
  pnlEl.textContent = (p >= 0 ? "+" : "") + p.toFixed(2);
  pnlEl.className = p >= 0 ? "c-pnl-pos" : "c-pnl-neg";
}

async function refresh() {
  try {
    const r = await fetch("/api/gann-prices");
    const data = await r.json();
    const ebox = document.getElementById("errbox");
    if (data.error) {
      ebox.innerHTML = '<div class="err-banner">Data error: ' + data.error + '</div>';
    } else {
      ebox.innerHTML = "";
    }
    document.getElementById("livedot").className = data.error ? "stale" : "live";
    document.getElementById("livedot").textContent = data.error ? "● Stale" : "● Live";
    updateStats(data.stats);

    for (const s of data.scrips) {
      const tr = document.querySelector('tr[data-symbol="' + s.symbol + '"]');
      if (!tr) continue;
      const ltp = s.ltp;
      const ltpCell = tr.querySelector('[data-col=ltp]');
      ltpCell.textContent = fmt(ltp);
      paintLtpCell(ltpCell, s.nearest_level);
      tr.querySelector('[data-col=open]').textContent = fmt(s.open);
      tr.querySelector('[data-col=low]').textContent  = fmt(s.low);
      tr.querySelector('[data-col=high]').textContent = fmt(s.high);
      const sell = (s.levels && s.levels.sell) || {};
      const buy  = (s.levels && s.levels.buy)  || {};
      for (const lvl of SELL_LVLS) {
        paintCell(tr.querySelector('[data-col=' + lvl + ']'), lvl, sell[lvl], ltp);
      }
      for (const lvl of BUY_LVLS) {
        paintCell(tr.querySelector('[data-col=' + lvl + ']'), lvl, buy[lvl], ltp);
      }
    }
    refreshOpenPositions(data);
  } catch (e) {
    document.getElementById("livedot").className = "stale";
    document.getElementById("livedot").textContent = "● Offline";
  }
}

function tickClock() {
  const now = new Date();
  const ist = new Date(now.getTime() + (now.getTimezoneOffset() + 330) * 60000);
  const hh = String(ist.getHours()).padStart(2,'0');
  const mm = String(ist.getMinutes()).padStart(2,'0');
  const ss = String(ist.getSeconds()).padStart(2,'0');
  document.getElementById("clock").textContent = hh+":"+mm+":"+ss + " IST";
}

// ---- Order ticket ----
let _ticketSide = "B";
let _ticketSymbol = "";
let _availMargin = null;

function fmtRs(n) {
  if (n === null || n === undefined || !isFinite(n)) return "-";
  return "₹" + Number(n).toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function getRowLtp(symbol) {
  const tr = document.querySelector('tr[data-symbol="' + symbol + '"]');
  if (!tr) return null;
  const t = tr.querySelector('[data-col=ltp]').textContent;
  const n = parseFloat(t);
  return isFinite(n) ? n : null;
}

function recalcTotal() {
  const qty = parseInt(document.getElementById('mQty').value, 10) || 0;
  const ltp = getRowLtp(_ticketSymbol) || 0;
  document.getElementById('mEntryPx').textContent = ltp ? ltp.toFixed(2) : '-';
  document.getElementById('mTotal').textContent = fmtRs(qty * ltp);
}

function openTicket(symbol, side) {
  _ticketSymbol = symbol;
  _ticketSide = side;
  document.getElementById('mSym').textContent = symbol;
  const pill = document.getElementById('mPill');
  pill.textContent = side === 'B' ? 'BUY' : 'SELL';
  pill.className = 'pill pill-' + side;
  const conf = document.getElementById('mConfirm');
  conf.textContent = side === 'B' ? 'Confirm BUY' : 'Confirm SELL';
  conf.className = 'btn-confirm ' + side;
  conf.disabled = false;
  const ltp = getRowLtp(symbol);
  document.getElementById('mLtp').textContent = ltp !== null ? ltp.toFixed(2) : '-';
  document.getElementById('mQty').value = 1;
  document.getElementById('mMsg').className = 'modal-msg';
  document.getElementById('mMsg').textContent = '';
  document.getElementById('modalBg').classList.add('show');
  recalcTotal();
}

function closeTicket() {
  document.getElementById('modalBg').classList.remove('show');
}

async function submitTicket() {
  const qty  = document.getElementById('mQty').value;
  const conf = document.getElementById('mConfirm');
  const msg  = document.getElementById('mMsg');
  conf.disabled = true;
  conf.textContent = 'Placing...';
  msg.className = 'modal-msg';
  msg.textContent = '';
  try {
    const r = await fetch('/api/paper-open', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        symbol: _ticketSymbol, side: _ticketSide, qty: qty,
      })
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      msg.className = 'modal-msg ok';
      msg.textContent = '✓ Paper ' + (_ticketSide === 'B' ? 'BUY' : 'SELL')
                     + ' opened @ ₹' + d.trade.entry_price;
      conf.textContent = 'Done';
      refresh();
      setTimeout(closeTicket, 1500);
    } else {
      msg.className = 'modal-msg err';
      msg.textContent = '✗ ' + (d.error || 'Failed');
      conf.disabled = false;
      conf.textContent = _ticketSide === 'B' ? 'Confirm BUY' : 'Confirm SELL';
    }
  } catch(e) {
    msg.className = 'modal-msg err';
    msg.textContent = '✗ Network error: ' + e.message;
    conf.disabled = false;
    conf.textContent = _ticketSide === 'B' ? 'Confirm BUY' : 'Confirm SELL';
  }
}

document.addEventListener('input', (e) => {
  if (e.target && e.target.id === 'mQty') recalcTotal();
});
document.getElementById('modalBg').addEventListener('click', (e) => {
  if (e.target.id === 'modalBg') closeTicket();
});

// ---- Open positions ----
async function refreshOpenPositions(quotes) {
  let tradesResp;
  try {
    tradesResp = await (await fetch('/api/paper-trades')).json();
  } catch(e) { return; }
  const open = (tradesResp.trades || []).filter(t => t.status === 'OPEN');
  const bar = document.getElementById('openPosBar');
  const rows = document.getElementById('openPosRows');
  if (open.length === 0) { bar.style.display = 'none'; rows.innerHTML = ''; return; }
  bar.style.display = '';
  const ltpBySym = {};
  for (const s of (quotes && quotes.scrips) || []) ltpBySym[s.symbol] = s.ltp;
  const out = [];
  out.push('<div class="open-pos-row" style="font-weight:600;color:#6b7280;">'
         + '<div>Scrip</div><div>Side</div><div>Qty</div>'
         + '<div>Entry</div><div>LTP</div><div>Live P&L</div>'
         + '<div>Reached</div><div></div></div>');
  for (const t of open) {
    const ltp = ltpBySym[t.scrip];
    let pnl = null;
    if (ltp !== null && ltp !== undefined) {
      pnl = t.order_type === 'BUY'
        ? (ltp - t.entry_price) * t.qty
        : (t.entry_price - ltp) * t.qty;
    }
    const pnlClass = pnl === null ? '' : (pnl >= 0 ? 'pnl-pos' : 'pnl-neg');
    const pnlStr = pnl === null ? '-' : (pnl >= 0 ? '+' : '') + pnl.toFixed(2);
    out.push('<div class="open-pos-row">'
           + '<div class="scr">' + t.scrip + '</div>'
           + '<div><span class="pill-sm ' + t.order_type + '">' + t.order_type + '</span></div>'
           + '<div>' + t.qty + '</div>'
           + '<div>₹' + t.entry_price.toFixed(2) + '</div>'
           + '<div>' + (ltp !== null && ltp !== undefined ? '₹' + ltp.toFixed(2) : '-') + '</div>'
           + '<div class="' + pnlClass + '">' + pnlStr + '</div>'
           + '<div>' + (t.target_level_reached || '-') + '</div>'
           + '<div><button class="btn-close" onclick="closeTrade(\\'' + t.id + '\\')">Close</button></div>'
           + '</div>');
  }
  rows.innerHTML = out.join('');
}

async function closeTrade(id) {
  if (!confirm('Close this paper trade at current LTP?')) return;
  try {
    const r = await fetch('/api/paper-close', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({id: id, reason: 'MANUAL'})
    });
    const d = await r.json();
    if (!(r.ok && d.ok)) alert('Failed: ' + (d.error || 'unknown'));
    refresh();
  } catch(e) { alert('Error: ' + e.message); }
}

setInterval(tickClock, 1000); tickClock();
setInterval(refresh, 2500); refresh();
</script>
</body>
</html>
"""


OPTIONS_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Options - Kotak Neo</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif;
         background: #f5f6f8; color: #1f2937; margin: 0; padding: 16px; }
  header { display: flex; justify-content: space-between; align-items: center;
           background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
           padding: 12px 16px; margin-bottom: 12px; }
  .title { font-weight: 700; font-size: 18px; }
  .live { color: #16a34a; font-size: 12px; margin-left: 8px; }
  .stale { color: #dc2626; font-size: 12px; margin-left: 8px; }
  .clock { font-family: monospace; background: #f3f4f6; padding: 6px 10px;
           border-radius: 6px; font-size: 14px; }
  .ucc { background: #f3f4f6; padding: 6px 10px; border-radius: 6px;
         font-size: 13px; color: #374151; }
  .tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
  .tabs a { padding: 6px 12px; background: #fff; color: #6b7280;
            text-decoration: none; border-radius: 6px; font-size: 13px;
            border: 1px solid #e5e7eb; }
  .tabs a.active { background: #2563eb; color: white; border-color: #2563eb; }
  .idx-tabs { display: flex; gap: 6px; margin-bottom: 12px; }
  .idx-tabs button { padding: 8px 16px; background: #fff; color: #374151;
                     border: 1px solid #e5e7eb; border-radius: 6px;
                     cursor: pointer; font-size: 13px; font-weight: 600; }
  .idx-tabs button.active { background: #1f2937; color: #fff;
                            border-color: #1f2937; }
  .meta-bar { display: flex; gap: 18px; padding: 12px 16px; background: #fff;
              border: 1px solid #e5e7eb; border-radius: 8px; margin-bottom: 8px;
              font-size: 13px; flex-wrap: wrap; align-items: center; }
  .meta-bar .m { display: flex; gap: 6px; align-items: baseline; }
  .meta-bar .k { color: #6b7280; font-size: 11px; text-transform: uppercase; }
  .meta-bar .v { font-weight: 700; font-family: monospace; font-size: 14px; }
  .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
          overflow-x: auto; }
  table.chain { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.chain th { text-align: center; padding: 10px 8px; background: #f9fafb;
       color: #6b7280; font-weight: 600; font-size: 11px;
       text-transform: uppercase; border-bottom: 1px solid #e5e7eb; }
  table.chain th.ce-grp { background: #065f46; color: #fff; }
  table.chain th.pe-grp { background: #991b1b; color: #fff; }
  table.chain th.strike-col { background: #1f2937; color: #fff; }
  table.chain td { padding: 10px 8px; border-bottom: 1px solid #f3f4f6;
       color: #1f2937; text-align: center;
       font-variant-numeric: tabular-nums; }
  table.chain td.strike { font-weight: 700; background: #f3f4f6;
                          font-family: monospace; font-size: 14px; }
  table.chain td.ltp-ce, table.chain td.ltp-pe {
       font-weight: 700; font-size: 14px; }
  table.chain td.ltp-ce { background: #d1fae5; color: #065f46; }
  table.chain td.ltp-pe { background: #fee2e2; color: #991b1b; }
  table.chain tr.atm td { background: #fffbeb !important; }
  table.chain tr.atm td.strike { background: #f59e0b !important; color: #fff; }
  table.chain td.chg-pos { color: #16a34a; font-weight: 600; }
  table.chain td.chg-neg { color: #dc2626; font-weight: 600; }
  .err { background: #fee2e2; color: #991b1b; padding: 8px 12px;
         border-radius: 6px; font-size: 12px; margin-bottom: 10px;
         display: none; }
  .err.show { display: block; }
  .hint { color: #6b7280; font-size: 11px; padding: 10px 16px; }
  .loading { padding: 60px; text-align: center; color: #6b7280; font-size: 13px; }
</style>
</head>
<body>
<header>
  <div>
    <span class="title">Options Chain</span>
    <span id="live" class="live">● live</span>
  </div>
  <div style="display:flex;gap:10px;align-items:center;">
    <span class="clock" id="clock">00:00:00 IST</span>
    {% if ucc %}<span class="ucc">UCC: {{ ucc }}</span>{% endif %}
  </div>
</header>

<div class="tabs">
  {% for t in tabs %}
    <a href="{{ t.url }}" class="{{ 'active' if t.key == active else '' }}">{{ t.label }}</a>
  {% endfor %}
</div>

<div class="idx-tabs" id="idxTabs">
  {% for i in indices %}
    <button data-idx="{{ i.key }}" class="{{ 'active' if loop.first else '' }}">{{ i.label }}</button>
  {% endfor %}
</div>

<div class="err" id="err"></div>

<div class="meta-bar" id="metaBar">
  <div class="m"><span class="k">Spot</span><span class="v" id="mSpot">-</span></div>
  <div class="m"><span class="k">ATM</span><span class="v" id="mAtm">-</span></div>
  <div class="m"><span class="k">Expiry</span><span class="v" id="mExpiry">-</span></div>
</div>

<div class="card">
  <table class="chain" id="chainTable">
    <thead>
      <tr>
        <th class="ce-grp" colspan="2">CALL (CE)</th>
        <th class="strike-col" rowspan="2">Strike</th>
        <th class="pe-grp" colspan="2">PUT (PE)</th>
      </tr>
      <tr>
        <th>LTP</th><th>Chg %</th>
        <th>LTP</th><th>Chg %</th>
      </tr>
    </thead>
    <tbody id="chainBody">
      <tr><td colspan="5" class="loading">Loading option chain…</td></tr>
    </tbody>
  </table>
</div>

<div class="hint" id="hint">Select an index above. Auto-refreshes every 3s.</div>

<div style="margin-top:24px;">
  <h3 style="margin:0 0 8px 0; font-size:15px;">Auto Option Trades (paper) — today</h3>
  <div style="font-size:12px; color:#666; margin-bottom:8px;">
    Strategy auto-buys ATM CE when index spot crosses Gann BUY level up, or ATM PE when spot crosses SELL level down. Exits at T1/S1 target or opposite-level SL. Squares off at 15:15.
  </div>
  <table style="width:100%; border-collapse:collapse; font-size:13px;">
    <thead>
      <tr style="background:#f5f5f5; text-align:left;">
        <th style="padding:6px;">Time</th>
        <th style="padding:6px;">Underlying</th>
        <th style="padding:6px;">Contract</th>
        <th style="padding:6px;">Trigger</th>
        <th style="padding:6px; text-align:right;">Entry</th>
        <th style="padding:6px; text-align:right;">Current / Exit</th>
        <th style="padding:6px; text-align:right;">P&amp;L pts</th>
        <th style="padding:6px;">Status</th>
      </tr>
    </thead>
    <tbody id="tradesBody">
      <tr><td colspan="8" style="padding:12px; text-align:center; color:#888;">Monitoring index spot crossings…</td></tr>
    </tbody>
  </table>
</div>

<script>
let currentIdx = "{{ indices[0].key if indices else '' }}";
let lastData = null;

document.getElementById('idxTabs').addEventListener('click', (e) => {
  if (e.target.tagName !== 'BUTTON') return;
  const idx = e.target.dataset.idx;
  document.querySelectorAll('#idxTabs button').forEach(b =>
    b.classList.toggle('active', b.dataset.idx === idx));
  currentIdx = idx;
  render(lastData);
});

function fmt(n, dp) {
  if (n === null || n === undefined || isNaN(n)) return '-';
  return Number(n).toFixed(dp === undefined ? 2 : dp);
}
function fmtChg(c) {
  if (c === null || c === undefined) return {text: '-', cls: ''};
  const sign = c >= 0 ? '+' : '';
  return {text: sign + c.toFixed(2) + '%', cls: c >= 0 ? 'chg-pos' : 'chg-neg'};
}

function render(d) {
  if (!d) return;
  const body = document.getElementById('chainBody');
  if (d.loading) {
    body.innerHTML = '<tr><td colspan="5" class="loading">Warming option universe (first load ~60-90s on cold start)…</td></tr>';
    return;
  }
  const chain = d.chains && d.chains[currentIdx];
  if (!chain) { body.innerHTML = '<tr><td colspan="5" class="loading">No data</td></tr>'; return; }
  document.getElementById('mSpot').textContent = fmt(chain.spot);
  document.getElementById('mAtm').textContent = chain.atm ?? '-';
  document.getElementById('mExpiry').textContent = chain.expiry || '-';
  if (chain.error) {
    document.getElementById('err').textContent = currentIdx + ': ' + chain.error;
    document.getElementById('err').classList.add('show');
  } else {
    document.getElementById('err').classList.remove('show');
  }
  const rows = chain.rows || [];
  if (rows.length === 0) {
    body.innerHTML = '<tr><td colspan="5" class="loading">Resolving option chain (first load may take 5-10s)…</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => {
    const ce = r.ce || {};
    const pe = r.pe || {};
    const ceChg = fmtChg(ce.change_pct);
    const peChg = fmtChg(pe.change_pct);
    return `<tr class="${r.is_atm ? 'atm' : ''}">
      <td class="ltp-ce">${fmt(ce.ltp)}</td>
      <td class="${ceChg.cls}">${ceChg.text}</td>
      <td class="strike">${r.strike}</td>
      <td class="ltp-pe">${fmt(pe.ltp)}</td>
      <td class="${peChg.cls}">${peChg.text}</td>
    </tr>`;
  }).join('');
  renderTrades(d.option_trades || []);
}

function renderTrades(trades) {
  const tb = document.getElementById('tradesBody');
  if (!tb) return;
  if (!trades.length) {
    tb.innerHTML = '<tr><td colspan="8" style="padding:12px; text-align:center; color:#888;">Monitoring index spot crossings…</td></tr>';
    return;
  }
  tb.innerHTML = trades.map(t => {
    const isOpen = t.status === 'OPEN';
    const cur = isOpen ? (t.live_ltp ?? '-') : (t.exit_price ?? '-');
    const pnl = isOpen ? (t.live_pnl_points ?? 0) : (t.pnl_points ?? 0);
    const pnlCls = pnl > 0 ? 'chg-pos' : (pnl < 0 ? 'chg-neg' : '');
    const reason = isOpen ? '—' : (t.exit_reason || '');
    const contract = `${t.strike} ${t.option_type}`;
    const statusHtml = isOpen
      ? '<span style="color:#1976d2; font-weight:600;">OPEN</span>'
      : `<span style="color:#666;">CLOSED</span> <span style="font-size:11px; color:#888;">(${reason})</span>`;
    return `<tr>
      <td style="padding:6px;">${t.entry_time}${!isOpen && t.exit_time ? ' → ' + t.exit_time : ''}</td>
      <td style="padding:6px;">${t.underlying}</td>
      <td style="padding:6px;">${contract}</td>
      <td style="padding:6px; font-size:11px; color:#666;">${t.trigger_level || ''} @ ${fmt(t.trigger_spot)}</td>
      <td style="padding:6px; text-align:right;">${fmt(t.entry_price)}</td>
      <td style="padding:6px; text-align:right;">${fmt(cur)}</td>
      <td style="padding:6px; text-align:right;" class="${pnlCls}">${fmt(pnl)}</td>
      <td style="padding:6px;">${statusHtml}</td>
    </tr>`;
  }).join('');
}

async function refresh() {
  const live = document.getElementById('live');
  try {
    const r = await fetch('/api/option-prices');
    const d = await r.json();
    lastData = d;
    if (d.error) {
      live.textContent = '● stale'; live.className = 'stale';
    } else {
      live.textContent = '● live'; live.className = 'live';
    }
    render(d);
  } catch(e) {
    live.textContent = '● stale'; live.className = 'stale';
  }
}
function tickClock() {
  const now = new Date();
  const ist = new Date(now.getTime() + (now.getTimezoneOffset() + 330) * 60000);
  document.getElementById('clock').textContent =
    ist.toTimeString().slice(0, 8) + ' IST';
}
setInterval(tickClock, 1000); tickClock();
setInterval(refresh, 3000); refresh();
</script>
</body>
</html>
"""


ORDERLOG_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Order Log - Kotak Neo</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif;
         background: #f5f6f8; color: #1f2937; margin: 0; padding: 16px; }
  header { display: flex; justify-content: space-between; align-items: center;
           background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
           padding: 12px 16px; margin-bottom: 12px; }
  .title { font-weight: 700; font-size: 18px; }
  .tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
  .tabs a { padding: 6px 12px; background: #fff; color: #6b7280;
            text-decoration: none; border-radius: 6px; font-size: 13px;
            border: 1px solid #e5e7eb; }
  .tabs a.active { background: #2563eb; color: white; border-color: #2563eb; }
  .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
          padding: 12px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { text-align: left; padding: 8px; background: #f9fafb;
       color: #6b7280; font-weight: 600; font-size: 11px;
       text-transform: uppercase; border-bottom: 1px solid #e5e7eb; }
  td { padding: 8px; border-bottom: 1px solid #f3f4f6; }
  .placed { color: #166534; font-weight: 600; }
  .rejected { color: #991b1b; font-weight: 600; }
  .side-B { color: #166534; font-weight: 700; }
  .side-S { color: #991b1b; font-weight: 700; }
  .empty { color: #9ca3af; padding: 20px; text-align: center; }
  .dlbtn { padding: 6px 12px; background: #2563eb; color: #fff; border: none;
           border-radius: 6px; cursor: pointer; font-size: 13px;
           text-decoration: none; }
</style>
</head>
<body>
<header>
  <div><span class="title">Order Log</span></div>
  <a class="dlbtn" href="/orderlog.csv">Download CSV</a>
</header>

<div class="tabs">
  {% for t in tabs %}
    <a href="{{ t.url }}" class="{% if t.key == active %}active{% endif %}">{{ t.label }}</a>
  {% endfor %}
</div>

<div class="card">
{% if orders and orders|length > 0 %}
  <table>
    <thead>
      <tr>
        <th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th>
        <th>Type</th><th>Price</th><th>Product</th><th>Validity</th>
        <th>Status</th><th>Kotak Order ID</th><th>Message</th>
      </tr>
    </thead>
    <tbody>
      {% for o in orders %}
      <tr>
        <td>{{ o.timestamp }}</td>
        <td>{{ o.symbol }}</td>
        <td class="side-{{ o.side }}">{{ o.side }}</td>
        <td>{{ o.qty }}</td>
        <td>{{ o.order_type }}</td>
        <td>{{ o.price }}</td>
        <td>{{ o.product }}</td>
        <td>{{ o.validity }}</td>
        <td class="{% if o.status == 'PLACED' %}placed{% else %}rejected{% endif %}">{{ o.status }}</td>
        <td>{{ o.kotak_order_id or '-' }}</td>
        <td>{{ o.message }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
{% else %}
  <div class="empty">No orders placed yet.</div>
{% endif %}
</div>
</body>
</html>
"""


HISTORY_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Login History - Kotak Neo Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif;
         background: #0f1419; color: #d4d4d8; margin: 0; padding: 20px; }
  header { display: flex; justify-content: space-between; align-items: center;
           border-bottom: 2px solid #27272a; padding-bottom: 12px; margin-bottom: 20px; }
  h1 { margin: 0; color: #fafafa; font-size: 24px; }
  .tabs { display: flex; gap: 4px; margin-bottom: 16px; flex-wrap: wrap; }
  .tabs a { padding: 8px 16px; background: #18181b; color: #a1a1aa;
            text-decoration: none; border-radius: 6px; font-size: 14px;
            border: 1px solid #27272a; }
  .tabs a.active { background: #2563eb; color: white; border-color: #2563eb; }
  .tabs a:hover:not(.active) { background: #27272a; color: #fafafa; }
  .card { background: #18181b; border: 1px solid #27272a;
          border-radius: 8px; padding: 20px; }
  h2 { margin-top: 0; color: #fafafa; font-size: 18px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 12px; background: #27272a;
       color: #a1a1aa; font-weight: 600; font-size: 11px;
       text-transform: uppercase; letter-spacing: 0.5px; }
  td { padding: 10px 12px; border-bottom: 1px solid #27272a; color: #d4d4d8; }
  .ok { color: #4ade80; font-weight: 600; }
  .bad { color: #f87171; font-weight: 600; }
  .empty { color: #71717a; font-style: italic; padding: 20px 0; }
  .refresh { padding: 8px 16px; background: #16a34a; color: white; border: none;
             border-radius: 6px; cursor: pointer; font-size: 14px; }
</style>
</head>
<body>
<header>
  <h1>Kotak Neo Dashboard</h1>
  <form method="post" action="/refresh" style="margin:0">
    <button class="refresh" type="submit">Refresh Login</button>
  </form>
</header>
<div class="tabs">
  {% for t in tabs %}
    <a href="{{ t.url }}" class="{% if t.key == active %}active{% endif %}">{{ t.label }}</a>
  {% endfor %}
</div>
<div class="card">
  <h2>Login History (last {{ history|length }} attempts)</h2>
  {% if history %}
    <table>
      <thead>
        <tr><th>Timestamp</th><th>Status</th><th>Detail</th></tr>
      </thead>
      <tbody>
        {% for h in history %}
          <tr>
            <td>{{ h.timestamp }}</td>
            <td class="{% if h.status == 'success' %}ok{% else %}bad{% endif %}">
              {% if h.status == 'success' %}&#10003; SUCCESS{% else %}&#10007; FAILED{% endif %}
            </td>
            <td>{{ h.detail }}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <div class="empty">No login history yet.</div>
  {% endif %}
</div>
</body>
</html>
"""


def render(active, heading, data, error):
    rows = []
    cols = []
    if data:
        # Normalise: SDK returns list of dicts (each row) or a dict-of-scalars
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            # Some endpoints (limits) return a single flat dict - show as one row
            rows = [data]
        if rows and isinstance(rows[0], dict):
            # Collect all keys across rows, preserve first-row order
            seen = []
            for r in rows:
                for k in r.keys():
                    if k not in seen:
                        seen.append(k)
            cols = seen

    return render_template_string(
        PAGE,
        tabs=TABS,
        active=active,
        heading=heading,
        rows=rows,
        cols=cols,
        view_error=error,
        greeting=_state.get("greeting"),
        login_time=_state["login_time"].strftime("%H:%M:%S IST") if _state.get("login_time") else None,
        error=_state.get("error"),
    )


@app.route("/")
def holdings_view():
    try:
        client = ensure_client()
        data, err = safe_call(client.holdings)
        return render("holdings", "Portfolio Holdings", data, err)
    except Exception as e:
        return render("holdings", "Portfolio Holdings", None, traceback.format_exc())


@app.route("/positions")
def positions_view():
    try:
        client = ensure_client()
        data, err = safe_call(client.positions)
        return render("positions", "Open Positions", data, err)
    except Exception as e:
        return render("positions", "Open Positions", None, traceback.format_exc())


@app.route("/orders")
def orders_view():
    try:
        client = ensure_client()
        data, err = safe_call(client.order_report)
        return render("orders", "Order Book", data, err)
    except Exception as e:
        return render("orders", "Order Book", None, traceback.format_exc())


@app.route("/trades")
def trades_view():
    try:
        client = ensure_client()
        data, err = safe_call(client.trade_report)
        return render("trades", "Trade Book", data, err)
    except Exception as e:
        return render("trades", "Trade Book", None, traceback.format_exc())


@app.route("/limits")
def limits_view():
    try:
        client = ensure_client()
        data, err = safe_call(client.limits, segment="ALL", exchange="ALL", product="ALL")
        return render("limits", "Funds & Limits", data, err)
    except Exception as e:
        return render("limits", "Funds & Limits", None, traceback.format_exc())


@app.route("/history")
def history_view():
    return render_template_string(
        HISTORY_PAGE,
        tabs=TABS,
        active="history",
        history=read_history(),
    )


@app.route("/gann")
def gann_view():
    return render_template_string(
        GANN_PAGE,
        tabs=TABS,
        active="gann",
        scrips=SCRIPS,
        ucc=os.getenv("KOTAK_UCC", ""),
        level_colors=LEVEL_COLORS,
    )


@app.route("/api/feed-status")
def feed_status_api():
    """WebSocket QuoteFeed diagnostics."""
    return jsonify({
        "started": _feed_started["flag"],
        **_feed.status(),
        "fresh_threshold_seconds": WS_FRESH_SECONDS,
        "ts": now_ist().strftime("%H:%M:%S IST"),
    })


@app.route("/api/gann-prices")
def gann_prices_api():
    data, err = fetch_quotes()
    # Run auto-strategy (paper trades only) + update MFE on any open trades
    try:
        auto_strategy_tick(data)
    except Exception:
        traceback.print_exc()
    try:
        update_open_trades_mfe(data)
    except Exception:
        pass
    # Preserve scrip order; attach nearest_level so the UI can color the LTP cell
    ordered = []
    for s in SCRIPS:
        row = data.get(s["symbol"], {
            "symbol": s["symbol"], "ltp": None, "open": None,
            "low": None, "high": None, "levels": {"sell": {}, "buy": {}},
        })
        # Shallow copy to avoid mutating cache
        row = dict(row)
        nl, _ = nearest_gann_level(row)
        row["nearest_level"] = nl
        ordered.append(row)
    stats = compute_stats(read_paper_trades())
    return jsonify({"scrips": ordered, "error": err,
                    "ts": now_ist().strftime("%H:%M:%S IST"),
                    "stats": stats})


# ---------- Options routes ----------
@app.route("/options")
def options_view():
    return render_template_string(
        OPTIONS_PAGE,
        tabs=TABS,
        active="options",
        indices=[{"key": k, "label": v["label"]} for k, v in INDEX_OPTIONS_CONFIG.items()],
        ucc=os.getenv("KOTAK_UCC", ""),
    )


@app.route("/api/option-prices")
def option_prices_api():
    """Returns option chains for all configured indices.
    Non-blocking: if the per-day F&O universe cache isn't warm yet, return a
    quick 'loading' payload so the worker isn't held for 60-90s (which would
    trip Render's ~30s proxy timeout and return 502)."""
    today = now_ist().strftime("%Y-%m-%d")
    universe_ready = (
        _option_universe["date"] == today
        and all(i in _option_universe["by_index"] for i in INDEX_OPTIONS_CONFIG)
    )
    if not universe_ready:
        # Kick the preload in background (idempotent — daily cache guards it)
        # and return immediately so the frontend keeps polling.
        if not _option_universe.get("loading"):
            _option_universe["loading"] = True
            def _warm():
                try:
                    _preload_option_universe()
                finally:
                    _option_universe["loading"] = False
            threading.Thread(target=_warm, daemon=True).start()
        return jsonify({
            "chains": {},
            "error": None,
            "loading": True,
            "preload_status": _option_universe.get("preload_status") or {},
            "loaded_indices": list(_option_universe.get("by_index", {}).keys()),
            "ts": now_ist().strftime("%H:%M:%S IST"),
        })
    data, meta, err = fetch_option_quotes()
    # Run paper auto-strategy on each tick
    try:
        option_auto_strategy_tick(data, meta)
    except Exception as e:
        print(f"[option_auto] tick failed: {type(e).__name__}: {e}")
    # Group by index: chains[index] = {spot, atm, expiry, rows:[{strike, ce:{...}, pe:{...}, is_atm}]}
    chains = {}
    for idx_name, m in meta.items():
        chains[idx_name] = {
            "label": INDEX_OPTIONS_CONFIG[idx_name]["label"],
            "spot": m.get("spot"),
            "atm": m.get("atm"),
            "expiry": m.get("expiry"),
            "error": m.get("error"),
            "rows": [],
        }
    # Group quote rows into per-index per-strike
    by_idx_strike = {}
    for q in data.values():
        idx = q["index"]; s = q["strike"]
        by_idx_strike.setdefault(idx, {}).setdefault(s, {})[q["option_type"]] = q
    for idx_name, per_strike in by_idx_strike.items():
        cfg = INDEX_OPTIONS_CONFIG[idx_name]
        atm = chains[idx_name]["atm"]
        step = cfg["strike_step"]
        win = cfg["atm_window"]
        strikes = [atm + i * step for i in range(-win, win + 1)] if atm else sorted(per_strike.keys())
        for s in strikes:
            legs = per_strike.get(s, {})
            chains[idx_name]["rows"].append({
                "strike": s,
                "is_atm": atm is not None and s == atm,
                "ce": legs.get("CE"),
                "pe": legs.get("PE"),
            })
    # Attach live option trades (open + today's closed) so Options UI can show them
    today = now_ist().strftime("%Y-%m-%d")
    all_trades = read_paper_trades()
    option_trades = [
        t for t in all_trades
        if t.get("asset_type") == "option"
        and (t.get("status") == "OPEN" or t.get("date") == today)
    ]
    # Live MFE for open positions
    for t in option_trades:
        if t.get("status") == "OPEN":
            q = data.get(t.get("option_key"))
            if q and q.get("ltp") is not None:
                t["live_ltp"] = q["ltp"]
                t["live_pnl_points"] = round(float(q["ltp"]) - float(t["entry_price"]), 2)

    return jsonify({
        "chains": chains,
        "error": err,
        "option_trades": option_trades,
        "ts": now_ist().strftime("%H:%M:%S IST"),
    })


# ---------- Paper trading routes ----------
@app.route("/api/paper-open", methods=["POST"])
def paper_open_api():
    """Open a new paper trade at the current LTP. No Kotak involvement."""
    payload = request.get_json(force=True, silent=True) or {}
    symbol = (payload.get("symbol") or "").strip()
    side   = (payload.get("side") or "").strip().upper()   # B or S
    try:
        qty = int(payload.get("qty") or 1)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Qty must be integer"}), 400
    if qty <= 0:
        return jsonify({"ok": False, "error": "Qty must be positive"}), 400
    if side not in ("B", "S"):
        return jsonify({"ok": False, "error": "Side must be B or S"}), 400
    scrip = find_scrip(symbol)
    if not scrip:
        return jsonify({"ok": False, "error": "Unknown symbol"}), 400
    # Indices aren't tradable cash even in paper (no real lot mapping) — allow
    # but we keep the tradeable gate consistent with live mode:
    if not scrip.get("tradeable"):
        return jsonify({"ok": False, "error": "Index symbols can't be paper-traded"}), 400

    quotes, _ = fetch_quotes()
    q = quotes.get(symbol)
    if not q or q.get("ltp") is None:
        return jsonify({"ok": False, "error": "No LTP available yet"}), 400
    entry_price = float(q["ltp"])
    now = now_ist()
    trades = read_paper_trades()
    trade = {
        "id": _next_paper_id(trades),
        "date": now.strftime("%Y-%m-%d"),
        "scrip": symbol,
        "order_type": "BUY" if side == "B" else "SELL",
        "entry_time": now.strftime("%H:%M:%S"),
        "entry_ts": now.timestamp(),
        "entry_price": round(entry_price, 2),
        "qty": qty,
        "max_min_target_price": round(entry_price, 2),
        "target_level_reached": None,
        "exit_time": None,
        "exit_ts": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl_points": None,
        "pnl_pct": None,
        "duration_seconds": None,
        "status": "OPEN",
    }
    trades.insert(0, trade)
    write_paper_trades(trades)
    return jsonify({"ok": True, "trade": trade})


@app.route("/api/paper-close", methods=["POST"])
def paper_close_api():
    """Close an open paper trade at current LTP."""
    payload = request.get_json(force=True, silent=True) or {}
    tid = str(payload.get("id") or "").strip()
    reason = (payload.get("reason") or "MANUAL").strip().upper()
    if not tid:
        return jsonify({"ok": False, "error": "id required"}), 400
    trades = read_paper_trades()
    target = None
    for t in trades:
        if str(t.get("id")) == tid:
            target = t
            break
    if not target:
        return jsonify({"ok": False, "error": "trade not found"}), 404
    if target.get("status") == "CLOSED":
        return jsonify({"ok": False, "error": "already closed"}), 400

    quotes, _ = fetch_quotes()
    q = quotes.get(target["scrip"])
    if not q or q.get("ltp") is None:
        return jsonify({"ok": False, "error": "No LTP available"}), 400
    exit_price = float(q["ltp"])
    now = now_ist()
    entry_price = float(target["entry_price"])
    qty = int(target.get("qty") or 1)
    if target["order_type"] == "BUY":
        pnl_points = round(exit_price - entry_price, 2)
    else:
        pnl_points = round(entry_price - exit_price, 2)
    pnl_pct = round((pnl_points / entry_price) * 100.0, 2) if entry_price else 0
    duration = now.timestamp() - float(target.get("entry_ts") or now.timestamp())

    target["exit_time"] = now.strftime("%H:%M:%S")
    target["exit_ts"] = now.timestamp()
    target["exit_price"] = round(exit_price, 2)
    target["exit_reason"] = reason
    target["pnl_points"] = pnl_points
    target["pnl_pct"] = pnl_pct
    target["duration_seconds"] = round(duration, 0)
    target["status"] = "CLOSED"
    # Finalise max-min-target and target_level_reached if not set yet
    side = "B" if target["order_type"] == "BUY" else "S"
    best = target.get("max_min_target_price") or exit_price
    reached = compute_target_level_reached(side, entry_price, best, q.get("levels"))
    if reached:
        target["target_level_reached"] = reached
    write_paper_trades(trades)
    return jsonify({"ok": True, "trade": target})


@app.route("/api/paper-trades")
def paper_trades_api():
    trades = read_paper_trades()
    return jsonify({"trades": trades, "stats": compute_stats(trades)})


@app.route("/paper-trades.xlsx")
def paper_trades_xlsx():
    """Export all paper trades as a formatted Excel file."""
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Paper Trades"
    headers = ["Date", "Scrip", "Order Type", "Entry Time (IST)", "Entry Price",
               "Target Level Reached", "Max/Min Target Price", "Exit Time (IST)",
               "Exit Price", "Exit Reason", "P&L Points", "P&L %", "Duration"]
    ws.append(headers)
    # Header styling
    hdr_fill = PatternFill("solid", fgColor="FFEB3B")
    for c, _ in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True)
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center")
    sell_fill = PatternFill("solid", fgColor="F8CBAD")
    buy_fill  = PatternFill("solid", fgColor="C6EFCE")
    pos_font  = Font(color="006100")
    neg_font  = Font(color="9C0006")
    for t in read_paper_trades():
        ws.append([
            t.get("date", ""),
            t.get("scrip", ""),
            t.get("order_type", ""),
            t.get("entry_time", ""),
            t.get("entry_price", ""),
            t.get("target_level_reached", "") or "",
            t.get("max_min_target_price", "") or "",
            t.get("exit_time", "") or "",
            t.get("exit_price", "") or "",
            t.get("exit_reason", "") or "",
            t.get("pnl_points", "") if t.get("pnl_points") is not None else "",
            t.get("pnl_pct", "") if t.get("pnl_pct") is not None else "",
            fmt_duration(t.get("duration_seconds")),
        ])
        r = ws.max_row
        # Colour the Order Type cell
        otype_cell = ws.cell(row=r, column=3)
        if t.get("order_type") == "SELL":
            otype_cell.fill = sell_fill
        else:
            otype_cell.fill = buy_fill
        otype_cell.alignment = Alignment(horizontal="center")
        # P&L colouring
        try:
            pl = float(t.get("pnl_points") or 0)
            ws.cell(row=r, column=11).font = pos_font if pl >= 0 else neg_font
            ws.cell(row=r, column=12).font = pos_font if pl >= 0 else neg_font
        except (TypeError, ValueError):
            pass
    # Column widths
    widths = [12, 12, 12, 18, 14, 20, 20, 18, 12, 14, 12, 10, 12]
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_paper_export.xlsx")
    wb.save(out)
    with open(out, "rb") as f:
        data = f.read()
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=paper_trades.xlsx"},
    )


@app.route("/api/margin-summary")
def margin_summary_api():
    """Return available cash so the order ticket can show margin headroom."""
    try:
        client = ensure_client()
        data, err = safe_call(client.limits, segment="ALL", exchange="ALL", product="ALL")
        if err:
            return jsonify({"error": err, "available": None})
        # limits may return dict-of-fields or list-of-rows. Surface the most
        # useful field. Kotak typically uses Net or CashAvailable / Net.
        avail = None
        if isinstance(data, dict):
            for k in ("Net", "net", "CashAvailable", "cashAvailable",
                     "AvailableCash", "availableCash", "DepositValue"):
                if k in data:
                    try:
                        avail = float(data[k])
                        break
                    except (TypeError, ValueError):
                        pass
        return jsonify({"available": avail, "raw": data, "error": None})
    except Exception as e:
        return jsonify({"error": str(e), "available": None})


@app.route("/api/place-order", methods=["POST"])
def place_order_api():
    """Place a real order with Kotak. Records every attempt to orders_log.json."""
    payload = request.get_json(force=True, silent=True) or {}
    symbol      = (payload.get("symbol") or "").strip()
    side        = (payload.get("side") or "").strip().upper()      # B or S
    qty         = str(payload.get("qty") or "").strip()
    order_type  = (payload.get("order_type") or "L").strip().upper()  # L / MKT
    price       = str(payload.get("price") or "0").strip()
    product     = (payload.get("product") or "MIS").strip().upper()    # MIS / CNC
    validity    = (payload.get("validity") or "DAY").strip().upper()
    trigger     = str(payload.get("trigger") or "0").strip()
    pin         = (payload.get("pin") or "").strip()

    ts = now_ist().strftime("%Y-%m-%d %H:%M:%S IST")
    log_entry = {
        "timestamp": ts, "symbol": symbol, "side": side, "qty": qty,
        "order_type": order_type, "price": price, "product": product,
        "validity": validity, "trigger": trigger,
        "status": "REJECTED", "kotak_order_id": None, "message": "",
    }

    # Validation
    expected_pin = os.getenv("ORDER_PIN", "").strip()
    if expected_pin and pin != expected_pin:
        log_entry["message"] = "Wrong PIN"
        append_order(log_entry)
        return jsonify({"ok": False, "error": "Wrong PIN"}), 401

    scrip = find_scrip(symbol)
    if not scrip:
        log_entry["message"] = "Unknown symbol"
        append_order(log_entry)
        return jsonify({"ok": False, "error": "Unknown symbol"}), 400
    if not scrip.get("tradeable"):
        log_entry["message"] = "Symbol not tradeable (index)"
        append_order(log_entry)
        return jsonify({"ok": False, "error": "Index symbols can't be traded as cash"}), 400
    if side not in ("B", "S"):
        log_entry["message"] = "Side must be B or S"
        append_order(log_entry)
        return jsonify({"ok": False, "error": "Side must be B or S"}), 400
    try:
        if int(qty) <= 0:
            raise ValueError("qty<=0")
    except (TypeError, ValueError):
        log_entry["message"] = "Qty must be a positive integer"
        append_order(log_entry)
        return jsonify({"ok": False, "error": "Qty must be a positive integer"}), 400
    if order_type == "L":
        try:
            if float(price) <= 0:
                raise ValueError("price<=0")
        except (TypeError, ValueError):
            log_entry["message"] = "Price required for LIMIT"
            append_order(log_entry)
            return jsonify({"ok": False, "error": "Price required for LIMIT order"}), 400

    # Place the order
    try:
        client = ensure_client()
    except Exception as e:
        log_entry["message"] = f"login: {e}"
        append_order(log_entry)
        return jsonify({"ok": False, "error": str(e)}), 500

    try:
        resp = client.place_order(
            exchange_segment=scrip["exchange"],
            product=product,
            price=price if order_type == "L" else "0",
            order_type=order_type,
            quantity=qty,
            validity=validity,
            trading_symbol=scrip["trading_symbol"],
            transaction_type=side,
            trigger_price=trigger,
            tag="gann-ui",
        )
    except Exception as e:
        log_entry["message"] = f"{type(e).__name__}: {e}"
        append_order(log_entry)
        return jsonify({"ok": False, "error": log_entry["message"]}), 500

    # Parse response
    if isinstance(resp, dict):
        # success path: resp commonly has nOrdNo / data.orderId
        oid = (resp.get("nOrdNo")
               or (resp.get("data") or {}).get("orderId")
               or (resp.get("data") or {}).get("nOrdNo"))
        err = resp.get("error") or resp.get("Error") or resp.get("errMsg")
        msg = resp.get("stat") or resp.get("Message") or resp.get("statusDescription")
        if oid:
            log_entry["status"] = "PLACED"
            log_entry["kotak_order_id"] = str(oid)
            log_entry["message"] = msg or "Order accepted"
            append_order(log_entry)
            return jsonify({"ok": True, "order_id": str(oid),
                           "message": log_entry["message"], "raw": resp})
        if err:
            err_str = err if isinstance(err, str) else json.dumps(err)
            log_entry["message"] = err_str
            append_order(log_entry)
            return jsonify({"ok": False, "error": err_str, "raw": resp}), 400

    log_entry["message"] = f"Unexpected response: {json.dumps(resp)[:200]}"
    append_order(log_entry)
    return jsonify({"ok": False, "error": "Unexpected response", "raw": resp}), 500


@app.route("/orderlog")
def orderlog_view():
    return render_template_string(
        ORDERLOG_PAGE, tabs=TABS, active="orderlog",
        orders=read_orders(),
    )


@app.route("/orderlog.csv")
def orderlog_csv():
    rows = read_orders()
    cols = ["timestamp", "symbol", "side", "qty", "order_type", "price",
            "product", "validity", "trigger", "status", "kotak_order_id", "message"]
    lines = [",".join(cols)]
    for r in rows:
        vals = []
        for c in cols:
            v = str(r.get(c, ""))
            if "," in v or '"' in v or "\n" in v:
                v = '"' + v.replace('"', '""') + '"'
            vals.append(v)
        lines.append(",".join(vals))
    return Response("\n".join(lines), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders_log.csv"})


@app.route("/refresh", methods=["POST", "GET"])
def refresh():
    _state["client"] = None
    _state["login_time"] = None
    _state["greeting"] = None
    _state["error"] = None
    # Trigger fresh login immediately (don't wait for next page view)
    try:
        ensure_client()
    except Exception:
        pass  # error already recorded in _state and history
    return redirect(url_for("holdings_view"))


def _preload_option_universe():
    """Warm up the F&O universe cache in background so first /options visit
    is fast instead of waiting 90s for 3 sequential search_scrip calls."""
    _option_universe["preload_status"] = {}
    for idx_name in INDEX_OPTIONS_CONFIG:
        try:
            items, err = _fetch_index_fo_universe(idx_name)
            msg = f"{len(items)} contracts" + (f" (err: {err})" if err else "")
            _option_universe["preload_status"][idx_name] = msg
            print(f"[options] preloaded {idx_name}: {msg}")
        except Exception as e:
            msg = f"EXCEPTION: {type(e).__name__}: {e}"
            _option_universe["preload_status"][idx_name] = msg
            print(f"[options] preload {idx_name} failed: {msg}")


if __name__ == "__main__":
    print("=" * 60)
    print("Kotak Neo Dashboard starting...")
    print("Open http://localhost:5000 in your browser")
    print("=" * 60)
    # threaded=True: don't block other requests while a slow search_scrip runs
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

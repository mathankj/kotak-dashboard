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
import traceback
import pyotp
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template_string, jsonify, redirect, url_for
from dotenv import load_dotenv
from neo_api_client import NeoAPI

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
    {"symbol": "NIFTY 50",  "token": "Nifty 50",   "exchange": "nse_cm"},
    {"symbol": "BANKNIFTY", "token": "Nifty Bank", "exchange": "nse_cm"},
    {"symbol": "SENSEX",    "token": "SENSEX",     "exchange": "bse_cm"},
    {"symbol": "RELIANCE",  "token": "2885",       "exchange": "nse_cm"},
    {"symbol": "TCS",       "token": "11536",      "exchange": "nse_cm"},
    {"symbol": "INFOSYS",   "token": "1594",       "exchange": "nse_cm"},
    {"symbol": "HDFCBANK",  "token": "1333",       "exchange": "nse_cm"},
    {"symbol": "ICICIBANK", "token": "4963",       "exchange": "nse_cm"},
    {"symbol": "SBIN",      "token": "3045",       "exchange": "nse_cm"},
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


def _extract_ohlc(quote_obj):
    """Pull (ltp, open, low, high) from a Kotak quote response item.
    Tolerant of various key shapes the SDK might return."""
    if not isinstance(quote_obj, dict):
        return None, None, None, None
    def pick(*keys):
        for k in keys:
            if k in quote_obj and quote_obj[k] not in (None, "", "0"):
                try:
                    return float(quote_obj[k])
                except (ValueError, TypeError):
                    pass
        return None
    ltp  = pick("ltp", "last_traded_price", "lastPrice", "lp")
    op   = pick("open", "openPrice", "op", "o")
    low  = pick("low", "lowPrice", "lo", "l")
    high = pick("high", "highPrice", "hp", "h")
    return ltp, op, low, high


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

    _quote_cache["data"] = out
    _quote_cache["ts"] = now
    _quote_cache["error"] = last_err
    return out, last_err


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
    {"key": "holdings", "url": "/", "label": "Holdings"},
    {"key": "positions", "url": "/positions", "label": "Positions"},
    {"key": "orders", "url": "/orders", "label": "Orders"},
    {"key": "trades", "url": "/trades", "label": "Trades"},
    {"key": "limits", "url": "/limits", "label": "Limits"},
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
  td.ltp { font-weight: 700; background: #f9fafb; font-size: 13px; }
  td.pnl { background: #dcfce7; color: #166534; font-weight: 600; }
  td.pnl.neg { background: #fee2e2; color: #991b1b; }
  th.group-sell { background: #6b7280; color: #fff; }
  th.group-buy  { background: #6b7280; color: #fff; }
  th.group-ltp  { background: #1f2937; color: #fff; }
  .legend { display: flex; gap: 16px; justify-content: center;
            padding: 12px; font-size: 12px; color: #6b7280; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .swatch { width: 14px; height: 14px; border-radius: 3px; display: inline-block; }
  .err-banner { background: #fef3c7; border: 1px solid #fbbf24; color: #78350f;
                padding: 10px 14px; border-radius: 8px; margin-bottom: 12px;
                font-size: 13px; }
</style>
</head>
<body>
<header>
  <div>
    <span class="title">Gann Trader</span>
    <span id="livedot" class="live">● Live</span>
  </div>
  <div style="display:flex; gap:8px; align-items:center;">
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
    <tr data-symbol="{{ s.symbol }}">
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
<div class="legend">
  <span><span class="swatch" style="background:#EF9A9A"></span>Sell Levels (Darker = Far from LTP)</span>
  <span><span class="swatch" style="background:#FF9800"></span>WA (Watch Area)</span>
  <span><span class="swatch" style="background:#A5D6A7"></span>Buy Levels (Darker = Far from LTP)</span>
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

function paintCell(td, level, value, ltp) {
  if (value === null || value === undefined) {
    td.textContent = "-";
    td.style.background = "";
    td.style.color = "";
    return;
  }
  td.textContent = fmt(value);
  // Distance shading: closer to LTP = lighter
  if (ltp && value) {
    const distAbs = Math.abs(value - ltp);
    // small distance => light, large => dark (use base color from LEVEL_COLORS)
    td.style.background = LEVEL_COLORS[level] || "";
    td.style.color = "#000";
    // Lighten if close to LTP (within ~0.3% of LTP)
    if (distAbs / ltp < 0.003) {
      td.style.opacity = "0.55";
    } else {
      td.style.opacity = "1";
    }
  } else {
    td.style.background = "";
    td.style.color = "";
  }
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

    for (const s of data.scrips) {
      const tr = document.querySelector('tr[data-symbol="' + s.symbol + '"]');
      if (!tr) continue;
      const ltp = s.ltp;
      tr.querySelector('[data-col=ltp]').textContent = fmt(ltp);
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

setInterval(tickClock, 1000); tickClock();
setInterval(refresh, 2500); refresh();
</script>
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


@app.route("/api/gann-prices")
def gann_prices_api():
    data, err = fetch_quotes()
    # Preserve scrip order
    ordered = [data.get(s["symbol"], {
        "symbol": s["symbol"], "ltp": None, "open": None,
        "low": None, "high": None, "levels": {"sell": {}, "buy": {}},
    }) for s in SCRIPS]
    return jsonify({"scrips": ordered, "error": err, "ts": now_ist().strftime("%H:%M:%S IST")})


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


if __name__ == "__main__":
    print("=" * 60)
    print("Kotak Neo Dashboard starting...")
    print("Open http://localhost:5000 in your browser")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)

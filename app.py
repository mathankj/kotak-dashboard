"""
Kotak Neo Dashboard - Web app showing holdings, positions, orders, trades, limits.

Run:
    pip install flask pyotp python-dotenv
    python app.py
Open: http://localhost:5000
"""
import os
import json
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
    {"key": "holdings", "url": "/", "label": "Holdings"},
    {"key": "positions", "url": "/positions", "label": "Positions"},
    {"key": "orders", "url": "/orders", "label": "Orders"},
    {"key": "trades", "url": "/trades", "label": "Trades"},
    {"key": "limits", "url": "/limits", "label": "Limits"},
    {"key": "history", "url": "/history", "label": "Login History"},
]


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

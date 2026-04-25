"""
Kotak Neo Dashboard - Web app showing holdings, positions, orders, trades, limits.

Run:
    pip install flask pyotp python-dotenv
    python app.py
Open: http://localhost:5000
"""
import os
import json
import threading
import traceback
from datetime import datetime
from flask import Flask, render_template_string, jsonify, redirect, url_for, request, Response

from backend.utils import IST, now_ist
from backend.kotak.client import (
    _state, login, ensure_client, safe_call,
    append_history, read_history, HISTORY_FILE,
)
from backend.kotak.instruments import (
    SCRIPS, find_scrip,
    INDEX_OPTIONS_CONFIG, _option_universe,
    _fetch_index_fo_universe, _parse_item_strike, _parse_item_expiry_date,
)
from backend.quotes import (
    fetch_quotes, fetch_option_quotes, build_option_chain,
    build_all_option_tokens, _feed,
)
from backend.storage.trades import (
    PAPER_FILE, read_paper_trades, write_paper_trades, next_paper_id,
)
from backend.storage.orders import ORDERS_FILE, append_order, read_orders
from backend.strategy.gann import (
    GANN_STEP, SELL_LEVELS, BUY_LEVELS, LEVEL_COLORS,
    BUY_LEVEL_ORDER, SELL_LEVEL_ORDER,
    gann_levels, nearest_gann_level, compute_target_level_reached,
)
from backend.strategy.stocks import (
    AUTO_STRATEGY_ENABLED, AUTO_HOURS_START, AUTO_HOURS_END,
    AUTO_MAX_TRADES_PER_SCRIP, AUTO_QTY,
    _auto_state, auto_strategy_tick, update_open_trades_mfe,
)
from backend.strategy.options import (
    AUTO_OPTION_STRATEGY_ENABLED, _option_auto_state, option_auto_strategy_tick,
)

app = Flask(__name__)


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


@app.route("/api/health")
def health_api():
    """Strong-API stats: per-method call counts, errors, retries, breaker state."""
    from backend.kotak.api import stats as kotak_stats
    return jsonify({
        "ok": True,
        "kotak": kotak_stats(),
        "feed": _feed.status(),
        "feed_started": _feed_started["flag"],
        "logged_in": _state.get("client") is not None,
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
    # Run paper auto-strategy on each tick. Pass gann_quotes so the strategy
    # module doesn't need to import fetch_quotes (would be a circular import).
    try:
        gann_quotes, _ = fetch_quotes()
        option_auto_strategy_tick(data, meta, gann_quotes)
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
        "id": next_paper_id(trades),
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

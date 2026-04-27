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
import time
import traceback
from datetime import datetime
from flask import Flask, render_template, jsonify, redirect, url_for, request, Response

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
    LEDGER_FILE, read_trade_ledger, write_trade_ledger, next_trade_id,
)
from backend.storage.orders import ORDERS_FILE, append_order, read_orders
from backend.storage.blocked import (
    append_blocked, read_recent_blocked, read_blocked_since,
)
from backend.strategy.gann import (
    GANN_STEP, SELL_LEVELS, BUY_LEVELS, LEVEL_COLORS,
    BUY_LEVEL_ORDER, SELL_LEVEL_ORDER,
    gann_levels, nearest_gann_level, compute_target_level_reached,
)
from backend.strategy.common import update_open_trades_mfe, _auto_in_hours
from backend.strategy.options import (
    AUTO_OPTION_STRATEGY_ENABLED, LIVE_MODE,
    _option_auto_state, option_auto_strategy_tick,
)
from backend.safety.kill_switch import is_halted, halt, halt_info
from backend.safety.orders import (
    place_order_safe,
    RESULT_OK, RESULT_PAPER, RESULT_BLOCKED_HALTED,
    RESULT_BLOCKED_MARGIN, RESULT_KOTAK_ERROR,
)
from backend.safety.audit import audit, read_audit_tail

app = Flask(__name__,
            template_folder="frontend/templates",
            static_folder="frontend/static")


def compute_stats(trades):
    open_n   = sum(1 for t in trades if t.get("status") == "OPEN")
    closed_n = sum(1 for t in trades if t.get("status") == "CLOSED")
    wins   = sum(1 for t in trades if t.get("status") == "CLOSED"
                 and (t.get("pnl_points") or 0) > 0)
    losses = sum(1 for t in trades if t.get("status") == "CLOSED"
                 and (t.get("pnl_points") or 0) < 0)
    total_pnl = 0.0
    for t in trades:
        if t.get("status") == "CLOSED" and t.get("pnl_points") is not None:
            try:
                total_pnl += float(t["pnl_points"]) * int(t.get("qty", 1))
            except (TypeError, ValueError):
                pass
    return {
        "total": len(trades), "open": open_n, "closed": closed_n,
        "wins": wins, "losses": losses,
        "total_pnl_points": round(total_pnl, 2),
        # Legacy keys still referenced by older code paths:
        "active": open_n, "pnl": round(total_pnl, 2),
    }


def fmt_duration(seconds):
    if seconds is None or seconds < 0:
        return ""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------- HTML template (single-file, dark theme) ----------
TABS = [
    {"key": "gann", "url": "/gann", "label": "Gann Trader"},
    {"key": "options", "url": "/options", "label": "Options"},
    {"key": "holdings", "url": "/", "label": "Holdings"},
    {"key": "positions", "url": "/positions", "label": "Positions"},
    {"key": "trades", "url": "/trades", "label": "Trade Log"},
    {"key": "blockers", "url": "/blockers", "label": "Blockers"},
    {"key": "audit", "url": "/audit", "label": "Audit"},
    {"key": "history", "url": "/history", "label": "Login History"},
]






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

    return render_template(
        "base.html",
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


@app.route("/trade-book")
def trade_book_view():
    """Kotak's broker-side executed trade list. Distinct from our internal
    trade ledger (/trades) which records every signal we acted on."""
    try:
        client = ensure_client()
        data, err = safe_call(client.trade_report)
        return render("trade-book", "Trade Book", data, err)
    except Exception as e:
        return render("trade-book", "Trade Book", None, traceback.format_exc())


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
    return render_template(
        "history.html",
        tabs=TABS,
        active="history",
        history=read_history(),
    )


@app.route("/gann")
def gann_view():
    return render_template(
        "gann.html",
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
    # Stock auto-buy is disabled — only update MFE / farthest-level on any
    # open trades (used by the dashboard). Option auto-strategy runs from
    # /api/option-prices, not here.
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
    stats = compute_stats(read_trade_ledger())
    return jsonify({"scrips": ordered, "error": err,
                    "ts": now_ist().strftime("%H:%M:%S IST"),
                    "stats": stats})


# ---------- Options routes ----------
def _build_option_chain_payload():
    """Build the same dict /api/option-prices returns, for server-side render.

    Returns None when the F&O universe cache isn't warm yet (first hit of
    the day) so the template falls back to the "Loading…" placeholder and
    the JS poller takes over.

    Why a separate helper: we want the /options page to come down with all
    three index chains already in the HTML so reload + tab-switch are
    instant — no flash of "Loading option chain…" while the JS does its
    first fetch. We deliberately do NOT call option_auto_strategy_tick()
    here; the autonomous background ticker (in __main__) already runs the
    strategy every 3s, so calling it from the page render would be a
    duplicate tick and could double-stamp open_evaluated.
    """
    today = now_ist().strftime("%Y-%m-%d")
    universe_ready = (
        _option_universe["date"] == today
        and all(i in _option_universe["by_index"] for i in INDEX_OPTIONS_CONFIG)
    )
    if not universe_ready:
        return None
    data, meta, err = fetch_option_quotes()
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
    all_trades = read_trade_ledger()
    option_trades = [
        t for t in all_trades
        if t.get("asset_type") == "option"
        and (t.get("status") == "OPEN" or t.get("date") == today)
    ]
    for t in option_trades:
        if t.get("status") == "OPEN":
            q = data.get(t.get("option_key"))
            if q and q.get("ltp") is not None:
                t["live_ltp"] = q["ltp"]
                t["live_pnl_points"] = round(float(q["ltp"]) - float(t["entry_price"]), 2)
    return {
        "chains": chains,
        "error": err,
        "option_trades": option_trades,
        "ts": now_ist().strftime("%H:%M:%S IST"),
    }


@app.route("/options")
def options_view():
    # Server-side render the initial chain so reload/tab-switch is instant.
    # If the universe isn't warm yet, initial_data is None and the page
    # falls back to the "Loading…" placeholder + JS poller (unchanged path).
    try:
        initial_data = _build_option_chain_payload()
    except Exception as e:
        print(f"[options_view] initial render failed: {type(e).__name__}: {e}")
        initial_data = None
    return render_template(
        "options.html",
        tabs=TABS,
        active="options",
        indices=[{"key": k, "label": v["label"]} for k, v in INDEX_OPTIONS_CONFIG.items()],
        ucc=os.getenv("KOTAK_UCC", ""),
        initial_data=initial_data,
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
    # Run option auto-strategy on each tick. Pass gann_quotes so the strategy
    # module doesn't need to import fetch_quotes (would be a circular import).
    # Pass `client` so the strategy can verify positions with Kotak before
    # proposing exits when LIVE_MODE is True. In paper mode the client arg
    # is unused.
    try:
        gann_quotes, _ = fetch_quotes()
        try:
            client_for_strategy = ensure_client()
        except Exception:
            client_for_strategy = None  # Strategy will refuse live exits without it
        option_auto_strategy_tick(data, meta, gann_quotes,
                                  client=client_for_strategy)
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
    all_trades = read_trade_ledger()
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


# ---------- Trade ledger routes ----------
@app.route("/api/trades")
def trades_api():
    trades = read_trade_ledger()
    return jsonify({"trades": trades, "stats": compute_stats(trades)})


@app.route("/trades")
def trades_view():
    """Trade Log page: every signal acted on, with a 'Download as Excel' button.
    Includes both LIVE rows (real Kotak orders) and any legacy paper rows."""
    trades = read_trade_ledger()
    # Newest first; attach a human-readable duration for the template.
    trades_sorted = sorted(
        trades,
        key=lambda t: (t.get("date") or "", t.get("entry_time") or ""),
        reverse=True,
    )
    for t in trades_sorted:
        t["duration_str"] = fmt_duration(t.get("duration_seconds"))
    return render_template(
        "trade_ledger.html",
        tabs=TABS,
        active="trades",
        trades=trades_sorted,
        stats=compute_stats(trades),
    )


@app.route("/trades.xlsx")
def trades_xlsx():
    """Export the trade ledger as a formatted Excel file."""
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Trade Ledger"
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
    for t in read_trade_ledger():
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
    # Write the xlsx to data/ (kept out of source listing) then stream it back.
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)
    out = os.path.join(data_dir, "_trade_ledger_export.xlsx")
    wb.save(out)
    with open(out, "rb") as f:
        data = f.read()
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=trade_ledger.xlsx"},
    )


# ---------- Blockers (refused order attempts) ----------
@app.route("/blockers")
def blockers_view():
    """Page showing every order the safety wrapper refused. Lets Ganesh
    see exactly what the auto-strategy *would have* traded and why it
    couldn't (e.g. zero balance, kill switch engaged, broker error)."""
    return render_template(
        "blockers.html",
        tabs=TABS,
        active="blockers",
        blocks=read_recent_blocked(500),
    )


@app.route("/api/recent-blocks")
def recent_blocks_api():
    """Toaster poll endpoint. Returns blocked-attempt records strictly newer
    than `since` (ISO timestamp). Browsers tail this every few seconds and
    pop a red toast for each new record."""
    since_ts = (request.args.get("since") or "").strip()
    return jsonify({
        "blocks": read_blocked_since(since_ts),
        "ts": now_ist().isoformat(),
    })


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

    # Login (must happen before margin fetch + safe wrapper)
    try:
        client = ensure_client()
    except Exception as e:
        log_entry["message"] = f"login: {e}"
        append_order(log_entry)
        return jsonify({"ok": False, "error": str(e)}), 500

    # Fetch available cash for the margin pre-check inside place_order_safe.
    # Best-effort: if Kotak limits() fails, we just skip the check (wrapper
    # treats available_cash=None as 'skip').
    available_cash = None
    try:
        ld, _ = safe_call(client.limits, segment="ALL", exchange="ALL", product="ALL")
        if isinstance(ld, dict):
            for k in ("Net", "net", "CashAvailable", "cashAvailable",
                      "AvailableCash", "availableCash", "DepositValue"):
                if k in ld:
                    try:
                        available_cash = float(ld[k]); break
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass

    # Single safe entry-point. Handles LIVE_MODE / kill switch / margin /
    # broker call / response parsing in one place. See backend/safety/orders.py.
    res = place_order_safe(
        client=client, scrip=scrip, side=side, qty=qty, price=price,
        order_type=order_type, product=product, validity=validity,
        trigger=trigger, tag="gann-ui",
        live_mode=LIVE_MODE, available_cash=available_cash,
        lot_size=1, source="manual_ticket",
    )

    # Persist a row to orders_log.json (for the /orderlog UI). Map wrapper
    # result -> the existing log_entry shape so the orderlog page stays the same.
    if res["result"] == RESULT_OK:
        log_entry["status"] = "PLACED"
        log_entry["kotak_order_id"] = res["order_id"]
        log_entry["message"] = res["message"]
        append_order(log_entry)
        return jsonify({"ok": True, "order_id": res["order_id"],
                        "message": res["message"], "raw": res["raw"]})

    if res["result"] == RESULT_PAPER:
        # LIVE_MODE is False — the manual ticket was clicked but we're in paper
        # mode. Surface a clear message so the operator isn't confused into
        # thinking a real order went through.
        log_entry["status"] = "PAPER"
        log_entry["message"] = res["message"]
        append_order(log_entry)
        return jsonify({"ok": False, "error": res["message"],
                        "paper_mode": True}), 400

    if res["result"] == RESULT_BLOCKED_HALTED:
        log_entry["status"] = "HALTED"
        log_entry["message"] = res["message"]
        append_order(log_entry)
        return jsonify({"ok": False, "error": res["message"],
                        "halted": True}), 423  # 423 Locked

    if res["result"] == RESULT_BLOCKED_MARGIN:
        log_entry["status"] = "INSUFFICIENT_FUNDS"
        log_entry["message"] = res["message"]
        append_order(log_entry)
        return jsonify({"ok": False, "error": res["message"]}), 402  # Payment Required

    # RESULT_KOTAK_ERROR or anything unrecognised
    log_entry["status"] = "REJECTED"
    log_entry["message"] = res["message"]
    append_order(log_entry)
    return jsonify({"ok": False, "error": res["message"],
                    "raw": res["raw"]}), 400


@app.route("/orderlog")
def orderlog_view():
    return render_template(
        "orderlog.html", tabs=TABS, active="orderlog",
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


@app.route("/audit")
def audit_view():
    """Last 100 audit events — human-readable. Useful for forensics after
    a halt or unexpected behaviour."""
    return render_template("audit.html",
                           tabs=TABS, active="audit",
                           events=list(reversed(read_audit_tail(100))))


# ---------- KILL SWITCH ----------
# /STOP shows a confirmation page (GET) so a fat-finger swipe doesn't halt
# trading. /STOP/confirm (POST) is what actually engages the halt. Re-arming
# is intentionally NOT a web action — Ganesh must SSH in and remove
# data/HALTED.flag manually after investigating.
@app.route("/STOP", methods=["GET"])
def stop_view():
    return render_template("stop_confirm.html",
                           tabs=TABS, active=None,
                           live_mode=LIVE_MODE,
                           halted=is_halted(),
                           halt_info=halt_info())


@app.route("/STOP/confirm", methods=["POST"])
def stop_confirm():
    reason = (request.form.get("reason") or "manual_web").strip()
    halt(reason=reason)
    audit("KILL_SWITCH_HALT", source="web", reason=reason)
    return redirect(url_for("stop_view"))


# Make LIVE_MODE + halt state available to every template (header button)
@app.context_processor
def _inject_safety_state():
    return {"live_mode": LIVE_MODE, "halted": is_halted()}


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


# ============================================================
# Autonomous strategy ticker — runs option_auto_strategy_tick()
# every 3 seconds during market hours, regardless of whether any
# browser has the Options page open.
#
# Why: in Phase 4 (LIVE), we cannot depend on a human keeping a
# tab open. The bot must see every tick autonomously. Browser
# polling still calls the same tick function (idempotent — guarded
# by the strategy's per-path lock) but is no longer the only
# trigger. - matha
# ============================================================
TICKER_INTERVAL_SECONDS = 3
_ticker_state = {"started": False}


def _strategy_ticker_loop():
    """Daemon thread body. Sleep / check hours / tick / repeat. Errors are
    swallowed and logged so a single bad tick can't kill the loop."""
    print("[ticker] autonomous strategy ticker started "
          f"(every {TICKER_INTERVAL_SECONDS}s during 09:15-15:15 IST)")
    while True:
        try:
            now = now_ist()
            if _auto_in_hours(now):
                try:
                    data, meta, _err = fetch_option_quotes()
                    if meta:
                        gann_quotes, _ = fetch_quotes()
                        try:
                            client_for_strategy = ensure_client()
                        except Exception:
                            client_for_strategy = None
                        option_auto_strategy_tick(
                            data, meta, gann_quotes,
                            client=client_for_strategy,
                        )
                except Exception as e:
                    print(f"[ticker] tick failed: "
                          f"{type(e).__name__}: {e}")
        except Exception as e:
            # Outermost guard — should be unreachable but keeps the
            # thread alive even on truly unexpected failures.
            print(f"[ticker] loop guard caught: "
                  f"{type(e).__name__}: {e}")
        time.sleep(TICKER_INTERVAL_SECONDS)


def _start_strategy_ticker_once():
    """Idempotent — safe to call from multiple boot paths."""
    if _ticker_state["started"]:
        return
    _ticker_state["started"] = True
    t = threading.Thread(target=_strategy_ticker_loop,
                         daemon=True, name="strategy-ticker")
    t.start()


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
    mode = "LIVE (real money)" if LIVE_MODE else "PAPER (no real orders)"
    print(f"Trading mode: {mode}")
    if is_halted():
        print("!! KILL SWITCH IS ENGAGED — new live orders will be refused.")
    print("Open http://localhost:5000 in your browser")
    print("=" * 60)
    audit("MODE_STARTUP", live_mode=LIVE_MODE, halted=is_halted())
    # Kick off the autonomous strategy ticker BEFORE app.run blocks. Daemon
    # thread, dies with the process on shutdown.
    _start_strategy_ticker_once()
    # threaded=True: don't block other requests while a slow search_scrip runs
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

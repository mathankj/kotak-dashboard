"""
SnapshotStore — hot in-memory cache of pre-serialized API payloads.

Pattern: a single producer thread refreshes the snapshots on a fixed
interval by calling the existing fetch_*_quotes() helpers and json.dumps()ing
the result once. HTTP request handlers do an O(1) read of the pre-built
bytes — no I/O, no JSON serialization, no compute on the request thread.

This is the "hot cache" pattern Zerodha Kite uses (see Zerodha Tech Blog,
"Scaling with common sense"): an incoming GET reads bytes and dumps them
to the HTTP connection.

Three payloads are maintained in lockstep so all three pages — gann,
options, futures — return instantly:

    options_payload()  -> bytes for /api/option-prices
    gann_payload()     -> bytes for /api/gann-prices
    futures_payload()  -> bytes for /api/future-prices

Build helpers live in this module (rather than app.py) so the producer
thread has zero dependency on Flask. Lazy imports inside the helpers keep
the import graph simple — backend.snapshot imports no Flask, app.py imports
backend.snapshot, no cycles.
"""
import json
import math
import threading
import time

from backend.utils import now_ist


# ---------- payload builders (called from the producer thread) ----------
def _empty_options():
    return {"chains": {}, "error": "warming", "loading": True,
            "option_trades": [], "ts": ""}


def _empty_gann():
    return {"scrips": [], "error": "warming", "loading": True,
            "stats": {}, "ts": ""}


def _empty_futures():
    return {"rows": [], "error": "warming", "loading": True,
            "future_trades": [], "apply_to": "both",
            "futures_enabled": True, "ts": ""}


def _build_options_payload():
    """Mirrors what /api/option-prices used to return. Does NOT tick the
    auto-strategy — the autonomous _strategy_ticker_loop in app.py owns
    that on its own 3s daemon."""
    from backend.quotes import fetch_option_quotes
    from backend.kotak.instruments import (
        INDEX_OPTIONS_CONFIG, _option_universe,
    )
    from backend.storage.trades import read_trade_ledger

    today = now_ist().strftime("%Y-%m-%d")
    universe_ready = (
        _option_universe.get("date") == today
        and all(i in _option_universe.get("by_index", {})
                for i in INDEX_OPTIONS_CONFIG)
    )
    if not universe_ready:
        return {
            "chains": {},
            "error": None,
            "loading": True,
            "option_trades": [],
            "ts": now_ist().strftime("%H:%M:%S IST"),
        }
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
        by_idx_strike.setdefault(q["index"], {}) \
            .setdefault(q["strike"], {})[q["option_type"]] = q
    for idx_name, per_strike in by_idx_strike.items():
        cfg = INDEX_OPTIONS_CONFIG[idx_name]
        atm = chains[idx_name]["atm"]
        step = cfg["strike_step"]
        win = cfg["atm_window"]
        strikes = ([atm + i * step for i in range(-win, win + 1)]
                   if atm else sorted(per_strike.keys()))
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
                t["live_pnl_points"] = round(
                    float(q["ltp"]) - float(t["entry_price"]), 2)
    return {
        "chains": chains,
        "error": err,
        "option_trades": option_trades,
        "ts": now_ist().strftime("%H:%M:%S IST"),
    }


def _build_gann_payload():
    """Mirrors /api/gann-prices."""
    from backend.quotes import fetch_quotes
    from backend.kotak.instruments import SCRIPS
    from backend.strategy.gann import nearest_gann_level
    from backend.strategy.common import update_open_trades_mfe
    from backend.storage.trades import read_trade_ledger
    # compute_stats lives in app.py — lazy import is safe because the
    # producer only starts after app.py has finished importing.
    from app import compute_stats

    data, err = fetch_quotes()
    try:
        update_open_trades_mfe(data)
    except Exception:
        pass
    ordered = []
    for s in SCRIPS:
        row = data.get(s["symbol"], {
            "symbol": s["symbol"], "ltp": None, "open": None,
            "low": None, "high": None, "levels": {"sell": {}, "buy": {}},
        })
        row = dict(row)
        nl, _ = nearest_gann_level(row)
        row["nearest_level"] = nl
        ordered.append(row)
    stats = compute_stats(read_trade_ledger())
    return {
        "scrips": ordered,
        "error": err,
        "ts": now_ist().strftime("%H:%M:%S IST"),
        "stats": stats,
    }


def _build_futures_payload():
    """Mirrors /api/future-prices."""
    from backend.quotes import fetch_quotes, fetch_future_quotes
    from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
    from backend.storage.trades import read_trade_ledger
    from backend import config_loader

    fut_data, err = fetch_future_quotes()
    gann_quotes, _ = fetch_quotes()
    today = now_ist().strftime("%Y-%m-%d")

    cfg = config_loader.get()
    entry_cfg = cfg["entry"]
    apply_to = cfg.get("apply_to", "both")

    rows = []
    for idx_name, fut in fut_data.items():
        spot_q_key = INDEX_OPTIONS_CONFIG[idx_name]["spot_symbol_key"]
        gq = gann_quotes.get(spot_q_key) or {}
        spot = gq.get("ltp")
        levels = gq.get("levels") or {}
        cr_buy_lvl = config_loader.resolve_buy_level(
            levels, entry_cfg["crossing_buy_level"])
        cr_sell_lvl = config_loader.resolve_sell_level(
            levels, entry_cfg["crossing_sell_level"])
        signal = "—"
        if spot is not None:
            if cr_buy_lvl is not None and float(spot) > cr_buy_lvl:
                signal = "LONG"
            elif cr_sell_lvl is not None and float(spot) < cr_sell_lvl:
                signal = "SHORT"
            else:
                signal = "IN-CHANNEL"
        step = config_loader.futures_round_step(idx_name)
        ltp = fut.get("ltp")
        buy_limit = sell_limit = None
        if ltp is not None and step > 0:
            buy_limit = math.floor(float(ltp) / step) * step
            sell_limit = math.ceil(float(ltp) / step) * step
        lots_mult = config_loader.lot_multiplier(idx_name)
        lot_size = fut.get("lot_size")
        rows.append({
            "idx": idx_name,
            "label": INDEX_OPTIONS_CONFIG[idx_name]["label"],
            "trading_symbol": fut.get("trading_symbol"),
            "expiry": str(fut.get("expiry") or ""),
            "lot_size": lot_size,
            "lots_mult": lots_mult,
            "qty": (lot_size or 0) * lots_mult,
            "ltp": ltp,
            "spot": spot,
            "buy_lvl": cr_buy_lvl,
            "sell_lvl": cr_sell_lvl,
            "signal": signal,
            "round_step": step,
            "buy_limit": buy_limit,
            "sell_limit": sell_limit,
        })

    all_trades = read_trade_ledger()
    future_trades = [
        t for t in all_trades
        if t.get("asset_type") == "future"
        and (t.get("status") == "OPEN" or t.get("date") == today)
    ]
    for t in future_trades:
        if t.get("status") == "OPEN":
            fut = fut_data.get(t.get("underlying")) or {}
            if fut.get("ltp") is not None:
                t["live_ltp"] = fut["ltp"]
                entry = float(t.get("entry_price") or 0)
                ltp_v = float(fut["ltp"])
                pnl = (ltp_v - entry) if t.get("order_type") == "BUY" \
                    else (entry - ltp_v)
                t["live_pnl_points"] = round(pnl, 2)

    return {
        "rows": rows,
        "future_trades": future_trades,
        "apply_to": apply_to,
        "futures_enabled": apply_to in ("futures", "both"),
        "error": err,
        "ts": now_ist().strftime("%H:%M:%S IST"),
    }


# ---------- store ----------
class SnapshotStore:
    """Three pre-serialized payloads, refreshed by one producer thread."""

    def __init__(self, refresh_interval=2.0, log=print):
        self._refresh_interval = refresh_interval
        self._log = log

        self._payloads = {
            "options": (json.dumps(_empty_options()).encode("utf-8"),
                        0.0, 0.0),
            "gann":    (json.dumps(_empty_gann()).encode("utf-8"),
                        0.0, 0.0),
            "futures": (json.dumps(_empty_futures()).encode("utf-8"),
                        0.0, 0.0),
        }
        self._refresh_count = 0
        self._error_count = {"options": 0, "gann": 0, "futures": 0}

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _payload(self, key):
        with self._lock:
            return self._payloads[key]

    def options_payload(self):  return self._payload("options")
    def gann_payload(self):     return self._payload("gann")
    def futures_payload(self):  return self._payload("futures")

    def stats(self):
        with self._lock:
            now = time.time()
            return {
                "refresh_interval_s": self._refresh_interval,
                "refresh_count": self._refresh_count,
                "errors": dict(self._error_count),
                "payloads": {
                    k: {
                        "bytes": len(blob),
                        "built_at": built_at,
                        "age_s": (round(now - built_at, 3)
                                  if built_at else None),
                        "build_ms": round(build_ms, 1),
                    }
                    for k, (blob, built_at, build_ms)
                    in self._payloads.items()
                }
            }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="SnapshotStore")
        self._thread.start()
        self._log("[snapshot] producer thread started "
                  f"(interval={self._refresh_interval}s)")

    def stop(self):
        self._stop.set()

    def _refresh_one(self, key, builder):
        t0 = time.time()
        try:
            obj = builder()
            blob = json.dumps(obj, default=str).encode("utf-8")
            with self._lock:
                self._payloads[key] = (blob, time.time(),
                                       (time.time() - t0) * 1000.0)
        except Exception as e:
            with self._lock:
                self._error_count[key] += 1
            self._log(f"[snapshot] {key} refresh failed: "
                      f"{type(e).__name__}: {e}")

    def _run(self):
        while not self._stop.is_set():
            t0 = time.time()
            # Build all three sequentially in this thread. The slowest
            # payload (options) dominates cadence; the others piggy-back
            # on the same 2s loop. This keeps total CPU load bounded —
            # one quote pipeline at a time, never overlapping.
            self._refresh_one("options", _build_options_payload)
            self._refresh_one("gann",    _build_gann_payload)
            self._refresh_one("futures", _build_futures_payload)
            with self._lock:
                self._refresh_count += 1
            elapsed = time.time() - t0
            self._stop.wait(max(0.1, self._refresh_interval - elapsed))


# Module-level singleton — same pattern as backend.quotes._feed
_store = SnapshotStore(refresh_interval=2.0)

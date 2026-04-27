"""
SnapshotStore — hot in-memory cache of pre-serialized API payloads.

Pattern: a single producer thread refreshes the snapshot on a fixed interval
by calling the existing fetch_*_quotes() helpers and json.dumps()ing the
result once. HTTP request handlers then do a O(1) read of the pre-built
bytes — no I/O, no JSON serialization, no compute on the request thread.

This is the "hot cache" pattern Zerodha Kite uses (see Zerodha Tech Blog,
"Scaling with common sense"): an incoming GET reads bytes and dumps them
to the HTTP connection.

Currently scoped to options as a proof-of-concept route /api/option-prices-v2.
If the speedup is proven on real market data, this module will grow to cover
gann + futures payloads too.
"""
import json
import threading
import time

from backend.utils import now_ist


class SnapshotStore:
    def __init__(self, refresh_interval=2.0, log=print):
        self._refresh_interval = refresh_interval
        self._log = log

        # Pre-serialized payload bytes. Routes return these directly.
        self._options_bytes = b'{"chains":{},"error":"warming","loading":true,"option_trades":[],"ts":""}'
        self._options_built_at = 0.0
        self._options_build_ms = 0.0
        self._options_refresh_count = 0
        self._options_error_count = 0

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ---------- public reads ----------
    def options_payload(self):
        """Return (bytes, built_at_epoch, build_ms). O(1)."""
        with self._lock:
            return (self._options_bytes,
                    self._options_built_at,
                    self._options_build_ms)

    def stats(self):
        with self._lock:
            age = (time.time() - self._options_built_at
                   if self._options_built_at else None)
            return {
                "options_built_at": self._options_built_at,
                "options_age_s": round(age, 3) if age is not None else None,
                "options_build_ms": round(self._options_build_ms, 1),
                "options_refresh_count": self._options_refresh_count,
                "options_error_count": self._options_error_count,
                "options_bytes": len(self._options_bytes),
                "refresh_interval_s": self._refresh_interval,
            }

    # ---------- lifecycle ----------
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

    # ---------- producer loop ----------
    def _run(self):
        # Lazy import so this module has no circular dep with backend.quotes.
        # backend.quotes imports nothing from us; we read from it.
        from backend.quotes import fetch_option_quotes
        from backend.kotak.instruments import (
            INDEX_OPTIONS_CONFIG, _option_universe,
        )
        from backend.storage.trades import read_trade_ledger

        while not self._stop.is_set():
            t0 = time.time()
            try:
                today = now_ist().strftime("%Y-%m-%d")
                universe_ready = (
                    _option_universe.get("date") == today
                    and all(i in _option_universe.get("by_index", {})
                            for i in INDEX_OPTIONS_CONFIG)
                )

                if not universe_ready:
                    payload = {
                        "chains": {},
                        "error": None,
                        "loading": True,
                        "option_trades": [],
                        "ts": now_ist().strftime("%H:%M:%S IST"),
                    }
                else:
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
                        strikes = ([atm + i * step
                                    for i in range(-win, win + 1)]
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
                                    float(q["ltp"]) - float(t["entry_price"]),
                                    2)
                    payload = {
                        "chains": chains,
                        "error": err,
                        "option_trades": option_trades,
                        "ts": now_ist().strftime("%H:%M:%S IST"),
                    }

                # Serialize ONCE here, in the producer.
                blob = json.dumps(payload, default=str).encode("utf-8")
                build_ms = (time.time() - t0) * 1000.0

                with self._lock:
                    self._options_bytes = blob
                    self._options_built_at = time.time()
                    self._options_build_ms = build_ms
                    self._options_refresh_count += 1

            except Exception as e:
                with self._lock:
                    self._options_error_count += 1
                self._log(f"[snapshot] refresh failed: "
                          f"{type(e).__name__}: {e}")

            # Sleep so total cadence ≈ refresh_interval, not interval+build.
            elapsed = time.time() - t0
            self._stop.wait(max(0.1, self._refresh_interval - elapsed))


# Module-level singleton — same pattern as backend.quotes._feed
_store = SnapshotStore(refresh_interval=2.0)

"""
QuoteFeed — persistent Kotak Neo WebSocket subscriber.

Maintains an in-memory cache of the latest tick per (exchange_segment, token).
Auto-reconnects on disconnect. Lets options subscriptions be updated when ATM
shifts.

The cache keys mirror the indexing scheme used by app.fetch_quotes /
fetch_option_quotes — `(exchange.lower(), str(token).lower())` — so the
existing fetchers can read from this cache with the same lookup.

Design constraints:
- Never raises out of callbacks; everything caught & counted in `errors`.
- Thread-safe: cache reads/writes guarded by a single Lock.
- Reconnect with exponential backoff capped at 30 s.
- Subscriptions are idempotent — re-calling subscribe with same set is a no-op.

Usage:
    from quote_feed import QuoteFeed
    feed = QuoteFeed(client_provider=ensure_client)
    feed.set_index_subs([
        {"instrument_token": "Nifty 50",  "exchange_segment": "nse_cm"},
        {"instrument_token": "Nifty Bank","exchange_segment": "nse_cm"},
        {"instrument_token": "SENSEX",    "exchange_segment": "bse_cm"},
    ])
    feed.set_scrip_subs([
        {"instrument_token": "2885",  "exchange_segment": "nse_cm"},
        ...
    ])
    feed.start()
    ...
    tick = feed.get("nse_cm", "2885")     # {"ltp": ..., "ts": ..., ...} or None
"""
import json
import threading
import time
import traceback


class QuoteFeed:
    def __init__(self, client_provider, log=print):
        """
        client_provider: zero-arg callable returning a logged-in NeoAPI client.
                         Called whenever we need to (re)open the socket.
        log: callable(str) for diagnostic messages.
        """
        self._client_provider = client_provider
        self._log = log

        # cache: key=(exch_lower, token_lower) -> {"ltp": float, "ts": epoch,
        #                                          "op","lo","h","c","v"}
        self._cache = {}
        self._lock = threading.Lock()

        # subscription state
        self._index_subs = []   # list[{"instrument_token","exchange_segment"}]
        self._scrip_subs = []
        self._option_subs = []  # rebuilt by update_option_subs()
        self._future_subs = []  # nearest-expiry index futures (NIFTY/BN/SENSEX)
        self._subs_lock = threading.Lock()

        # status / diagnostics
        self._connected = False
        self._last_tick_ts = 0.0
        self._reconnects = 0
        self._errors = []          # bounded list of recent error strings
        self._last_subscribed_at = 0.0

        # control
        self._stop = threading.Event()
        self._thread = None
        self._client = None

    # ---------- public subscription setters ----------
    @staticmethod
    def _sub_key(s):
        """D.4 — collapse a sub-dict to its identity tuple so set comparison
        ignores caller-side dict ordering, extra keys, and list ordering.
        The producer rebuilds the futures sub-list every tick from a Python
        dict iteration; even when logically identical, that triggered a full
        WS resubscribe every 2s ('future subs changed (3 -> 3)') and
        periodic socket churn. Comparing as frozenset of identity tuples
        makes equality genuinely set-equal."""
        return (str(s.get("instrument_token", "")),
                str(s.get("exchange_segment", "")))

    @classmethod
    def _subs_set(cls, subs):
        return frozenset(cls._sub_key(s) for s in (subs or []))

    def set_index_subs(self, subs):
        with self._subs_lock:
            self._index_subs = list(subs or [])

    def set_scrip_subs(self, subs):
        with self._subs_lock:
            self._scrip_subs = list(subs or [])

    def set_option_subs(self, subs):
        """Replace option subscriptions wholesale; resubscribe on next loop tick."""
        with self._subs_lock:
            new = list(subs or [])
            # D.4 — compare as frozenset, not list==list.
            if self._subs_set(new) == self._subs_set(self._option_subs):
                return False
            self._option_subs = new
            return True

    def set_future_subs(self, subs):
        """Replace futures subscriptions (one nearest-expiry contract per
        index). Same delta-resubscribe behavior as options. Kept in its
        own slot so option-chain ATM drift can resub without touching
        the (rarely-changing) futures set."""
        with self._subs_lock:
            new = list(subs or [])
            # D.4 — compare as frozenset, not list==list.
            if self._subs_set(new) == self._subs_set(self._future_subs):
                return False
            self._future_subs = new
            return True

    # ---------- public reads ----------
    def get(self, exchange, token):
        k = (str(exchange).lower(), str(token).lower())
        with self._lock:
            v = self._cache.get(k)
            return dict(v) if v else None

    def status(self):
        with self._lock:
            sample = {f"{k[0]}|{k[1]}": v for k, v in
                      list(self._cache.items())[:12]}
            return {
                "connected": self._connected,
                "subs_index": len(self._index_subs),
                "subs_scrip": len(self._scrip_subs),
                "subs_option": len(self._option_subs),
                "subs_future": len(self._future_subs),
                "cached_keys": len(self._cache),
                "last_tick_ts": self._last_tick_ts,
                "last_tick_age": (time.time() - self._last_tick_ts)
                                 if self._last_tick_ts else None,
                "reconnects": self._reconnects,
                "errors": list(self._errors[-10:]),
                "cache_sample": sample,
            }

    # ---------- lifecycle ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="QuoteFeed")
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---------- internals ----------
    def _record_error(self, where, exc):
        msg = f"{where}: {type(exc).__name__}: {exc}"
        self._log(f"[quote_feed] {msg}")
        with self._lock:
            self._errors.append(f"{int(time.time())} {msg}")
            if len(self._errors) > 50:
                self._errors = self._errors[-50:]

    def _on_message(self, message):
        try:
            # Kotak SDK wraps ticks as {"type": "stock_feed", "data": [...]}
            if not isinstance(message, dict):
                return
            mtype = message.get("type")
            data = message.get("data")
            if mtype != "stock_feed" or not isinstance(data, list):
                return
            now = time.time()
            with self._lock:
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    tk = item.get("tk")
                    e  = item.get("e")
                    if not tk:
                        continue
                    # Some ticks omit exchange; if so, keep the previously known
                    # one for this token so cache stays consistent.
                    if not e:
                        # find any existing key with same token
                        e = next((kk[0] for kk in self._cache
                                  if kk[1] == str(tk).lower()), "")
                    key = (str(e).lower(), str(tk).lower())
                    prev = self._cache.get(key, {})
                    def _f(*names):
                        for n in names:
                            v = item.get(n)
                            if v in (None, "", "0"):
                                continue
                            try:
                                return float(v)
                            except (TypeError, ValueError):
                                pass
                        return None
                    # Stocks emit: ltp/op/lo/h/c
                    # Indices emit: iv/openingPrice/lowPrice/highPrice/ic
                    ltp = _f("ltp", "lp", "iv")
                    op  = _f("op", "openingPrice")
                    lo  = _f("lo", "lowPrice")
                    hi  = _f("h",  "highPrice")
                    c   = _f("c",  "ic")
                    merged = dict(prev)
                    if ltp is not None: merged["ltp"] = ltp
                    if op  is not None: merged["op"]  = op
                    if lo  is not None: merged["lo"]  = lo
                    if hi  is not None: merged["h"]   = hi
                    if c   is not None: merged["c"]   = c
                    merged["ts"] = now
                    self._cache[key] = merged
                self._last_tick_ts = now
        except Exception as e:
            self._record_error("on_message", e)

    def _on_error(self, *args, **kwargs):
        # SDK calls with a single error arg; tolerate any signature.
        err = args[0] if args else kwargs.get("error", "")
        self._log(f"[quote_feed] on_error: {err}")
        with self._lock:
            self._connected = False
            self._errors.append(f"{int(time.time())} on_error: {err}")
            if len(self._errors) > 50:
                self._errors = self._errors[-50:]

    def _on_open(self, *args, **kwargs):
        # SDK passes a banner string like "The Session has been Opened!"
        self._log(f"[quote_feed] socket open ({args[0] if args else ''})")
        with self._lock:
            self._connected = True

    def _on_close(self, *args, **kwargs):
        self._log(f"[quote_feed] socket closed ({args[0] if args else ''})")
        with self._lock:
            self._connected = False

    def _attach_callbacks(self, client):
        # The NeoAPI client exposes these as plain attributes that the SDK
        # forwards into NeoWebSocket on subscribe().
        client.on_message = self._on_message
        client.on_error = self._on_error
        client.on_open = self._on_open
        client.on_close = self._on_close

    def _all_subs_snapshot(self):
        with self._subs_lock:
            return (list(self._index_subs),
                    list(self._scrip_subs),
                    list(self._option_subs),
                    list(self._future_subs))

    def _do_subscribe(self, client):
        """(Re)subscribe to current index + scrip + option + future sets."""
        idx_subs, scrip_subs, opt_subs, fut_subs = self._all_subs_snapshot()
        # Indices must use isIndex=True; non-index uses False.
        if idx_subs:
            try:
                client.subscribe(instrument_tokens=idx_subs, isIndex=True)
            except Exception as e:
                self._record_error("subscribe(index)", e)
        non_index = scrip_subs + opt_subs + fut_subs
        if non_index:
            try:
                client.subscribe(instrument_tokens=non_index, isIndex=False)
            except Exception as e:
                self._record_error("subscribe(scrip+option+future)", e)
        self._last_subscribed_at = time.time()

    def _run(self):
        """Subscribe once. The Kotak SDK runs the WS in its own daemon thread
        with run_forever(reconnect=5), so we don't need an outer reconnect
        loop — just monitor for option-sub changes (ATM drift) and resubscribe.
        """
        try:
            client = self._client_provider()
            self._client = client
            self._attach_callbacks(client)
            self._do_subscribe(client)
        except Exception as e:
            self._record_error("initial_subscribe", e)
            self._log(traceback.format_exc())
            return

        last_opt_subs = list(self._option_subs)
        last_fut_subs = list(self._future_subs)
        # D.4 — track previous as frozenset alongside list so the per-tick
        # equality check is order-insensitive. Avoids the every-2s socket
        # churn that was visible as 'future subs changed (3 -> 3)' in
        # data/app.log.
        last_opt_set = self._subs_set(last_opt_subs)
        last_fut_set = self._subs_set(last_fut_subs)
        while not self._stop.is_set():
            self._stop.wait(2.0)
            with self._subs_lock:
                cur_opts = list(self._option_subs)
                cur_futs = list(self._future_subs)
            cur_opt_set = self._subs_set(cur_opts)
            cur_fut_set = self._subs_set(cur_futs)
            if cur_opt_set != last_opt_set:
                self._log(f"[quote_feed] option subs changed "
                          f"({len(last_opt_subs)} -> {len(cur_opts)}), "
                          "resubscribing")
                try:
                    # Subscribe only the new (delta) — SDK accumulates subs.
                    # Set-difference on identity tuples avoids dict-equality
                    # quirks that leaked through the earlier 'in list' test.
                    new_keys = cur_opt_set - last_opt_set
                    new_set = [s for s in cur_opts
                               if self._sub_key(s) in new_keys]
                    if new_set:
                        try:
                            self._client.subscribe(instrument_tokens=new_set,
                                                   isIndex=False)
                        except Exception as e:
                            self._record_error("subscribe(option_delta)", e)
                except Exception as e:
                    self._record_error("resubscribe", e)
                last_opt_subs = cur_opts
                last_opt_set = cur_opt_set
            if cur_fut_set != last_fut_set:
                self._log(f"[quote_feed] future subs changed "
                          f"({len(last_fut_subs)} -> {len(cur_futs)}), "
                          "resubscribing")
                try:
                    new_keys = cur_fut_set - last_fut_set
                    new_set = [s for s in cur_futs
                               if self._sub_key(s) in new_keys]
                    if new_set:
                        try:
                            self._client.subscribe(instrument_tokens=new_set,
                                                   isIndex=False)
                        except Exception as e:
                            self._record_error("subscribe(future_delta)", e)
                except Exception as e:
                    self._record_error("resubscribe(future)", e)
                last_fut_subs = cur_futs
                last_fut_set = cur_fut_set

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

        # Reconnect signalling. Set by on_error/on_close (SDK thread);
        # cleared by _run after a successful reconnect attempt. Plain
        # bool reads/writes are atomic in CPython, so no extra lock.
        # Pre-this-flag, on_error logged the disconnect but the socket
        # was never restarted — dashboard would go dark until manual
        # systemctl restart (observed Fri May 1 13:30 IST production).
        self._needs_reconnect = False

        # OHLC snapshot state. Kotak's index WS pushes only LTP-only ticks
        # for indices most of the time; OHLC fields (op/lo/h/c) arrive
        # sporadically — typically a single snapshot frame around session
        # open. After a mid-session restart we never see that frame, so
        # OPEN stays None and Gann levels can't compute. Fix: explicitly
        # request an OHLC snapshot via the SDK's SNAP_IF protocol message
        # ("ifsp") right after subscribe. Pure WS, no REST. Gated to fire
        # only after 09:15 IST so we never pick up a pre-market /
        # previous-day candle which would corrupt today's stop-loss
        # ladders. _snap_pending stays True across loop iterations until
        # the WS is open AND time has crossed 09:15 — so a pre-market
        # auto-login subscribe still gets its OHLC the moment the bell
        # rings.
        self._snap_pending = False
        self._last_snap_at = 0.0

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

    def seed_index_op(self, exchange, token, op):
        """Seed an index opening price into the WS cache only if WS has not
        already written one.  Called from the REST-OHLC fallback in quotes.py
        when Kotak's WS fails to deliver openingPrice for NIFTY 50 (known
        broker bug).  WS ticks always win: if the cache already has a non-zero
        op this is a silent no-op."""
        key = (str(exchange).lower(), str(token).lower())
        with self._lock:
            prev = self._cache.get(key, {})
            if prev.get("op"):
                return  # WS already populated it — don't overwrite
            merged = dict(prev)
            merged["op"] = float(op)
            merged.setdefault("ts", time.time())
            self._cache[key] = merged

    def clear_cache(self):
        """Wipe the in-memory tick cache atomically. Called from the
        08:45 IST auto-login routine so yesterday's op/lo/h/c can never
        bleed into today's session.

        Why this matters: Kotak's WebSocket is unreliable about pushing
        a fresh `op` for NIFTY 50 — most days it sends today's open in
        the session-open snapshot frame, but on bad days (or after a
        mid-session reconnect) it sends only LTP-only ticks. If the
        process has been running across midnight without restart, the
        cache still holds yesterday's op for NIFTY 50, and without an
        explicit wipe at 08:45 it will silently surface yesterday's
        OPEN as today's — which in turn anchors the wrong Gann ladder
        and triggers wrong stop-loss orders.

        BANKNIFTY and SENSEX usually get a fresh op on subscribe, so
        for them the wipe is belt-and-suspenders. For NIFTY 50 it is
        the only line of defence (until the Kotak NIFTY-50-OPEN bug
        is solved at the source).

        Returns the number of cache entries removed (for logging)."""
        with self._lock:
            n = len(self._cache)
            self._cache.clear()
            self._last_tick_ts = 0
            return n

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
        # Signal the monitor loop to reconnect — Kotak SDK does NOT
        # auto-reconnect after socket errors; the prior assumption that
        # run_forever(reconnect=5) handled this was wrong.
        self._needs_reconnect = True

    def _on_open(self, *args, **kwargs):
        # SDK passes a banner string like "The Session has been Opened!"
        self._log(f"[quote_feed] socket open ({args[0] if args else ''})")
        with self._lock:
            self._connected = True

    def _on_close(self, *args, **kwargs):
        self._log(f"[quote_feed] socket closed ({args[0] if args else ''})")
        with self._lock:
            self._connected = False
        self._needs_reconnect = True

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
        # SNAP_IF disabled — the raw "ifsp" hs_send caused Kotak to
        # close the WS immediately on every reconnect, putting the
        # feed in a tight reconnect→snap→disconnect loop (observed
        # 2026-05-04, ~16:50 IST in data/app.log). Until we figure
        # out the correct snapshot path (likely needs the SDK's
        # call_quotes / get_live_feed wrappers, not raw hs_send),
        # leave _snap_pending=False so the deferred-pending block in
        # _run() stays a no-op. _try_send_index_snap is kept for
        # reference but never called.
        self._snap_pending = False

    def _try_send_index_snap(self):
        """Fire a one-shot OHLC snapshot request for index subs over WS.

        Returns True if the request was sent, False if deferred (socket
        not open yet, pre-market, or no index subs). Caller (the run
        loop) should leave _snap_pending=True on a False return so the
        next iteration retries.

        Why SNAP_IF: Kotak's regular index subscription only carries
        OHLC in occasional snapshot frames; a mid-session reconnect
        therefore sees LTP-only ticks indefinitely. SNAP_IF ("ifsp")
        is a Kotak protocol message that demands a current OHLC frame
        — the SDK uses it internally for `quotes(quote_type="ohlc")`
        when the WS is up. We hit hsWebsocket.hs_send directly because
        the public SDK surface doesn't expose a snap-only call. The
        response arrives via the same on_hsm_message path our
        existing _on_message handler already parses (extracts op, lo,
        h, c into _cache).
        """
        client = self._client
        if not client:
            return False
        try:
            ws = client.NeoWebSocket
            if not ws or getattr(ws, "is_hsw_open", 0) != 1:
                return False
            hsm = ws.hsWebsocket
            if not hsm:
                return False
        except AttributeError:
            return False

        # Gate by IST clock — pre-market SNAP can return previous-day
        # OHLC, which would seed wrong open/low/high into the cache and
        # corrupt every Gann level + stop-loss for the rest of the day.
        # 09:15 is NSE/BSE cash open; anything earlier we defer.
        try:
            from backend.utils import now_ist
            ist = now_ist()
            if (ist.hour, ist.minute) < (9, 15):
                return False
        except Exception:
            return False

        idx_subs = list(self._index_subs)
        if not idx_subs:
            # No index tokens means nothing to snap. We don't snap
            # options/futures here — Gann levels are computed off
            # spot OPEN, which only indices need. Live LTP for the
            # F&O legs comes through normal subscribe ticks.
            return False

        scrips = "&".join(
            f"{s['exchange_segment']}|{s['instrument_token']}"
            for s in idx_subs
        )
        try:
            hsm.hs_send(json.dumps(
                {"type": "ifsp", "scrips": scrips, "channelnum": 1}))
            self._last_snap_at = time.time()
            self._log(f"[quote_feed] SNAP_IF sent for {len(idx_subs)} "
                      f"indices ({scrips})")
            return True
        except Exception as e:
            self._record_error("snap_if", e)
            return False

    def _run(self):
        """Subscribe once, then monitor in a loop.

        Two responsibilities per iteration:
          (1) Reconnect-on-disconnect. The Kotak SDK does NOT auto-
              reconnect after socket errors — earlier assumption that
              run_forever(reconnect=5) handled it was wrong (proven by
              Fri May 1 13:30 IST production drop: on_error logged,
              dashboard went dark for 65+ hours until manual restart).
              When _needs_reconnect is set by on_error/on_close, walk
              an exponential backoff (2s, 4s, 8s, 16s, capped 30s),
              re-fetch client (handles login refresh), reattach
              callbacks, re-subscribe all sets, increment _reconnects.
          (2) Option/future ATM drift — resubscribe deltas.
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
        reconnect_attempt = 0
        while not self._stop.is_set():
            self._stop.wait(2.0)

            # ---- (0) Deferred OHLC snapshot ----
            # _snap_pending was set by _do_subscribe; we keep retrying
            # until the WS handshake completes AND the IST clock is past
            # 09:15. Once a SNAP_IF goes out, _try_send_index_snap returns
            # True and we clear the flag. Polled every 2s — well under the
            # latency a human would notice on the dashboard.
            if self._snap_pending and not self._stop.is_set():
                if self._try_send_index_snap():
                    self._snap_pending = False

            # ---- (1) Reconnect on dead socket ----
            if self._needs_reconnect and not self._stop.is_set():
                reconnect_attempt += 1
                # Exponential backoff: 2, 4, 8, 16, 30, 30, ...
                backoff = min(2.0 * (2 ** (reconnect_attempt - 1)), 30.0)
                self._log(f"[quote_feed] WS disconnected; reconnect "
                          f"attempt #{reconnect_attempt} in {backoff:.0f}s")
                if self._stop.wait(backoff):
                    break
                try:
                    client = self._client_provider()
                    self._client = client
                    self._attach_callbacks(client)
                    self._do_subscribe(client)
                    with self._lock:
                        self._reconnects += 1
                    self._needs_reconnect = False
                    reconnect_attempt = 0
                    self._log(f"[quote_feed] reconnect #"
                              f"{self._reconnects} subscribed")
                    # Reset delta-tracking sets so we don't trigger a
                    # spurious option/future re-subscribe on next iter
                    # (we just full-subscribed everything).
                    last_opt_subs = list(self._option_subs)
                    last_fut_subs = list(self._future_subs)
                    last_opt_set = self._subs_set(last_opt_subs)
                    last_fut_set = self._subs_set(last_fut_subs)
                    # No tick-flow verification on purpose: off-hours
                    # the socket is healthy but no ticks come. If the
                    # socket actually died, on_error / on_close will
                    # re-trigger _needs_reconnect on the next failure.
                except Exception as e:
                    self._record_error(
                        f"reconnect#{reconnect_attempt}", e)
                # Skip the option/future delta block this iter so we
                # don't redundantly resubscribe on top of the full
                # _do_subscribe we just ran.
                continue

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

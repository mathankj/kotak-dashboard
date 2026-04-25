# Architecture

The app is a single Flask process. Browsers poll JSON APIs every couple of
seconds, and the server keeps a long-lived Kotak Neo session in memory plus a
WebSocket subscribed to the live quote feed.

## Module map

```
                           +--------------------------+
   browser  --->  app.py   |  routes (page + JSON)    |
                           +-----------+--------------+
                                       |
                  +--------------------+--------------------+
                  |                    |                    |
        backend.quotes        backend.strategy        backend.storage
        ---------------       ----------------        ----------------
        fetch_quotes          gann.gann_levels        trades.read/write
        fetch_option_quotes   stocks.auto_strategy    orders.append
        _ws_overlay           options.option_auto     history.append
        _feed (WS)
                  |
        backend.kotak
        --------------
        client.ensure_client  -- TOTP login + session cache
        client.safe_call      -- rate-limited, retried, breaker-guarded
        api.call_with_retry   -- shared resilience primitives
        instruments.SCRIPS    -- watchlist + F&O universe lookup
        quote_feed.QuoteFeed  -- WebSocket subscription + tick cache
```

Every backend module is plain Python — no Flask import, no request context.
The route layer in `app.py` is the only thing that touches Flask.

## Data flow: a Gann tick

1. Browser hits `/api/gann-prices` every 2 s.
2. `backend.quotes.fetch_quotes()` returns its TTL-cached `{symbol: {...}}`
   dict, falling back to a Kotak REST call when stale.
3. `_ws_overlay()` overwrites LTPs with WebSocket ticks newer than 5 s; if
   REST didn't return OHLC (e.g. off-hours), it backfills from the cached
   tick and recomputes Gann levels.
4. `backend.strategy.stocks.auto_strategy_tick()` walks the new quotes,
   detects BUY/SELL level crossings, and writes paper trades to
   `paper_trades.json`.
5. `update_open_trades_mfe()` walks every OPEN paper trade and updates
   `max_min_target_price` + the deepest Gann level reached so the UI can
   show how far in favour each trade went.
6. The route returns the per-symbol rows + a stats summary; the dashboard
   re-renders.

The option chain (`/api/option-prices`) follows the same shape, but it also
asks `_feed` to update its option subscription set whenever the ATM strike
drifts so the WS streams the active ATM±N strikes only.

## State

The app keeps three pieces of long-lived state in memory:

- `backend.kotak.client._state` — the live `NeoAPI` client + login metadata.
  Mutated by `ensure_client()` and the `/refresh` route.
- `backend.quotes._quote_cache` / `_option_quote_cache` — 2-second TTL
  caches; protect Kotak from per-request hammering.
- `backend.quotes._feed` — the WebSocket `QuoteFeed`. Lazily started on the
  first quote fetch, then stays subscribed for the process lifetime.

All three are module-level dicts. No request context, no Flask `g`. That's
intentional — the app is single-process and these caches need to outlive any
one request.

## Resilience: rate limit + breaker + retry

`backend.kotak.api` provides three primitives wired together in
`call_with_retry(name, fn, ...)`:

- **RateLimiter** — token bucket, ~5 req/s. Sleeps the calling thread when
  out of tokens.
- **CircuitBreaker** — opens after 5 consecutive failures, stays open for
  30 s, then half-opens to probe recovery. While open, `call_with_retry`
  raises `CircuitOpenError` immediately.
- **Retry with exponential backoff** — 3 attempts, base delay 0.2 s.

`safe_call(fn, ...)` in `client.py` wraps the SDK call in `call_with_retry`,
turns Kotak's "no holdings found" / "no orders" responses into empty lists,
and surfaces breaker-open as the string `"breaker_open: ..."` so the UI
can render a useful message.

## Storage atomicity

JSON files (`paper_trades.json`, `orders_log.json`, `login_history.json`)
are read and written by multiple request threads. `backend.storage._safe_io`
provides the two primitives that prevent corruption and lost writes:

- `atomic_write_json(path, data)` — writes to `path.tmp` then `os.replace()`,
  so a reader either sees the old or the new file, never a partial write.
- `file_lock(path)` — returns a `threading.Lock` keyed by absolute path, so
  the read-modify-write blocks in `append_order` / `append_history` are
  serialised across threads.

The concurrent-append test in `tests/test_storage.py` proves the lock works:
20 threads each append to `orders_log.json` in parallel and all 20 entries
end up in the file.

## What's not here yet

- **No persistent DB.** Everything is JSON files. SQLite migration is on the
  Contabo VPS roadmap.
- **No SSE / WebSocket push to the browser.** The dashboard polls every 2 s.
  Adding SSE for live PnL is also on the VPS roadmap.
- **No live order placement from the auto-strategy.** `LIVE_MODE` is OFF;
  the auto-strategy modules only write to `paper_trades.json`.

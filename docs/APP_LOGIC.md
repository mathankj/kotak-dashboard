# App Logic — full reference

Single source of truth for what the app does, how it's organised, and what
every HTTP route returns. Read this end-to-end if you want the complete
picture; jump to a section if you don't.

For the trader-facing rule sheet (entry/exit in plain language) see
[FOR_GANESH.md](FOR_GANESH.md).

---

## 1. What the app does

A single Flask process that:

1. Auto-logs into Kotak Neo using TOTP every morning (and on demand).
2. Streams live LTPs over WebSocket and falls back to REST when stale.
3. Computes Gann Square-of-9 levels for a watchlist of stocks and indices.
4. Runs a paper auto-trading strategy on **index options** based on spot
   crossings of those Gann levels (the stock side is currently disabled).
5. Records every paper trade to `paper_trades.json`.
6. Exposes a dashboard for live levels, an option chain, an order log, and
   a one-off manual order form that talks to Kotak's `place_order` (the
   only path to a real order).

Going live (auto-strategy → real Kotak orders) is **deliberately not wired
up**. Paper-only is the safety invariant.

---

## 2. Module layout

```
app.py                       Flask routes (page + JSON entry points)
requirements.txt             Python dependencies
README.md                    project overview + quick start
.env.example                 template for credentials (.env is gitignored)

backend/                     all backend code (no Flask import here)
  utils.py                   IST timezone helper
  quotes.py                  fetch_quotes / fetch_option_quotes + WS overlay
  kotak/                     Kotak Neo SDK wrapper
    api.py                   rate limiter + circuit breaker + retry
    client.py                login(), ensure_client(), safe_call()
    instruments.py           scrip master + F&O universe lookup
    quote_feed.py            WebSocket subscription + tick cache
  storage/                   JSON file persistence
    _safe_io.py              atomic_write_json + per-file locks
    trades.py                data/paper_trades.json
    orders.py                data/orders_log.json
    history.py               data/login_history.json
  strategy/                  paper auto-trading strategies
    gann.py                  Square-of-9 levels + helpers (pure math)
    stocks.py                stock auto-strategy (currently DISABLED)
    options.py               option auto-strategy (ACTIVE)

frontend/                    web UI
  templates/                 Jinja2 HTML templates
  static/                    (empty for now — JS/CSS still inline)

scripts/                     standalone scripts (not part of the Flask app)
  auto_login.py              TOTP login smoke test (Task Scheduler runs this)
  run_login.bat              Task Scheduler wrapper
  setup_schedule.bat         one-time Task Scheduler setup

data/                        runtime data (gitignored, auto-created)
  paper_trades.json
  orders_log.json
  login_history.json
  login_history.log

docs/
  APP_LOGIC.md               this file — full reference
  FOR_GANESH.md              trader-facing rules sheet

tests/                       offline pytest suite
```

Every backend module is plain Python with no Flask import. The route layer
in `app.py` is the only thing that touches Flask.

---

## 3. The Gann ladder

`backend/strategy/gann.py` computes 10 price levels from the day's open.
For each step `n`, the level price is `(sqrt(open) + n * 0.0625) ** 2`:

```
   S3   S2   S1   SELL_WA   SELL    ──[OPEN]──    BUY   BUY_WA   T1   T2   T3
   n=-6 -5    -4    -3       -2                    +2    +3      +4   +5   +6
```

- `BUY` / `SELL` are the entry trigger levels.
- `T1` / `S1` are the profit targets.
- `SELL_WA` / `BUY_WA` are visual cues only — they don't trigger anything.
- `T2` / `T3` / `S2` / `S3` are not triggers; they're used by
  `compute_target_level_reached()` to label how deep a trade ran in its
  favoured direction (the "trade got to T2 before reversing" badge in the
  trade table).

---

## 4. Stock auto-strategy (`backend/strategy/stocks.py`)

**Currently disabled** (`AUTO_STRATEGY_ENABLED = False`).

When enabled, it watches LTP of every tradeable scrip in `SCRIPS` and on a
crossing opens a paper trade. Rules (paper only):

| Trigger | Action |
|---|---|
| LTP crosses BUY ↑ | open paper BUY of `AUTO_QTY` shares |
| LTP crosses SELL ↓ | open paper SELL of `AUTO_QTY` shares |
| BUY trade: LTP ≥ T1 | exit, `TARGET_T1` |
| BUY trade: LTP < SELL | exit, `SL_SELL_LVL` |
| SELL trade: LTP ≤ S1 | exit, `TARGET_S1` |
| SELL trade: LTP > BUY | exit, `SL_BUY_LVL` |
| 15:15 IST and later | force-close all OPEN, `AUTO_SQUARE_OFF` |

Constraints:
- 09:15 – 15:15 IST, weekdays only.
- Max `AUTO_MAX_TRADES_PER_SCRIP` (default 2) entries per symbol per day.
- One open trade per symbol at a time.
- A "crossing" requires the previous tick on one side and the current on the
  other; the very first tick after process start never triggers an entry.

The module is kept intact (and tested) so it can be flipped back on by a
single-flag change.

---

## 5. Option auto-strategy (`backend/strategy/options.py`)

**This is the live one.** Triggers on the **index spot**, but trades the
**ATM CE/PE option** of that index.

### Entry

| Trigger (on index spot) | Action |
|---|---|
| Spot crosses BUY ↑ | paper BUY 1 lot ATM **CE** at current option LTP |
| Spot crosses SELL ↓ | paper BUY 1 lot ATM **PE** at current option LTP |

The Gann levels (BUY / SELL / T1 / S1) come from the **stock-side** quote of
the index spot. That's why the route layer passes `gann_quotes` into
`option_auto_strategy_tick(option_data, option_index_meta, gann_quotes)`
explicitly — without it the option module would have to import
`fetch_quotes`, which would create a circular import.

### Exit — three logics, first hit wins

| # | Logic | Trigger | Reason recorded |
|---|---|---|---|
| 1 | Stop loss (one of 3 variants — see below) | depends on active variant | varies |
| 2 | Profit target | CE: spot ≥ T1 · PE: spot ≤ S1 | `TARGET_T1` / `TARGET_S1` |
| 3 | Time square-off | clock ≥ 15:15 IST | `AUTO_SQUARE_OFF` |

### Stop loss variants (in `_check_exit_reason`)

Three alternatives in the code; **only one is active** at a time. Switch by
commenting / uncommenting.

| Variant | Condition | Reason | Status |
|---|---|---|---|
| A | `option_ltp ≤ entry - PREMIUM_SL_FIXED_POINTS` (default ₹5) | `SL_PREMIUM_FIXED` | commented |
| B | `option_ltp ≤ entry × (1 - PREMIUM_SL_PCT)` (default 30%) | `SL_PREMIUM_PCT` | commented |
| C | CE: `spot < SELL` · PE: `spot > BUY` | `SL_SELL_LVL` / `SL_BUY_LVL` | **ACTIVE** |

### Constraints

- 09:15 – 15:15 IST, weekdays only (shared time gate with stocks).
- `MAX_TRADES_PER_INDEX_PER_DAY = None` → unlimited (set to int to cap).
- One open option trade per underlying index at a time.
- Crossing requires both prev_spot and cur_spot known; first tick after
  restart never triggers.

### Tunables (top of `options.py`)

```python
AUTO_OPTION_STRATEGY_ENABLED  = True
MAX_TRADES_PER_INDEX_PER_DAY  = None    # None = unlimited
PREMIUM_SL_FIXED_POINTS       = 5       # used only by Variant A
PREMIUM_SL_PCT                = 0.30    # used only by Variant B
```

---

## 6. Tick lifecycle

### Stock tick (every 2 s, via `GET /api/gann-prices`)

1. Browser polls `/api/gann-prices`.
2. `fetch_quotes()` returns its TTL-cached `{symbol: {ltp, open, low, high,
   levels, ...}}`, refreshing from Kotak REST when stale (> 2 s).
3. `_ws_overlay()` overwrites LTPs with WebSocket ticks newer than 5 s. If
   REST didn't return OHLC (e.g. off-hours), it backfills from the cached
   WS tick and recomputes Gann levels.
4. `auto_strategy_tick(quotes)` runs (no-op while disabled).
5. `update_open_trades_mfe(quotes)` walks every OPEN trade and refreshes
   `max_min_target_price` + `target_level_reached`.
6. Route returns per-symbol rows + `stats = {active, closed, pnl}`.

### Option tick (every 2 s, via `GET /api/option-prices`)

1. `fetch_option_quotes()` returns the option chains per index.
2. `fetch_quotes()` is also called to get the Gann levels of each index
   spot (the option strategy triggers on spot crossings).
3. `option_auto_strategy_tick(option_data, option_index_meta, gann_quotes)`
   runs: time-gate, square-off, exits-checked-before-entries per index.
4. The route also asks `_feed` to re-subscribe its option set if the ATM
   strike has drifted as spot moved.

---

## 7. Trade record shape

Every paper trade — stock or option — is one JSON object in
`paper_trades.json`. Same file, mixed records, distinguished by
`asset_type`.

### Stock trade

```json
{
  "id": "42",
  "date": "2026-04-25",
  "scrip": "RELIANCE",
  "order_type": "BUY",
  "entry_time": "10:14:32",
  "entry_ts":   1745568872.5,
  "entry_price": 2870.40,
  "qty": 1,
  "max_min_target_price": 2882.10,
  "target_level_reached": "T1",
  "exit_time":  "11:02:18",
  "exit_price": 2880.50,
  "exit_reason": "TARGET_T1",
  "pnl_points":  10.10,
  "pnl_pct":     0.35,
  "duration_seconds": 2865.5,
  "status": "CLOSED",
  "auto":   true
}
```

### Option trade

Adds `asset_type: "option"` plus option-specific fields:

```json
{
  "id": "43",
  "date": "2026-04-25",
  "scrip": "NIFTY 50 22500 CE",
  "option_key": "NIFTY 50 22500 CE",
  "asset_type": "option",
  "underlying": "NIFTY 50",
  "strike": 22500,
  "option_type": "CE",
  "expiry": "2026-04-30",
  "order_type": "BUY",
  "entry_price": 142.50,
  "qty": 1,
  "trigger_spot":  22487.30,
  "trigger_level": "BUY",
  ...
}
```

`order_type` is always `BUY` for option trades — there is no short-option
side. A bearish view becomes "buy a PE", not "sell a CE".

### MFE fields (updated continuously while OPEN)

- `max_min_target_price` — most-favourable price seen since entry (max for
  BUY, min for SELL).
- `target_level_reached` — deepest Gann level the trade touched in its
  favour (`BUY`, `BUY_WA`, `T1`, `T2`, `T3`, or `Beyond T3`). Frozen at
  close.

---

## 8. State

Three pieces of long-lived state in memory:

- `backend.kotak.client._state` — live `NeoAPI` client + login metadata.
  Mutated by `ensure_client()` and `/refresh`.
- `backend.quotes._quote_cache` / `_option_quote_cache` — 2-second TTL
  caches; protect Kotak from per-request hammering.
- `backend.quotes._feed` — the WebSocket `QuoteFeed`. Lazily started on
  the first quote fetch, then stays subscribed for the process lifetime.

All three are module-level dicts. No request context, no Flask `g`. That's
intentional — the app is single-process and these caches need to outlive
any one request.

---

## 9. Resilience: rate limit + breaker + retry

`backend.kotak.api` provides three primitives wired together in
`call_with_retry(name, fn, ...)`:

- **RateLimiter** — token bucket, ~5 req/s. Sleeps the calling thread when
  out of tokens.
- **CircuitBreaker** — opens after 5 consecutive failures, stays open for
  30 s, then half-opens to probe recovery. While open, `call_with_retry`
  raises `CircuitOpenError` immediately.
- **Retry with exponential backoff** — 3 attempts, base delay 0.2 s.

`safe_call(fn, ...)` in `client.py` wraps the SDK call in
`call_with_retry`, turns Kotak's "no holdings found" / "no orders"
responses into empty lists, and surfaces breaker-open as the string
`"breaker_open: ..."` so the UI can render a useful message.

---

## 10. Storage atomicity

JSON files (`paper_trades.json`, `orders_log.json`, `login_history.json`)
are read and written by multiple request threads.
`backend.storage._safe_io` provides:

- `atomic_write_json(path, data)` — writes to `path.tmp` then
  `os.replace()`, so a reader either sees the old or the new file, never
  a partial write.
- `file_lock(path)` — returns a `threading.Lock` keyed by absolute path,
  so the read-modify-write blocks in `append_order` / `append_history` are
  serialised across threads.

The concurrent-append test in `tests/test_storage.py` proves it: 20
threads each append to `orders_log.json` in parallel and all 20 entries
end up in the file.

---

## 11. HTTP routes

All in `app.py`. Page routes return rendered HTML; `/api/*` routes return
JSON. POST routes expect `application/json` unless noted.

### Page routes

| Method | Path         | What it shows |
|--------|--------------|------------------------------------------------|
| GET    | `/`          | Portfolio holdings (live Kotak data)           |
| GET    | `/positions` | Open intraday positions                        |
| GET    | `/orders`    | Order book                                     |
| GET    | `/trades`    | Trade book                                     |
| GET    | `/limits`    | Funds and segment limits                       |
| GET    | `/history`   | Last 30 login attempts (success / failure)     |
| GET    | `/gann`      | Gann Square-of-9 dashboard for SCRIPS          |
| GET    | `/options`   | Index option chains (NIFTY/BANKNIFTY/SENSEX)   |
| GET    | `/orderlog`  | Audit log of every `/api/place-order` attempt  |
| GET/POST | `/refresh` | Forces re-login (clears cached session)        |

### Quote APIs

- `GET /api/feed-status` — WebSocket QuoteFeed diagnostics.
- `GET /api/health` — liveness check (WS + last fetch time).
- `GET /api/gann-prices` — live LTP + Gann levels for each SCRIPS row.
  Side effects: stock auto-strategy tick + MFE update. Returns
  `stats = {active, closed, pnl}` for the header counters.
- `GET /api/option-prices` — option chains + per-index meta. On first call
  warms the option universe in a background thread and returns
  `loading: true`. Side effect: option auto-strategy tick.

### Paper-trading APIs (manual, bypass strategy)

- `POST /api/paper-open  { symbol, side, qty }` — opens a paper trade at
  current LTP.
- `POST /api/paper-close { id }` — closes an OPEN paper trade at current
  LTP, records `MANUAL` reason.
- `GET  /api/paper-trades` — full `paper_trades.json` (newest first).
- `GET  /paper-trades.xlsx` — same data as a downloadable Excel file
  (uses `openpyxl`).

### Order APIs

- `GET  /api/margin-summary` — cash, total margin, margin used.
- `POST /api/place-order` — **the only endpoint that talks to Kotak's
  order endpoint.** Every attempt (success or any error) is appended to
  `orders_log.json` with the full request/response for audit.
- `GET  /orderlog.csv` — same data as a downloadable CSV.

### Failure modes

- **Login fails** — same template re-rendered with `error=...` banner; a
  failed attempt is appended to `login_history.json`.
- **Kotak rate-limits** — `safe_call` retries up to 3× with exponential
  backoff; if retries exhaust, the breaker opens and subsequent calls
  return `error: "breaker_open: ..."` for 30 s.
- **Quote feed dies** — REST is the fallback; the WS overlay only
  overrides REST when its tick is < 5 s old. The dashboard keeps working
  off REST polls.
- **Paper file corruption** — `read_paper_trades()` falls back to `[]` on
  any parse error so strategies see an empty book rather than crashing.

---

## 12. Going live (intentionally not wired up)

To switch the auto-strategy from paper to real:

1. Add a module-level `LIVE_MODE = False` flag.
2. Inside `_auto_open` / `_auto_close` (or the option equivalents in
   `options.py`), when `LIVE_MODE` is True, also call
   `client.place_order(...)` via `safe_call`.
3. Reuse the `/api/place-order` audit pattern — every live call should
   land in `orders_log.json`.

This is **deliberately not done**. Paper-only is the architecture's
safety invariant. Going live is a Contabo VPS roadmap item with extra
checks (margin, max daily loss, kill switch) before any real order goes
through.

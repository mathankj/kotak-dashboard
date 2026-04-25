# HTTP API

All routes live in `app.py`. Page routes return rendered HTML; `/api/*`
routes return JSON. POST routes expect `application/json` unless noted.

## Pages

| Method | Path         | What it shows                                  |
|--------|--------------|------------------------------------------------|
| GET    | `/`          | Portfolio holdings (live Kotak data)           |
| GET    | `/positions` | Open intraday positions                        |
| GET    | `/orders`    | Order book                                     |
| GET    | `/trades`    | Trade book                                     |
| GET    | `/limits`    | Funds and segment limits                       |
| GET    | `/history`   | Last 30 login attempts (success / failure)     |
| GET    | `/gann`      | Gann Square-of-9 dashboard for SCRIPS          |
| GET    | `/options`   | Index option chains (NIFTY/BANKNIFTY/etc.)     |
| GET    | `/orderlog`  | Audit log of every `/api/place-order` attempt  |
| GET/POST | `/refresh` | Forces re-login (clears cached session)        |

The first six are simple table renders backed by `safe_call(client.X)`. The
Gann and Options pages are JS-driven — they render the shell from the Jinja
template and then poll the JSON APIs below.

## Quote APIs

### `GET /api/feed-status`
WebSocket QuoteFeed diagnostics. Returns:
```json
{ "connected": true, "subs_index": 3, "subs_scrip": 12,
  "cached_keys": 15, "logged_in": true, "ts": "10:42:15 IST" }
```

### `GET /api/health`
Liveness check. Returns the status of the WS feed plus the time of the last
fetch. Used by the canary later, also handy for uptime checks.

### `GET /api/gann-prices`
Live LTP + Gann levels for every entry in `SCRIPS`. Each row:
```json
{ "symbol": "RELIANCE", "ltp": 2870.4, "open": 2854.0,
  "low": 2850.1, "high": 2878.0,
  "levels": { "sell": {...}, "buy": {...} },
  "nearest_level": "BUY", "ws_age": 1.2 }
```
Side effects: runs the stock auto-strategy tick and updates each open
trade's MFE / target_level_reached. Return body also carries
`stats = { active, closed, pnl }` for the header counters.

### `GET /api/option-prices`
Returns the option chains for all configured indices, plus per-index meta
(`spot`, `atm`, `expiry`). On first call it lazily warms the option universe
in a background thread and returns `loading: true`.

Side effect: runs the option auto-strategy tick (passing `gann_quotes` so
the strategy module doesn't have to import `fetch_quotes`).

## Paper-trading APIs

### `POST /api/paper-open`
Body: `{ "symbol": "RELIANCE", "side": "B", "qty": 1 }`. Opens a paper
trade at the current LTP. Returns the new trade record.

### `POST /api/paper-close`
Body: `{ "id": "42" }`. Closes the trade at the current LTP and records
PnL + duration + the deepest Gann level reached.

### `GET /api/paper-trades`
Returns the full `paper_trades.json` file (newest first).

### `GET /paper-trades.xlsx`
Same data as a downloadable Excel file (uses `openpyxl` if available).

## Orders APIs

### `GET /api/margin-summary`
Reads `client.limits` and surfaces the margin numbers the place-order form
needs (cash, total margin, margin used).

### `POST /api/place-order`
**The only endpoint that talks to Kotak's order endpoint.** Every attempt
(success or any error) is appended to `orders_log.json` with the full
request/response for audit. Body matches Kotak's `place_order` parameters.

### `GET /orderlog.csv`
Same data as a downloadable CSV. Used by the user to spot-check against
the broker's own order book at end of day.

## What goes wrong, and how

- **Login fails:** the route routes to the same template with
  `error=...` rendered in the banner. A failed attempt is appended to
  `login_history.json`.
- **Kotak rate-limits:** `safe_call` retries up to 3 times with exponential
  backoff. If retries exhaust the circuit breaker opens; subsequent calls
  return `error: "breaker_open: ..."` for 30 s.
- **Quote feed dies:** REST is the fallback. `_ws_overlay` only overrides
  REST when its tick is < 5 s old; if WS goes stale the dashboard keeps
  working off REST polls.
- **Paper file corruption:** `read_paper_trades()` falls back to `[]` on any
  parse error — strategies see an empty book rather than crashing.

# App Inventory — Kotak Neo Dashboard

A complete catalog of every route, every page, and every table column the
dashboard renders today. Used as the ground truth for the UI audit and the
improvements doc — so we can spot column drift between paper-vs-real, missing
columns vs Zerodha/Sensibull/Dhan, and dead pages.

Source-of-truth files:
- Routes: `app.py`
- Templates: `frontend/templates/*.html`
- Cross-ref companion docs: `COMPETITOR_REVIEW.md`, `STRUCTURE_REVIEW.md`

---

## 1. Routes (full list)

### 1.1 HTML pages (server-rendered)

| Route          | View func          | Template          | Heading                  | Live updates? |
| -------------- | ------------------ | ----------------- | ------------------------ | ------------- |
| `/`            | `holdings_view`    | `base.html`       | Portfolio Holdings       | No (static)   |
| `/positions`   | `positions_view`   | `base.html`       | Open Positions           | No (static)   |
| `/orders`      | `orders_view`      | `base.html`       | Order Book               | No (static)   |
| `/trade-book`  | `trade_book_view`  | `base.html`       | Trade Book (Kotak)       | No (static)   |
| `/limits`      | `limits_view`      | `base.html`       | Funds & Limits           | No (static)   |
| `/history`     | `history_view`     | `history.html`    | Login History            | No            |
| `/gann`        | `gann_view`        | `gann.html`       | Gann Square-of-9 levels  | Yes (≈500ms LTP via `/api/gann-live`, 2s levels via `/api/gann-prices`) |
| `/options`     | `options_view`     | `options.html`    | Option Chain             | Yes (poller hits `/api/option-prices` every 2s) |
| `/futures`     | `futures_view`     | `futures.html`    | Futures dashboard        | Yes (poller hits `/api/future-prices` every 2s) |
| `/trades`      | `trades_view`      | `trade_ledger.html` | Real-trade ledger      | Partial (live LTP for open rows) |
| `/paper-trades`| `paper_trades_view`| `paper_trades.html` | Paper-trade ledger     | Yes (poller hits `/api/paper-trades-live` every 2s) |
| `/blockers`    | `blockers_view`    | `blockers.html`   | Blocked order attempts   | Yes (poller hits `/api/blocked-list` every 3s) |
| `/audit`       | `audit_view`       | `audit.html`      | Audit log                | No (paginated) |
| `/config`      | `config_view`      | `config.html`     | Engines + indices config | No |
| `/orderlog`    | `orderlog_view`    | `orderlog.html`   | Order placement log      | No |
| `/STOP`        | `stop_view`        | `stop_confirm.html` | Kill-switch confirm    | No |

### 1.2 JSON APIs (read-mostly)

| Route                      | Returns                          | Cadence (when polled) | Source                  |
| -------------------------- | -------------------------------- | --------------------- | ----------------------- |
| `/api/feed-status`         | WS feed health diag              | one-shot              | `_feed.status()`        |
| `/api/health`              | Strong-API stats + feed + login  | one-shot              | `kotak.api.stats`       |
| `/api/gann-prices`         | Levels + LTP per scrip           | 2s (Gann page)        | SnapshotStore           |
| `/api/gann-live`           | LTP + nearest-level only         | 500ms (Gann page)     | WS QuoteFeed cache      |
| `/api/option-prices`       | Full option chain payload        | 2s (Options page)     | SnapshotStore           |
| `/api/snapshot-stats`      | Producer build-ms histogram      | one-shot              | `_snapshot.stats()`     |
| `/api/future-prices`       | Per-index spot/future/signal     | 2s (Futures page)     | SnapshotStore           |
| `/api/trades`              | Real-trade ledger JSON           | one-shot              | trade store             |
| `/api/paper-trades-live`   | Paper ledger w/ live LTP overlay | 2s (Paper page)       | paper book + WS LTP     |
| `/api/recent-blocks`       | Last N blocked attempts (toaster) | 3s (toaster on every page) | `blocked.jsonl`     |
| `/api/blocked-list`        | Paginated blocks (page+date)     | 3s (Blockers page)    | `blocked.jsonl`         |
| `/api/config` (GET/POST)   | Engines + indices config         | one-shot              | `config.yaml`           |
| `/api/margin-summary`      | Funds / margin                   | one-shot              | broker `limits()`       |
| `/api/place-order` (POST)  | Place + ack order                | manual                | `place_order_safe`      |

### 1.3 Side-effects / writes

| Route                  | Method      | Effect                                |
| ---------------------- | ----------- | ------------------------------------- |
| `/refresh`             | POST/GET    | Re-runs auto-login                    |
| `/STOP/confirm`        | POST        | Engages kill switch                   |
| `/api/place-order`     | POST        | Places order via safety wrapper       |
| `/paper-trades.xlsx`   | GET         | Downloads paper book as XLSX          |
| `/trades.xlsx`         | GET         | Downloads real ledger as XLSX         |
| `/orderlog.csv`        | GET         | Downloads order log CSV               |

---

## 2. Table columns per page

### 2.1 base.html — broker passthrough pages
`/`, `/positions`, `/orders`, `/trade-book`, `/limits` all funnel raw broker
JSON through `render()`, which auto-generates columns from the dict keys
returned by Kotak. **Columns are whatever Kotak sends back** — there is no
curation, no rename, no ordering hint, no conditional formatting. (UI audit
will flag this as the #1 polish gap.)

### 2.2 `/gann` — `gann.html`
Two-row grouped header:
- Row 1 (groups): SCRIP, P&L, LIVE P&L, QTY, OPEN, LOW, HIGH, **SELL LEVELS** (colspan 7), LTP, **BUY LEVELS** (colspan 7)
- Row 2 (under SELL): S5, S4, S3, S2, S1, WA, SELL
- Row 2 (under BUY):  BUY, WA, T1, T2, T3, T4, T5

### 2.3 `/options` — `options.html`
Strike-centred chain:
- Header row 1: **CALL (CE)** (colspan 2), Strike (rowspan 2), **PUT (PE)** (colspan 2)
- Header row 2 (under CE): LTP, Chg %
- Header row 2 (under PE): LTP, Chg %

Auto-trades panel (separate table on same page):
- Time, Underlying, Contract, Trigger, Entry, Current/Exit, P&L pts, Status

### 2.4 `/futures` — `futures.html`
Main grid:
- Index, Contract, Expiry, **Lot × Mult = Qty**, Spot, BUY level, SELL level, Future LTP, Signal, Step, Limit (BUY ↓), Limit (SELL ↑)

Auto-trades panel:
- Time, Side, Underlying, Contract, Trigger, Qty, Entry, Current/Exit, **P&L pts**, Status

### 2.5 `/trades` — `trade_ledger.html` (REAL trade ledger)
- Mode, Date, Scrip, Side, Entry Time, Entry, Exit Time, Exit, Exit Spot, Exit Reason, P&L pts, P&L %, **Trail SL** (conditional), Duration

### 2.6 `/paper-trades` — `paper_trades.html` (PAPER book)
- Mode, Date, Asset, Scrip, Side, **Entry Reason**, **Trigger Lvl**, **Trigger Spot**, Entry Time, Entry, **Live LTP**, **Live Spot**, Exit Time, Exit, Exit Spot, Exit Reason, P&L pts, P&L %, **Trail SL** (conditional), Duration

> **Column drift:** Paper has `Asset`, `Entry Reason`, `Trigger Lvl`, `Trigger Spot`, `Live LTP`, `Live Spot` that the real ledger does **not**.
> Real ledger lacks the "why this fired" trigger-context columns, so the post-mortem you can do on a paper trade is strictly richer than what you can do on a real one. Improvements doc must call this out.

#### Underlying ledger record-field gap (data, not just display)

| Field                     | Paper Book | Trade Log |
| ------------------------- | ---------- | --------- |
| `entry_reason`            | ✓          | ✗         |
| `instrument_token`        | ✓          | ✗         |
| `exchange_segment`        | ✓          | ✗         |
| `auto`                    | ✗          | ✓         |
| `kotak_entry_order_id`    | ✗          | ✓         |
| `kotak_exit_order_id`     | ✗          | ✓         |
| All other shared fields (id, scrip, side, entry/exit prices, P&L, trail_sl_*, status, mode, etc.) | ✓ | ✓ |

This is a **schema** gap, not just a UI omission — the live ledger physically
doesn't store `entry_reason` or `instrument_token`, so even if we add the
columns to the UI we can't backfill historical rows. Improvements doc should
call out: (a) start writing `entry_reason` into the live ledger now,
(b) add the columns to the UI, (c) accept that pre-fix trades will show "—".

### 2.7 `/blockers` — `blockers.html`
- Time, (BLOCKED pill), Kind, Scrip, Side, Qty, Price, Reason, Message, Source

### 2.8 `/audit` — `audit.html`
- Time (IST), Event, Details (free-form key=value pre block)

### 2.9 `/history` — `history.html`
- Timestamp, Status, Detail

### 2.10 `/orderlog` — `orderlog.html`
- Time, Symbol, Side, Qty, Type, Price, Product, Validity, Status, Kotak Order ID, Message

### 2.11 `/config` — `config.html`
Two-column matrix:
- Paper Book, Real Trade
(Each row is an engine/index toggle row; paper-vs-real are independent on/off.)

---

## 3. Live-update map

| Page         | Poll target              | Interval | Payload size | Notes |
| ------------ | ------------------------ | -------- | ------------ | ----- |
| `/gann`      | `/api/gann-live`         | 500 ms   | 1–2 KB       | Sub-second LTP repaint |
| `/gann`      | `/api/gann-prices`       | 2 s      | 5–10 KB      | Levels + stats         |
| `/options`   | `/api/option-prices`     | 2–3 s    | 10–20 KB     | Full chain + auto trades |
| `/futures`   | `/api/future-prices`     | 2–3 s    | 5–8 KB       | Per-index summary      |
| `/paper-trades` | `/api/paper-trades-live` | 1–2 s | 1–2 KB    | Live LTP overlay on open rows only |
| `/blockers`  | `/api/blocked-list`      | 3 s      | 3–5 KB       | Current page only      |
| All pages    | `/api/recent-blocks`     | 3 s      | <1 KB        | Toaster (`_blocker_toaster.html`) |

WS QuoteFeed (`backend/kotak/quote_feed.py`):
- Subscribes to all stocks + currently watched options + currently watched futures.
- Cache is `_feed.get(exchange, token)` → `{ltp, ts, ...}`.
- LTPs in cache are sub-second fresh; REST fetchers `_ws_overlay` them onto
  cached snapshots so even cache-hit reads return fresh prices (recent fix).

---

## 4. Storage / data files

| Path                              | Format       | Used by                                 |
| --------------------------------- | ------------ | --------------------------------------- |
| `data/audit.log`                  | JSONL        | `/audit`                                |
| `data/blocked_attempts.jsonl`     | JSONL        | `/blockers`, `/api/recent-blocks`       |
| `data/paper_ledger.json`          | JSON object  | `/paper-trades`, paper book engine      |
| `data/orderlog.csv`               | CSV          | `/orderlog`                             |
| `data/login_history.json`         | JSON list    | `/history`                              |
| `data/state.json`                 | JSON object  | kill-switch + LIVE/PAPER mode           |
| `data/app.log`                    | text         | systemd capture                         |

---

## 5. Pages that exist but aren't in the tab bar

(Reachable only by direct URL or POST):

- `/STOP`, `/STOP/confirm` — kill switch
- `/refresh` — manual login refresh
- `/paper-trades.xlsx`, `/trades.xlsx` — XLSX exports
- `/orderlog.csv` — CSV export
- `/api/*` — JSON

---

## 6. Open inventory questions for UI audit

These are the gaps to inspect on screen and call out in `IMPROVEMENTS.md`:

1. **Paper-vs-real column gap** (§2.5/2.6): real ledger missing `Trigger Lvl`,
   `Trigger Spot`, `Entry Reason`, `Live LTP`, `Live Spot`.
2. **Broker passthrough pages** (§2.1): `/`, `/positions`, `/orders`,
   `/trade-book`, `/limits` show raw broker keys with no rename, no ordering,
   no number formatting, no P&L coloring.
3. **`/audit` Details column** is free-form `key=value` text — no filter by
   event-type, no per-scrip filter, only date filter exists.
4. **`/blockers`** has Source column but doesn't show *which engine* (NIFTY
   options book vs SENSEX futures book) directly — only `kind`.
5. **`/config`** matrix is paper-vs-real; live indication of which engines
   have actually fired today is not on this page.
6. **No global P&L summary on top of any page** — competitors all surface
   today's net P&L in a fixed header strip.
7. **No keyboard shortcuts**, no `?` cheatsheet — every interaction is a
   mouse click.
8. **No empty-state CTAs** — pages with 0 rows just say "No records found."
   instead of explaining why (off-hours, paper-only mode, kill-switch on, etc.).

These questions feed directly into Phase 3 (UI audit) and Phase 4
(improvements doc).

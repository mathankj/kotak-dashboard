# Kotak Neo Paper-Trading Dashboard

Web dashboard for Ganesh's Kotak Neo account (UCC `V4M8I`) — TOTP auto-login,
live Gann Square-of-9 levels for stocks and index options, paper auto-trading
on level crossings, and an audit trail of every real-broker order attempt.

Single Flask app, REST + WebSocket overlay for quotes, JSON files for
storage. No external services. Runs on the local PC today; will move to a
Contabo VPS for 24/7 operation later.

## Quick start

```powershell
pip install -r requirements.txt
copy .env.example .env       # then edit .env with credentials
python app.py
# open http://localhost:5000
```

The first request auto-logs into Kotak Neo and caches the session for the
process lifetime. The WebSocket QuoteFeed starts on the first
`/api/gann-prices` call.

Required env vars (`.env`):

```
KOTAK_CONSUMER_KEY=...
KOTAK_UCC=V4M8I
KOTAK_MOBILE=+91...
KOTAK_MPIN=...
KOTAK_TOTP_SECRET=...     # base32 secret from the Authenticator QR
```

## Layout

```
app.py                       Flask routes (~740 lines after refactor)
backend/
  utils.py                   IST timezone, now_ist()
  quotes.py                  fetch_quotes / fetch_option_quotes + WS overlay
  kotak/
    api.py                   rate limiter, circuit breaker, call_with_retry
    client.py                login(), ensure_client(), safe_call()
    instruments.py           SCRIPS, F&O universe lookup
    quote_feed.py            WebSocket subscription + tick cache
  storage/
    _safe_io.py              atomic_write_json + per-file locks
    trades.py                paper_trades.json  (read/write/next_id)
    orders.py                orders_log.json    (append/read)
    history.py               login_history.json (append/read)
  strategy/
    gann.py                  Square-of-9 levels, target-reached helper
    stocks.py                paper auto-strategy on level crossings
    options.py               paper auto-strategy on index option chains
frontend/
  templates/                 base.html, gann.html, options.html, ...
  static/                    (empty for now — JS/CSS still inline in templates)
docs/
  ARCHITECTURE.md            module map and data flow
  API.md                     HTTP endpoint reference
  STRATEGY.md                Gann math + auto-strategy rules
tests/
  test_smoke.py              import + Flask client smoke
  test_kotak_api.py          rate limiter / breaker / retry
  test_storage.py            atomic write + concurrent append safety
auto_login.py                standalone script (Windows Task Scheduler)
run_login.bat                Task Scheduler wrapper
setup_schedule.bat           one-time scheduled task setup
```

## Tests

```powershell
python -m pytest tests/ -v
```

All tests are offline — no Kotak network calls. They cover module imports,
Gann math, paper-trade ID generation, the rate limiter / circuit breaker /
retry primitives, atomic writes, and concurrent-append safety.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — modules and how data flows through them.
- [docs/API.md](docs/API.md) — HTTP routes (page views + JSON APIs).
- [docs/STRATEGY.md](docs/STRATEGY.md) — Gann Square-of-9 math and the paper auto-strategy rules.
- [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md) — original 8-phase plan; kept for history.
- [docs/SUPER_DUPER_ENGINE.md](docs/SUPER_DUPER_ENGINE.md) — design notes for the WebSocket QuoteFeed.

## Security

- `.env` is gitignored; never commit it.
- The TOTP secret is the most sensitive value — rotate at the broker if leaked.
- Paper-trading writes only ever touch the local JSON files. The only code
  path that talks to Kotak's order endpoint is `/api/place-order`, and every
  attempt (success or failure) is appended to `orders_log.json` for audit.

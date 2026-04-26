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
app.py             Flask routes (page + JSON)
backend/           All backend code (quotes, strategy, storage, Kotak SDK wrapper)
frontend/          Jinja2 templates + static assets
scripts/           Auto-login script for Windows Task Scheduler
data/              Runtime JSON files (gitignored, auto-created)
docs/              Documentation
tests/             Offline pytest suite
```

See [docs/APP_LOGIC.md](docs/APP_LOGIC.md) for the full module-by-module map.

## Tests

```powershell
python -m pytest tests/ -v
```

All tests are offline — no Kotak network calls. They cover module imports,
Gann math, paper-trade ID generation, the rate limiter / circuit breaker /
retry primitives, atomic writes, and concurrent-append safety.

## Documentation

- [docs/APP_LOGIC.md](docs/APP_LOGIC.md) — full reference: app behaviour, modules, tick lifecycle, strategy rules, HTTP routes, failure modes. Single source of truth.
- [docs/FOR_GANESH.md](docs/FOR_GANESH.md) — trader-facing rule sheet for the option strategy (entry, the 3 exit logics, settings).
- [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md) — original 8-phase plan; kept for history.
- [docs/SUPER_DUPER_ENGINE.md](docs/SUPER_DUPER_ENGINE.md) — design notes for the WebSocket QuoteFeed.

## Security

- `.env` is gitignored; never commit it.
- The TOTP secret is the most sensitive value — rotate at the broker if leaked.
- Paper-trading writes only ever touch the local JSON files. The only code
  path that talks to Kotak's order endpoint is `/api/place-order`, and every
  attempt (success or failure) is appended to `orders_log.json` for audit.

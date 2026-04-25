# Refactor Plan — Strong API, Clean Structure, Dead Code Removal

**Status:** Draft for approval
**Date:** 2026-04-25
**Author:** Claude (for matha + Ganesh)
**Scope:** Restructure repo, harden API surface, kill dead code. **Defer** all
LIVE-mode / Kotak-truth-on-exit work until Contabo VPS is provisioned.

---

## 1. Why this plan exists

After Phase 1 (WebSocket QuoteFeed) shipped, the codebase has these problems:

| # | Problem | Evidence |
|---|---------|----------|
| 1 | Single 2,900-line `app.py` mixes routes, strategy, broker calls, HTML, helpers | grep shows ~50 top-level defs in one file |
| 2 | Dead code shipped in production | `_extract_ohlc()` at app.py:257 — defined, never called |
| 3 | No retry / rate-limit / circuit-breaker around Kotak API | every `safe_call()` is a single attempt; one transient 5xx kills a quote tick |
| 4 | `paper_trades.json` rewritten on every trade — not safe under concurrent writes | `write_paper_trades()` at app.py:684 does full JSON rewrite |
| 5 | UI polls `/api/gann-prices` every 3s even though WS already pushes ticks | wasted CPU, wasted DOM diffing, masks real WS health |
| 6 | No folder structure — Ganesh can't read the code to audit it | flat directory, everything in `app.py` |
| 7 | 4 untested branches of behaviour in auto-strategy (entry/exit/squareoff/seed) live in same file as routes | hard to test in isolation |

What we are **not** fixing in this plan (deferred to "Phase VPS"):
- Kotak truth-on-exit (verifying option-leg exits via `client.positions()`)
- LIVE_MODE switch (real-money order placement)
- SSE push (drop the 3s polling — needs a stable server)
- Production deploy (systemd, log rotation, backups)

These are deferred because they only pay off on a 24/7 server. On your laptop
they add risk without reward.

---

## 2. Target folder structure

I evaluated three options. Recommendation: **Option B (modular Python, no React)**.

### Option A — `frontend/` + `backend/` + `docs/` (what you asked about)
```
backend/   — Flask app
frontend/  — React app
docs/      — markdown
```
**Verdict: NO.** Reason: introducing React doubles the moving parts (Node toolchain,
build step, CORS, separate deploy). Your UI is 5 tables and a few buttons —
React is overkill and will *slow Ganesh down* when he tries to read the code.
Server-rendered HTML + a sprinkle of JS does this job in 1/10 the lines.

### Option B — Modular Python, server-rendered HTML, `docs/` (RECOMMENDED)
```
kotak-autologin/
├── app.py                    # tiny entrypoint: create_app() + main
├── backend/
│   ├── __init__.py
│   ├── config.py             # env, constants (SCRIPS, GANN, expiry overrides)
│   ├── kotak/
│   │   ├── __init__.py
│   │   ├── client.py         # ensure_client(), login flow
│   │   ├── api.py            # strong wrapper: rate-limit + retry + breaker
│   │   ├── quote_feed.py     # MOVED from root
│   │   └── instruments.py    # scrip master, expiry parsing, option chain
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── gann.py           # gann_levels(), nearest_gann_level()
│   │   ├── stocks.py         # auto_strategy_tick + helpers
│   │   └── options.py        # option_auto_strategy_tick + helpers
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── trades.py         # paper_trades CRUD (later: SQLite)
│   │   ├── orders.py         # orders_log
│   │   └── history.py        # login_history
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── pages.py          # /, /positions, /orders, /trades, /limits, /history, /gann, /options
│   │   ├── api_quotes.py     # /api/gann-prices, /api/option-prices, /api/feed-status
│   │   ├── api_trades.py     # /api/paper-open, /api/paper-close, /api/paper-trades
│   │   └── api_orders.py     # /api/place-order, /api/margin-summary, /orderlog*
│   └── templates.py          # render() + the inline HTML PAGE (or split to .html files)
├── frontend/                 # static assets only (no build step)
│   ├── static/
│   │   ├── app.js            # vanilla JS poller (later: SSE client)
│   │   └── app.css
│   └── templates/            # if we move HTML out of templates.py
│       └── page.html
├── docs/
│   ├── README.md             # what this app does (for Ganesh)
│   ├── ARCHITECTURE.md       # diagram + module responsibilities
│   ├── API.md                # every route, params, response shape
│   ├── STRATEGY.md           # Gann rules, entry/exit logic
│   ├── DEPLOY.md             # local + future VPS setup
│   └── REFACTOR_PLAN.md      # this file
├── tests/
│   ├── test_gann.py
│   ├── test_strategy_stocks.py
│   ├── test_strategy_options.py
│   ├── test_kotak_api_wrapper.py
│   └── test_routes_smoke.py
├── requirements.txt
├── auto_login.py             # leave as-is (standalone smoke script)
└── .env.example
```

**Why this layout:**
- Each file has one job → Ganesh can audit it in 5 min instead of a day.
- Strategy logic is *importable in isolation* → tests run in milliseconds, no Flask, no Kotak.
- `kotak/api.py` is the **single chokepoint** for every broker call → rate limit, retry, breaker live in one place.
- `frontend/` exists but contains zero JS frameworks. Just CSS + 1 vanilla JS file. No npm.
- `docs/` is real markdown that Ganesh can read directly on GitHub.

### Option C — Keep flat, just split into a few files
**Verdict: NO.** It's the path of least change but it leaves the audit problem
unsolved. Ganesh asked for structure precisely because he can't read 2,900 lines.

---

## 3. The strong-API layer (`backend/kotak/api.py`)

Single wrapper that **every** Kotak call goes through. No more raw `client.X()`
calls scattered across routes.

```python
# backend/kotak/api.py — sketch only, full code in Phase 2
class KotakAPI:
    """Hardened wrapper around neo_api_client.NeoAPI.

    Features:
      - Rate limit: token bucket, 5 req/s (Kotak's documented cap)
      - Retry: exponential backoff on 5xx and network errors, 3 attempts max
      - Circuit breaker: 5 consecutive failures → open for 30s → fail-fast
      - Single-flight: if 2 callers ask for the same quote in same 100ms, only 1 hits Kotak
      - Metrics: per-method count, p50/p95 latency, error count
      - Structured logging: every call logs {method, args_hash, latency_ms, status}
    """
    def __init__(self, client_provider): ...
    def quotes(self, instrument_tokens, **kw): ...
    def positions(self): ...
    def holdings(self): ...
    def margin(self): ...
    def order_report(self): ...
    def place_order(self, **kw): ...   # gated by LIVE_MODE flag (off until VPS)
    def stats(self) -> dict: ...       # for /api/health
```

**What this fixes:**
- Today: a transient 502 from Kotak nukes the page. Tomorrow: 1 retry, user sees nothing.
- Today: no idea how often we call Kotak. Tomorrow: `/api/health` shows req/s, error rate.
- Today: 3 page loads in 1s = 3 quote calls. Tomorrow: 1 call, 3 callers share the result.

**Cost to your Kotak quota:** *goes down*, not up. Single-flight + WS-overlay mean
fewer REST calls per UI interaction.

---

## 4. Dead code removal (audit results)

Confirmed dead via grep. Safe to delete in Phase 1.

| Symbol | Location | Status |
|--------|----------|--------|
| `_extract_ohlc()` | app.py:257 | DELETED in Phase 1 — confirmed dead |
| `option_demo_seed_api` (`/api/option-demo-seed`) | app.py:2456 | DELETED in Phase 1 — no JS callers |
| `_quote_cache` + `QUOTE_TTL` | app.py:184-185 | **NOT dead** — KEEP. Re-grep showed `fetch_quotes` reads/writes both, and `fetch_option_quotes` reads `QUOTE_TTL` for its own `_option_quote_cache`. Original plan was wrong. |
| `quote_feed._reconnects` counter | quote_feed.py:64 | Never incremented. Cosmetic, deferred. |
| `auto_login.py` print-only smoke script | root | KEEP — standalone debugging tool |
| `render.yaml`, `fly.toml`, `Dockerfile`, `.dockerignore` | root | DELETED in Phase 1 — going Contabo |

**To verify before deleting** (Phase 1, Step 1):
```bash
# For each candidate, prove zero usage:
rg -n "_extract_ohlc" .          # expect: 1 hit (the def)
rg -n "_quote_cache|QUOTE_TTL" . # expect: only the def + 0 reads
```

Probably-dead, **need user confirmation before delete**:
- `/api/option-demo-seed` route (app.py:2456) — was for seeding paper trades during demo. Still needed?
- `paper_trades.xlsx` route (app.py:2633) — does Ganesh actually export?
- `render.yaml` + `fly.toml` + `Dockerfile` — we're going Contabo, not Render/Fly. Keep for reference or delete?

I will **not** delete these without your explicit yes.

---

## 5. Phased execution

Each phase ships independently. App keeps working after every phase.
**Stop anywhere — no phase breaks the previous one.**

### Phase 0 — Pre-flight (15 min, zero risk)
- [ ] Commit Phase 1 WS feed work currently sitting uncommitted (`quote_feed.py`,
      `app.py` overlay changes, `SUPER_DUPER_ENGINE.md`)
- [ ] Create `docs/` folder; move `SUPER_DUPER_ENGINE.md` and this file into it
- [ ] Create `tests/` folder with one smoke test that imports `app.py` (proves
      we can move things without breaking imports)

**Acceptance:** `python -c "import app"` works; `git status` clean.

### Phase 1 — Dead code removal (30 min)
- [ ] Delete `_extract_ohlc()` (app.py:257)
- [ ] Delete `_quote_cache` + `QUOTE_TTL` if grep confirms no readers
- [ ] Ask user about `/api/option-demo-seed`, xlsx export, deploy configs
- [ ] Run app locally, click every page, confirm nothing 500s

**Acceptance:** all pages render; line count of app.py drops by ~50.

### Phase 2 — Extract `backend/kotak/` (1-2 hours)
- [ ] Create `backend/kotak/client.py` — move `ensure_client`, `login`, `safe_call`
- [ ] Create `backend/kotak/instruments.py` — move SCRIPS, expiry overrides,
      `_fetch_index_fo_universe`, `build_option_chain`, `build_all_option_tokens`,
      `find_scrip`, `_parse_item_*`
- [ ] Move `quote_feed.py` → `backend/kotak/quote_feed.py`
- [ ] Update imports in `app.py`
- [ ] Run app, verify pages still work

**Acceptance:** app.py shrinks by ~600 lines; all routes still respond.

### Phase 3 — Strong API wrapper `backend/kotak/api.py` (2-3 hours)
- [ ] Build `KotakAPI` class with: rate limit, retry, breaker, single-flight, stats
- [ ] Replace direct `client.quotes(...)` / `.positions()` / etc. calls in
      `fetch_quotes`, `fetch_option_quotes`, route handlers
- [ ] Add `/api/health` route exposing `KotakAPI.stats()`
- [ ] Write unit tests with a fake client (no Kotak hits)

**Acceptance:**
- Killing the network for 5s doesn't crash any page (breaker kicks in).
- `/api/health` shows non-zero call count after browsing.
- Test suite runs in <1s.

### Phase 4 — Extract `backend/strategy/` (1-2 hours)
- [ ] Move `gann_levels`, `nearest_gann_level`, `compute_target_level_reached`
      into `backend/strategy/gann.py`
- [ ] Move stock auto-strategy (`auto_strategy_tick`, `_auto_*` helpers) into
      `backend/strategy/stocks.py`
- [ ] Move option auto-strategy into `backend/strategy/options.py`
- [ ] Write tests with hand-crafted price ticks (no live data needed)

**Acceptance:** strategy tests run on a plane (no internet). app.py shrinks
another ~400 lines.

### Phase 5 — Extract `backend/storage/` (1 hour)
- [ ] Move paper-trades JSON helpers into `backend/storage/trades.py`
- [ ] Move orders log into `backend/storage/orders.py`
- [ ] Move login history into `backend/storage/history.py`
- [ ] Wrap each writer in a file-lock so two simultaneous writes can't corrupt the file

**Acceptance:** triggering 5 paper-opens in parallel doesn't lose any.

### Phase 6 — Extract `backend/routes/` (1-2 hours)
- [ ] Split routes into 4 blueprint files: `pages.py`, `api_quotes.py`,
      `api_trades.py`, `api_orders.py`
- [ ] `app.py` becomes a 30-line `create_app()` factory + main entrypoint

**Acceptance:** app.py < 100 lines; every route still works.

### Phase 7 — Move HTML to `frontend/` (1 hour, optional)
- [ ] Move inline PAGE template to `frontend/templates/page.html`
- [ ] Move inline JS to `frontend/static/app.js`
- [ ] Move inline CSS to `frontend/static/app.css`

**Acceptance:** UI looks identical; view-source shows separate files.

### Phase 8 — Docs (1 hour, can run anytime)
- [ ] `docs/README.md` — what this app does, how to run it (Ganesh-readable)
- [ ] `docs/ARCHITECTURE.md` — module map + data flow diagram
- [ ] `docs/API.md` — every route documented with curl examples
- [ ] `docs/STRATEGY.md` — Gann rules in plain English
- [ ] `docs/DEPLOY.md` — local Windows + (placeholder) Contabo VPS

**Acceptance:** Ganesh can read `docs/` and answer "what does this app do?"
without opening Python.

---

## 6. Deferred to "Phase VPS" (after Contabo arrives)

These are **intentionally not in this plan**. Doing them on your laptop is
wasted effort.

- **Kotak truth-on-exit** — fetch `client.positions()` after every paper exit,
  reconcile with our state. Needs a 24/7 server to be meaningful.
- **LIVE_MODE flag** — flip paper → real. Single env var, but only safe to
  test on a server you control.
- **SSE push** — replace 3s polling with server-sent events. Web-research
  confirmed this is the right pattern, but laptop-Wi-Fi keeps disconnecting
  long-lived connections.
- **SQLite migration** — `paper_trades.json` → `trades.db`. Worth it on a
  multi-user server. Single-user laptop, file-lock from Phase 5 is enough.
- **systemd / log rotation / daily backups / Cloudflare tunnel** — deploy
  concerns, not laptop concerns.

---

## 7. What I need from you before starting

1. **Approve folder structure (Option B)** — yes / change to A or C?
2. **Confirm dead-code deletes** — OK to delete `/api/option-demo-seed`?
   `paper_trades.xlsx` route? `render.yaml` / `fly.toml` / `Dockerfile`?
3. **Approve phase order** — start at Phase 0 (commit + docs folder) and stop
   between any phase for review?
4. **Tests** — should I write `pytest` tests as I go (recommended), or skip
   tests and rely on manual click-through?

Once you answer these, I'll start at Phase 0 and check in with you between
each phase before moving on.

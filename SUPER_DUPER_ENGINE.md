# Super Duper Engine — Implementation Plan

8-phase migration from polling Flask app to WebSocket-driven, decoupled, real-time
auto-trading engine. Each phase ships independently and leaves the system working.

## Architecture (target)

```
Kotak Neo  ──WS──▶ QuoteFeed ──▶ in-memory cache ──▶ StrategyEngine
                                       │                    │
                                       ▼                    ▼
                                   Web UI (SSE)        OrderQueue ──REST──▶ Kotak Neo
                                                            │
                                                            ▼
                                                       RiskManager
                                                            │
                                                            ▼
                                                        SQLite
```

## Phase 1 — WebSocket QuoteFeed (current)

**Goal:** Replace 3-second polling with real-time WS ticks. Zero behavior change
visible to user. REST kept as fallback so a WS disconnect can't break the demo.

**Files**
- Create: `quote_feed.py` — QuoteFeed class
- Modify: `app.py` — `fetch_quotes`, `fetch_option_quotes` read WS cache first;
  add `/api/feed-status`; start feed at module boot

**Subscriptions on boot**
- Index spots: NIFTY 50, BANKNIFTY, SENSEX (isIndex=True)
- Stocks: 6 from SCRIPS (already configured)
- Options: per index, ATM ± 5 strikes × {CE, PE} = 33 contracts × 3 indices = 99
- Re-subscribe option tokens whenever ATM shifts (spot moves > strike_step/2)

**QuoteFeed contract**
```python
class QuoteFeed:
    def start(self): ...                # spawns background thread
    def stop(self): ...
    def get(self, key) -> dict | None:  # {ltp, ts, ohlc?}; None if no tick yet
    def status(self) -> dict:           # {connected, subs, last_tick_ts, reconnects, errors}
    def update_option_subs(self, atm_by_index: dict): ...
```

**Cache freshness rule (in fetchers)**
- WS tick age ≤ 5 s → use WS LTP
- Else → REST fallback (existing path)
- OHLC always from REST (WS gives ticks only)

**Acceptance**
- `/api/feed-status` shows `connected:true` within 10 s of boot
- `/api/gann-prices` returns LTPs with `ws_age` field; for stocks during market
  hours `ws_age < 3` for >90% of calls
- Existing UI and auto-strategy keep working unchanged
- WS disconnect: status flips to `connected:false`, fetchers transparently
  fall back to REST, no errors surfaced to UI

## Phase 2 — Decoupled StrategyEngine
Move `auto_strategy_tick` and `option_auto_strategy_tick` out of the request
path. Background scheduler fires on every WS tick (not on UI poll).

## Phase 3 — OrderQueue + RiskManager
Single-consumer queue, 5 orders/sec cap, per-symbol exposure cap, daily loss
cap, watchdog that auto-pauses on N consecutive failures.

## Phase 4 — SQLite for paper_trades
Migrate `paper_trades.json` → `trades.db`. Atomic writes, no race conditions
under concurrent strategy + UI access.

## Phase 5 — Slim UI + SSE push
KPI bar at top (P&L, open positions, feed status). Full chain/details
collapsible. Server-Sent Events push updates instead of polling.

## Phase 6 — LIVE_MODE flag
Single env var flips paper → real. Order shape already matches Kotak
`place_order` API; only the call target changes.

## Phase 7 — Observability
Structured logs (JSON), per-trade audit trail with WS-vs-REST source tag,
latency histograms.

## Phase 8 — Production deploy
Contabo VPS, systemd unit, log rotation, daily backup of SQLite,
auto-restart on OOM, Cloudflare tunnel or domain + reverse proxy.

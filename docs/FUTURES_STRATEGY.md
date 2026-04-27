# Index Futures Strategy

Approved 2026-04-27 with Ganesh. Mirrors the options strategy but trades
the index FUTURES contract directly (no CE/PE, no strike). Runs alongside
the options strategy — they share the same Gann signals from the spot
quote but place independent orders.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **Long + Short** — bullish signal → BUY future; bearish → SELL future (short) | Mirrors options' two-sided behavior. Square-off at 15:15 closes whatever's open. |
| 2 | **Round LTP to 50 / 100 / 100** for NIFTY / BANKNIFTY / SENSEX | Matches Ganesh's "strike price" intuition; same step as option strikes. |
| 3 | **BUY rounds DOWN, SELL rounds UP** | Always tries to get a better fill price. Limit may not fill if price runs away — accepted trade-off. |
| 4 | **Exits use rounded limit** (same logic as entry) | Consistent. Buyer-to-close (closing short) rounds DOWN; seller-to-close (closing long) rounds UP. |
| 5 | **Alongside options** — separate strategy, separate config | Both can fire on same signal. Per-index toggle. |

## Contract specs (from live SDK 2026-04-27)

| Index | Trading symbol pattern | Lot size | Segment | InstType |
|---|---|---|---|---|
| NIFTY | `NIFTY{YY}{MMM}FUT` | **65** | nse_fo | FUTIDX |
| BANKNIFTY | `BANKNIFTY{YY}{MMM}FUT` | **30** | nse_fo | FUTIDX |
| SENSEX | `SENSEX{YY}{MMM}FUT` | **20** | bse_fo | IF |

Lot sizes differ from option lot sizes — fetched from SDK's `lLotSize`
field at runtime, never hardcoded.

## Signals (identical to options)

| Path | Bullish (LONG) | Bearish (SHORT) |
|---|---|---|
| A — Market-Open | First tick: spot already > BUY → buy future | First tick: spot already < SELL → short future |
| B — Crossing | spot crosses BUY upward → buy future | spot crosses SELL downward → short future |

## Exits (3 variants — pick one in /config, all live in code)

| Variant | LONG exit | SHORT exit |
|---|---|---|
| A — Fixed ₹X drop | future LTP <= entry − X | future LTP >= entry + X |
| B — % drop | future LTP <= entry × (1 − X%) | future LTP >= entry × (1 + X%) |
| C — Opposite Gann (default) | spot < SELL level | spot > BUY level |

## Profit target

LONG: spot >= configured CE-side level (T1/T2/T3/BUY_WA)
SHORT: spot <= configured PE-side level (S1/S2/S3/SELL_WA)

(Same selectors as options' target — kept on its own keys under `futures.target`.)

## Time square-off

15:15 IST closes any open futures position. Same window helpers as options
(reads `timings.market_start` / `timings.square_off` from config).

## Config schema (additions to config.yaml)

```yaml
futures:
  enabled:
    NIFTY:     false        # Master switch per index — start disabled
    BANKNIFTY: false
    SENSEX:    false

  entry:
    market_open_path: true
    crossing_path:    true

  stoploss:
    active: C               # A | B | C
    variant_a_drop_rs:  20  # Bigger than options because future moves in points
    variant_b_drop_pct: 1   # 1% of futures LTP

  target:
    long_level:  T1         # T1 | T2 | T3 | BUY_WA
    short_level: S1         # S1 | S2 | S3 | SELL_WA

  lots:
    NIFTY:     1            # Multiplier on broker lot (NIFTY 65 × 1 = 65 qty)
    BANKNIFTY: 1
    SENSEX:    1

  per_day_cap:
    NIFTY:     null         # null = unlimited
    BANKNIFTY: null
    SENSEX:    null

  round_step:
    NIFTY:     50
    BANKNIFTY: 100
    SENSEX:    100
```

## Margin warning to surface in UI

Futures need ~12-15% notional margin (~₹2L per NIFTY/BANKNIFTY lot). All
three indices long+short = ~₹6L margin tied up. The /config Futures
section displays this estimate and starts with all three indices DISABLED
so Ganesh enables them one at a time after confirming margin headroom.

## Files touched

- `config.yaml` — add `futures:` block (defaults disabled)
- `backend/config_loader.py` — DEFAULTS + validation for futures section
- `backend/kotak/quote_feed.py` — add `set_future_subs` slot
- `backend/kotak/instruments.py` — `_fetch_nearest_index_future(idx)`
- `backend/quotes.py` — `fetch_future_quotes()` (REST + WS overlay)
- `backend/strategy/futures.py` — NEW: `future_auto_strategy_tick`
- `app.py` — call futures tick alongside options in daemon; add UI tab
- `frontend/templates/config.html` — Futures section (per-index controls)

## Out of scope (deferred)

- `/futures` view tab — trades show in existing /trades ledger
- Automatic monthly contract roll on expiry day — runtime nearest-expiry
  lookup handles new expiries the next trading session
- Per-trade SL price (kept logic-driven via stoploss variant only)

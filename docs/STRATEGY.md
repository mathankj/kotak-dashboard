# Gann Square-of-9 strategy

Two implementations: one for stocks, one for index options. Both are paper
only — no real Kotak orders are placed, ever.

## Levels

`backend/strategy/gann.py` computes 10 price levels from the day's open by
stepping through Gann's 22.5° squares (0.0625 in sqrt-space):

```
S3   S2   S1   SELL_WA   SELL    [open]    BUY   BUY_WA   T1   T2   T3
n=-6 -5    -4    -3       -2                +2    +3      +4   +5   +6
```

For each step `n`, the level price is `(sqrt(open) + n * 0.0625) ** 2`.
Sell levels live below the open; buy levels above. The two `_WA` ("warning
area") levels at ±3 are visual cues only — they don't trigger entries.

## Stock auto-strategy (`backend/strategy/stocks.py`)

| Trigger             | Action                                |
|---------------------|---------------------------------------|
| LTP crosses BUY ↑   | open paper BUY of `AUTO_QTY` shares   |
| LTP crosses SELL ↓  | open paper SELL of `AUTO_QTY` shares  |
| BUY trade: LTP ≥ T1 | exit, reason `TARGET_T1`              |
| BUY trade: LTP < SELL | exit, reason `SL_SELL_LVL` (stop)   |
| SELL trade: LTP ≤ S1 | exit, reason `TARGET_S1`             |
| SELL trade: LTP > BUY | exit, reason `SL_BUY_LVL` (stop)    |
| 15:15 IST and later | force-close all OPEN, reason `AUTO_SQUARE_OFF` |

Constraints:
- Active 09:15 – 15:15 IST, weekdays only.
- Max `AUTO_MAX_TRADES_PER_SCRIP` (default 2) entries per symbol per day.
- One open trade per symbol at a time.

A "crossing" requires both sides: the previous tick must be on one side of
the level and the current tick on the other. The previous LTP is held in
`_auto_state["last_ltp"]` keyed by symbol — so the very first tick after
process start never triggers an entry, by design.

## Option auto-strategy (`backend/strategy/options.py`)

Same crossing concept, but driven by the **index spot** with paper trades
on the **ATM CE/PE option** rather than the index itself:

| Trigger                  | Action                                  |
|--------------------------|-----------------------------------------|
| Spot crosses BUY ↑       | paper BUY 1 lot ATM CE                  |
| Spot crosses SELL ↓      | paper BUY 1 lot ATM PE                  |
| CE trade: spot ≥ T1      | exit, reason `TARGET_T1`                |
| CE trade: spot < SELL    | exit, reason `SL_SELL_LVL`              |
| PE trade: spot ≤ S1      | exit, reason `TARGET_S1`                |
| PE trade: spot > BUY     | exit, reason `SL_BUY_LVL`               |
| 15:15 IST and later      | force-close all OPEN options            |

Entry/exit prices are option LTPs at the moment of the trigger. Because the
option module needs the **stock-side** Gann levels (which live on the
underlying spot symbol), the route layer passes the latest `gann_quotes`
into `option_auto_strategy_tick(option_data, option_index_meta, gann_quotes)`
explicitly. That signature was chosen to break what would otherwise be a
circular import (`strategy/options.py` → `app.fetch_quotes` →
`backend.quotes` → `strategy/options.py`).

## What "level reached" means in the UI

After every quote refresh, `update_open_trades_mfe` walks every OPEN paper
trade and updates two fields:

- `max_min_target_price` — the most-favourable price seen since entry
  (max for BUY, min for SELL).
- `target_level_reached` — the deepest Gann level the trade touched in
  its favour, computed by `compute_target_level_reached()` against the
  ladder `[BUY, BUY_WA, T1, T2, T3]` (or the SELL mirror). Returns
  `"Beyond T3"` if the trade ran further than the highest level.

That's what makes the trade table show "trade got to T2 before reversing"
even after the trade is closed.

## Tuning knobs

All in `backend/strategy/stocks.py`:

```python
AUTO_STRATEGY_ENABLED      = True
AUTO_HOURS_START           = (9, 15)
AUTO_HOURS_END             = (15, 15)
AUTO_MAX_TRADES_PER_SCRIP  = 2
AUTO_QTY                   = 1
```

And `AUTO_OPTION_STRATEGY_ENABLED` in `backend/strategy/options.py` for
the option side.

## Why this is paper-only

Real-broker integration is wired up at `/api/place-order` and exercised
manually by the user. The auto-strategy intentionally never touches that
path — it only writes to `paper_trades.json`. Going live will be a
deliberate flip of an `LIVE_MODE` flag plus a Kotak `place_order` call from
inside `_auto_open` / `_auto_close`, which is on the Contabo VPS roadmap.

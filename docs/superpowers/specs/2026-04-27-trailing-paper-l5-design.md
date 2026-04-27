# Trailing SL + Paper-Shadow Log + Ladder-to-±5 — Design

**Date:** 2026-04-27
**Status:** Draft, awaiting user approval
**Owner:** kotak-autologin (Ganesh)

## Problem statement

Three independent product asks from Ganesh, to be shipped in order of
blast radius (smallest first):

1. **Extend the Gann ladder to ±5.** Today the ladder is S3..T3. Add
   S5..T5 — two extra rungs on each side — so larger moves still have
   a visible rung above/below the LTP.
2. **Parallel paper-trade shadow log.** Run a *second* virtual book
   that takes every signal the live strategy takes, records buy/sell
   to a separate ledger, exposes its own page+Excel export, but never
   sends real orders. Used for comparing strategy outcomes against
   live while we tune.
3. **Trailing stop loss along the Gann ladder (variant D).** Today
   SL is one of three flavours (A=fixed-rs, B=fixed-pct,
   C=opposite-Gann-level). Add D: SL trails *one rung behind* the
   LTP's current rung as price walks favourably along the ladder.

## Phasing

| Phase | Feature | Why this order |
|-------|---------|----------------|
| 1 | Ladder ±5 | Pure widening of an existing data structure. Math + UI columns. No strategy semantics change. Lowest risk. |
| 2 | Paper-shadow log | Pure additive — new ledger file, new page, new shadow-write call. Zero edits to live order flow. |
| 3 | Trailing SL variant D | Touches live exit logic in two strategies (options + futures). Done last so the paper log can verify its behaviour against live A/B/C exits during rollout. |

User approved phasing in earlier session: *"P1 (extend ladder) → P2
(paper trade) → P3 (trailing stop)"*.

---

## Phase 1 — Ladder ±5

### What changes

`backend/strategy/gann.py` currently exposes:
```
SELL_LEVELS = ["S3", "S2", "S1", "SELL_WA", "SELL"]
BUY_LEVELS  = ["BUY", "BUY_WA", "T1", "T2", "T3"]
```
with sqrt-stepping `n ∈ [-6..-2] ∪ [+2..+6]`.

Extend to:
```
SELL_LEVELS = ["S5", "S4", "S3", "S2", "S1", "SELL_WA", "SELL"]
BUY_LEVELS  = ["BUY", "BUY_WA", "T1", "T2", "T3", "T4", "T5"]
```
with `n ∈ [-8..-2] ∪ [+2..+8]` (S5=-8, S4=-7, S3=-6 … T3=+6, T4=+7, T5=+8).
Step size and formula are unchanged.

### Touchpoints (verified)

- `backend/strategy/gann.py`
  - `SELL_LEVELS` (line 14), `BUY_LEVELS` (line 15) — extend to 7 entries each.
  - `LEVEL_COLORS` (lines 18–23) — add S5, S4, T4, T5 colour entries.
  - `BUY_LEVEL_ORDER` (line 26), `SELL_LEVEL_ORDER` (line 27) — extend.
  - `gann_levels()` (lines 30–45) — formula loop iterates the new lists.
  - `compute_target_level_reached()` (lines 73–98) — **the hard-coded
    "Beyond T3" / "Beyond S3" string clamps at lines 85–87 and 95–97
    must change to "Beyond T5" / "Beyond S5"** (and use `buy.get("T5")`,
    `sell.get("S5")` respectively). Otherwise prices above T3 but
    below T5 will be mis-labelled "Beyond T3".
  - Module docstring (lines 4–7) — update enumeration to cover S5..T5.
- `backend/config_loader.py`
  - `VALID_CE_TARGETS` (line 120) — add `"T4"`, `"T5"`.
  - `VALID_PE_TARGETS` (line 121) — add `"S4"`, `"S5"`.
  - `VALID_BUY_LEVELS` and `VALID_SELL_LEVELS` (lines 118–119) **stay
    unchanged** — the entry-side and SL-side pickers only ever offer
    BUY/BUY_WA and SELL/SELL_WA; only the *target* picks widen.
- `tests/` — verified to currently contain only
  `test_kotak_api.py`, `test_smoke.py`, `test_storage.py`. Phase 1
  may add `tests/test_gann_levels.py` (no collision).
- `frontend/templates/gann.html`
  - Add 4 columns to the table header and body rows (S5, S4 left;
    T4, T5 right).
  - Add 4 new CSS rules (`td.lvl-S5`, `td.lvl-S4`, `td.lvl-T4`,
    `td.lvl-T5`) to the heatmap palette block (lines 46–63), including
    the "crossed" font-weight enumeration at lines 58–63.
  - Update the JS arrays `SELL_LVLS` / `BUY_LVLS` (lines 267–268) and
    `LTP_LEVEL_CLASSES` (lines 277–280) to include the four new rungs;
    otherwise `paintCell()` is never called for the new columns.
- `frontend/templates/config.html`
  - Target dropdowns at line 319 (CE side, currently `['T1','T2','T3','BUY_WA']`)
    and line 329 (PE side, currently `['S1','S2','S3','SELL_WA']`) —
    extend literal lists to include T4/T5 and S4/S5.
  - **Do not widen** the entry / variant-A/B/C / market-open
    dropdowns — those legitimately stay {BUY,BUY_WA} / {SELL,SELL_WA}.
- `config.yaml` — no schema change. Existing picks remain valid; new
  T4/T5/S4/S5 picks become available for `target.ce_level` /
  `target.pe_level`.

### Risk

- **Numerical drift on far rungs:** `(sqrt(p) + 8*0.0625)^2` is still
  well-behaved for index prices (10k–80k); float precision is fine.
- **UI column overflow on small screens:** the table already has CSS
  width controls. May need a horizontal-scroll wrapper if BANKNIFTY
  rows get too wide. Decide visually after first render.
- **Existing trades' `target_level_reached` field:** rows written
  before this phase only know about T3/S3 caps. They're historical —
  no rewrite needed; new rows record T4/T5 organically.
- **Config files in the wild that picked T3 keep working** — the
  enums only widened, never shrunk.

### Done when

- Gann page shows 14 level columns (was 10), heatmap paints all of them.
- `/config` target-level dropdowns include T4/T5 and S4/S5 picks; the
  validator accepts those picks.
- Existing strategy behaviour with BUY/T1/etc. picks unchanged — verified
  by saving and loading the existing config.yaml unchanged and
  observing identical entries on a sample tick.
- `compute_target_level_reached()` correctly emits "T4", "T5", "Beyond T5"
  on synthetic LTPs walked above T3 (added test in `tests/test_smoke.py`
  or a new `tests/test_gann_levels.py` if one doesn't exist).

---

## Phase 2 — Paper-shadow log

### What it is

A **fully independent parallel book** that runs the same strategy
logic against the same quotes, but never sends real orders. It has
its own OPEN/CLOSED state machine — paper entries, paper exits, and
paper square-offs all happen on schedule **regardless of what the
live ledger did**.

Concretely (user's example, 2026-04-27): if the live BUY is blocked
by Kotak (zero margin / kill switch / position-verify mismatch), the
paper book *still* records the BUY at the signal LTP. Later when the
exit signal fires, the live side has nothing to sell (no real
position) — that's fine, the live exit attempt is correctly
short-circuited — but the paper book *still* records the SELL,
closing the paper position. End-of-day square-off applies to paper
OPENs the same way it applies to live OPENs.

So paper P&L stands on its own and answers "what would the strategy
have made if we had unlimited margin and zero blockers?" — which is
exactly what Ganesh wants for tuning.

User constraint (verbatim): *"that paper trade also same logic we set
now all oig and all set paper trade also"*. **Confirmed
interpretation:** the shadow runs the same gates and the same
decision logic as live. The /blockers page stays exclusively for
live (real-money) trades — paper-shadow records never show on that
tab. (Confirmed by user 2026-04-27: *"no paper trade not comes under
blocker, blocker tab is only for money trad"*.)

### Why

- Compare strategy outcomes vs. live in the same session.
- Continue testing while toggling LIVE_MODE on real money.
- Exportable Excel for Ganesh to review off-platform.

### Architecture

Each strategy tick now drives **two independent state machines** in
sequence, both reading the same in-memory signal:

```
strategy tick
  │
  ├── compute signal once: (now, in_hours, square_off?,
  │                         per-idx: spot, levels, prev_spot,
  │                         entry_side?, exit_reason?)
  │
  ├── apply to LIVE ledger
  │     read trades.json → loop OPEN-live → exits / square-off
  │     for indices with no OPEN-live → entry check
  │     calls place_order_safe (may succeed, paper-via-LIVE_MODE,
  │                              or fail/block)
  │     writes trades.json
  │
  └── apply to PAPER ledger      (NEW)
        read paper_ledger.json → loop OPEN-paper → exits / square-off
        for indices with no OPEN-paper → entry check (same gates)
        NEVER calls place_order_safe — fills at current LTP
        writes paper_ledger.json
```

The paper branch is **not** "shadow the live row" — it's a second
independent run of the same decision tree against its own ledger.
If the live row is blocked, the paper row still opens. If the paper
row exits while live has nothing to exit, live silently does
nothing.

### Gating

Both branches share the same upstream gates:
- `config_loader.options_enabled()` / `futures_enabled()` (apply_to)
- `_auto_in_hours(now)` (trading window)
- `_auto_at_or_after_squareoff(now)` (square-off cutoff)
- `_can_open_more(idx_name, paper_counts)` (per-day cap, computed
  separately per ledger — paper has its own count)

Kill-switch (the safety stop) gates the **live** branch only — that
switch exists to halt real-money risk. Paper is unaffected by the
kill switch (per user: "we wnat square all, all are wants happes").
This is a behavioural difference from the prior draft and a notable
risk if Ganesh ever flips the kill switch expecting paper to also
freeze; spec must surface in the UI.

### Reusable decision computation

To avoid duplicating the entry-side / exit-reason logic between the
two branches, refactor each strategy's tick to extract:
- `_compute_entry_signal(idx_name, spot, prev_spot, levels, cfg) -> "BUY"|"SELL"|None`
- `_compute_exit_reason(open_row, ltp, spot, ...) -> reason|None`
  (already exists for both — `_check_exit_reason` and
  `_check_futures_exit_reason`).

The live branch and the paper branch both call these. Live then
calls `place_order_safe` + `_execute_*`; paper calls a new
`_paper_execute_entry` / `_paper_execute_exit` that fills at LTP
and writes only to the paper ledger.

### Components

- `backend/storage/paper_ledger.py` — new file. Same shape as
  `trades.py`: `read_paper_ledger()`, `write_paper_ledger()`,
  `next_paper_id()`. Stored at `data/paper_ledger.json`. **Must
  `os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)` at
  import** (trades.py only does this inside the migration helper
  which won't run for paper_ledger.py). No legacy migration — the
  trades.py legacy file is `data/paper_trades.json`, a different
  filename, so no collision.
- `backend/strategy/paper_book.py` — new file. The paper-side
  state machine. Public surface:
  - `paper_options_tick(option_data, option_index_meta, gann_quotes)`
    — paper analogue of `option_auto_strategy_tick`. No `client`
    param (never sends orders). Reads `paper_ledger.json`, applies
    entries/exits/square-off, writes back.
  - `paper_futures_tick(future_data, gann_quotes)` — paper
    analogue of `future_auto_strategy_tick`.
  - Internally calls the **shared** `_compute_entry_signal` /
    `_check_exit_reason` / `_check_futures_exit_reason` from
    options.py / futures.py — these are the source of truth for
    "what would the strategy do." Paper never reimplements them.
  - Internally calls `_paper_execute_entry(row)` and
    `_paper_execute_exit(open_row, ltp, reason)` — both write to
    `paper_ledger.json` only; pnl math reuses `_auto_close()` from
    common.py (paper rows have the same field shape so it works
    unchanged).
- `backend/storage/paper_ledger.py` — new file (schema same as
  trades.py, see "Components → storage" above).
- Refactor in `backend/strategy/options.py`:
  - Extract the entry-side detection block (currently inline in
    `option_auto_strategy_tick`) into
    `_compute_entry_signal(idx_name, spot, prev_spot, atm, ...,
    cfg) → ("CE"|"PE"|None, "market_open"|"crossing"|None)`.
    `_check_exit_reason` already exists.
  - Live tick body shrinks to: build counts/state → loop indices →
    call `_compute_entry_signal` → `_execute_entry` (existing) /
    `_check_exit_reason` → `_execute_exit` (existing).
  - Paper tick reuses `_compute_entry_signal` and
    `_check_exit_reason` directly.
- Refactor in `backend/strategy/futures.py`: same extraction —
  `_compute_futures_entry_signal`, reuse existing
  `_check_futures_exit_reason`.
- Wiring in `app.py`:
  - The autonomous strategy ticker `_strategy_ticker_loop` (3s
    daemon, started by `_start_strategy_ticker_once()`) currently
    calls live ticks only. Add paper tick calls right after each
    live tick — same fetched quote data is reused, so no extra
    network I/O.
- `app.py` — new route `/paper-trades` rendering
  `frontend/templates/paper_trades.html` (clone of
  `trade_ledger.html`, reads paper ledger). New `/paper-trades.xlsx`
  export route (clone of trade_ledger Excel export).
- `app.py` — extend the `TABS` list (line 100) to include the new
  Paper Log entry. (`base.html` itself does not enumerate tabs;
  it iterates the `tabs` template variable.)

### Field schema for paper ledger row

Same fields as a live trade-ledger row (canonical schema = the dict
literal in `options.py:_execute_entry` and
`futures.py:_execute_entry` — fields: `id`, `date`, `scrip`,
`option_key`, `asset_type`, `underlying`, `strike`, `option_type`,
`expiry`, `trading_symbol`, `order_type`, `entry_time`, `entry_ts`,
`entry_price`, `qty`, `trigger_spot`, `trigger_level`,
`max_min_target_price`, `target_level_reached`, `exit_*`, `pnl_*`,
`duration_seconds`, `status`, `auto`, `mode`, `kotak_*_order_id`).

Paper rows differ in two fields:
```
"mode": "PAPER_BOOK"             # always — distinguishes paper rows.
                                  # NOT "PAPER_SHADOW" (prior draft);
                                  # paper is its own book, not a shadow.
"kotak_entry_order_id": null     # always — paper never sends orders.
"kotak_exit_order_id": null      # always.
```

**No cross-pointers between the two ledgers.** Paper and live are
fully independent — there is no `shadows_trade_id` or
`paper_trade_id`. If Ganesh wants to correlate a paper row to a
live row he can do so by `(date, underlying, entry_time)` proximity
in the UI; we don't need a stored linkage.

Paper id space is independent of live id space — `next_paper_id()`
counts only paper rows.

### Kill-switch + market-hours behaviour

- **In-hours / square-off:** shared. Paper tick uses the same
  `_auto_in_hours` / `_auto_at_or_after_squareoff` checks as live.
- **apply_to (options/futures/both):** shared. Paper tick respects
  `config_loader.options_enabled()` and `futures_enabled()`.
- **Per-day cap:** **independent counts**. Live cap counts live
  rows; paper cap counts paper rows. Same numeric cap applies to
  both (cap source = `config_loader.per_day_cap(idx)` — one value).
- **Position-verify (Kotak):** live only. Paper never calls Kotak.
- **Margin pre-check:** live only. Paper assumes infinite margin —
  this is the explicit user requirement ("we wnat square all").
- **Kill switch (the safety stop):** **live only**. Paper continues
  to take entries and exits even when the kill switch is set. The
  /paper-trades page must surface this clearly with a banner
  ("Paper book runs even when kill switch is set"). If Ganesh ever
  wants paper to also halt on kill switch, that's a future toggle.

### Risk

- **Per-tick cost:** one extra JSON read/write per index per tick.
  At 3 indices × 3s cadence × small JSON files this is negligible.
- **Disk growth:** paper ledger grows at the same rate as live
  ledger. Same JSON-list-in-a-file shape — fine until thousands of
  rows. Out of scope to migrate to sqlite for now.
- **UI confusion:** users mistaking paper rows for live rows.
  Mitigated by separate page (`/paper-trades`), separate URL,
  prominent "PAPER BOOK" header banner on the page.
- **Strategy logic drift between live and paper:** because both
  branches call the **same** `_compute_entry_signal` and
  `_check_exit_reason` functions (Phase 2 extracts these from
  inline tick code), a refactor that changes the live signal is
  guaranteed to change paper too. This is the whole point — paper
  must mirror live decisions. The spec forbids paper having its
  own copy of the decision logic.
- **Kill-switch divergence (intentional):** if the kill switch is
  set, live freezes but paper keeps trading. UI must communicate
  this; documented above. If a future change makes paper also
  freeze on kill switch, it should be a config knob, not silent.

### Done when

- Synthetic in-hours tick that triggers an entry adds a row to
  both `data/trade_ledger.json` and `data/paper_ledger.json` with
  the same idx_name/side/spot/trigger_level.
- Synthetic tick where the live entry is BLOCKED (e.g.,
  position-verify denial, margin shortfall) still adds a paper-OPEN
  row — verified end-to-end via a regression test that mocks
  `place_order_safe` to return RESULT_BLOCKED.
- Synthetic exit signal on an idx where live has no OPEN row but
  paper does → live no-op, paper closes the OPEN paper row.
- Square-off cutoff closes both ledgers' OPEN rows independently.
- `/paper-trades` page renders the paper ledger; visually similar
  to `/trade_ledger` but with a "PAPER BOOK" banner and a note
  that kill switch does not freeze paper.
- `/paper-trades.xlsx` exports the paper ledger to Excel.
- Live-ledger byte content for a representative replay is
  byte-identical to the pre-Phase-2 state — verified by hash diff
  on a stored fixture, confirming the live branch was not
  perturbed by the refactor.
- New tests in `tests/test_paper_book.py`:
  `test_paper_entry_when_live_ok`,
  `test_paper_entry_when_live_blocked`,
  `test_paper_exit_when_no_live_position`,
  `test_paper_square_off_independent`,
  `test_paper_per_day_cap_independent`,
  `test_paper_skips_kill_switch_freeze`.

---

## Phase 3 — Trailing SL variant D

### What it is

A new stoploss variant that trails the SL **one rung behind** the
LTP's current Gann rung as price walks favourably. Initial SL when
entering is the **entry price** (breakeven) — the industrial-standard
simplest trailing-SL initialisation.

### Concept walkthrough (user's example, BUY side)

> ltp=4000, BUY=3900, BUY_WA=3800, T1=3700 (illustrative numbers from
> user's message — direction-agnostic concept)
>
> Long entry at 4000. Initial SL = 4000 (breakeven).
> If LTP rises to **T1**, SL trails to the rung **behind T1** = BUY_WA.
> If LTP rises to **T2**, SL trails to **T1**.
> If LTP rises to **T3**, SL trails to **T2**.
> ... and so on through T4, T5.

User's clarifying answer: *"choose a"* (option a — trail one rung
BEHIND LTP's current rung).

### Concept (formal)

For a long position, with rung-ladder
`[BUY, BUY_WA, T1, T2, T3, T4, T5]`:

```
current_rung = highest rung r such that spot >= rung_price(r), else None
sl_rung      = rung immediately before current_rung in the ladder
sl_price     = rung_price(sl_rung) if sl_rung else entry_price
```

`sl_price` only **ratchets up**, never down. A pullback that drops
the current_rung does not lower the SL.

For a short: mirror image using `[SELL, SELL_WA, S1, S2, S3, S4, S5]`
with `<=`. Ratchets only downward.

### Spot vs. instrument LTP — DECISION

Both options and futures variant D use **spot** as the trigger
price, compared against `trail_sl_price` (which is itself a spot-rung
price). This matches existing variant-C semantics (`spot < sell_lvl`
at futures.py:137) and avoids the basis-mismatch hazard of comparing
fut_ltp against a spot-derived rung.

```
if is_long  and spot is not None and spot <= trail_sl_price:  # LONG triggered
if not is_long and spot is not None and spot >= trail_sl_price:  # SHORT triggered
```

When triggered, the closing order's *fill price* is `fut_ltp`
(futures) or `opt_ltp` (options) — same as A/B/C. Trail logic is
spot-driven; close fill is instrument-driven.

### State

Each OPEN trade gains two ledger fields:
```
"trail_high_rung": "T2"  | "S2"   # highest rung reached so far
                                   # (long uses BUY-side names;
                                    # short uses SELL-side names)
"trail_sl_price":  3850.0          # current SL (spot price);
                                   # ratchets monotonically.
```
`update_open_trades_mfe()` (`backend/strategy/common.py:65–94`) already
walks rungs once per snapshot refresh and updates
`target_level_reached`. We extend it to also compute `trail_high_rung`
and `trail_sl_price`.

**CRITICAL:** `update_open_trades_mfe()` is called from
`backend/snapshot.py:138` on **every** snapshot refresh — including
weekends and outside trading hours, when fetch_quotes still returns
last-known LTPs. The new trail-update logic **must** be guarded with
`_auto_in_hours(now_ist())` so the trail does not ratchet on stale
weekend prints. Otherwise Monday's open will fire SL_TRAIL on a
normal opening dip from a weekend high. (Existing
`target_level_reached` is advisory and can stay un-gated; the trail
SL is load-bearing.)

**Snapshot-thread error swallowing:** the call site at
`backend/snapshot.py:137–140` wraps `update_open_trades_mfe(data)` in
`try/except: pass`. Trail-SL update writes added inside this function
will be silently dropped on any exception. This phase must **add a
log line** in the new trail-update branch's exception path (a narrow
`try/except` around the `read/compute/write` block, logging via the
same `log` callable the SnapshotStore uses) so a malformed quote or
ledger doesn't quietly disarm the SL.

**Read-modify-write race:** `update_open_trades_mfe` reads the trade
ledger, mutates rows, and writes back (common.py:68, 94). Strategy
ticks (`option_auto_strategy_tick`, `future_auto_strategy_tick`)
also read+write the same file. Today this is racy for
`max_min_target_price` and `target_level_reached` but those fields
are advisory. `trail_sl_price` is load-bearing — a lost write
disarms the SL by one rung. The strategy ticks already hold
`_option_auto_state["lock"]` / `_future_auto_state["lock"]` and
`backend/storage/trades.py:write_trade_ledger` uses `file_lock`,
making the file write itself atomic, but the read-modify-write
window is not. Phase 3 must either (a) move the trail computation
into the strategy tick (under its lock) instead of the snapshot
producer, or (b) accept eventually-consistent semantics and document
that the trail can lag by one snapshot interval (~2s) on contention.
**Decision: option (b)** — 2s lag is below the strategy's tick
cadence and the trail price only ever ratchets monotonically, so a
lost write is overwritten by the next refresh with the correct (or
strictly-higher) value. No data corruption, just delayed arming.

### Wiring (verified)

- `config.yaml` schema: `stoploss.active` accepts new value `"D"`.
  No new sub-keys required (ladder is implicit).
- `backend/config_loader.py:117`: extend `VALID_STOPLOSS = {"A","B","C","D"}`.
- `backend/strategy/common.py::update_open_trades_mfe()` — extend the
  loop to also write `trail_high_rung` and `trail_sl_price`. Wrap the
  trail-update branch in an `_auto_in_hours(now_ist())` check.
- `backend/strategy/options.py::_check_exit_reason()` (lines 96–166):
  - **Convert the existing `else: # "C"` clause at line 146 into
    `elif active_sl == "C":`** — otherwise `active_sl == "D"` falls
    through into C's branch, which is the spot-reversal SL, and BOTH
    SLs would arm (violating the "exactly ONE variant runs" rule
    that the existing comment at line 131 explicitly states).
  - Add a new `elif active_sl == "D":` branch that reads
    `trail_sl_price` from `open_t` and compares against `spot` per
    the rule above. **Must guard `trail_sl_price is None`** — newly
    opened trades won't have it set until the next in-hours
    `update_open_trades_mfe` refresh runs (one snapshot interval,
    ~2s after entry). When None, the variant-D branch returns no
    exit reason; the trade is unprotected for that brief window,
    which is acceptable given the 2s cadence.
  - Update the docstring at lines 104–106 from `(A | B | C)` to
    `(A | B | C | D)` and document variant D inline.
- `backend/strategy/futures.py::_check_futures_exit_reason()`
  (lines 99–153): same two changes — convert
  `else: # "C"` at line 135 to `elif active == "C":`, add
  `elif active == "D":` branch comparing spot to `trail_sl_price`.
  Update the module docstring and the inline comment block at
  lines 21–25.
- `frontend/templates/config.html` — add D radio option to the
  stoploss-active radio group.
- `frontend/templates/trade_ledger.html` — show `trail_sl_price` in
  the OPEN-row column block when variant D is active. (Nice-to-have
  visibility — not load-bearing.)

### Initial SL choice

User said *"go with industrial standerts best"*. Three industrial-standard
options for trailing-SL initialisation:

- (i) **Breakeven (entry price).** Simplest, no extra config knob.
  Canonical starting point in published literature (Tharp, Elder).
- (ii) Fixed % below entry until first rung reached.
- (iii) Wait until the first favourable rung is crossed before arming
  the trail at all.

**Decision: (i) breakeven.** Reasons:
- No extra config knob to expose, fits user's "use defaults" stance.
- Composes cleanly with the ratchet rule: `trail_sl_price` starts
  at `entry_price`, only moves up.
- (iii) was considered (a "delayed-arm" trail) and rejected because
  it leaves the position unprotected between entry and first rung
  cross — which can be 0.5–1 % away on indices and would surprise
  Ganesh if a sudden adverse move happened pre-arm.

### Risk

- **Long-side whipsaw at entry:** if LTP barely moves above entry
  then dips, SL fires immediately at breakeven. Acceptable — that's
  how breakeven stops are *supposed* to behave.
- **Short-side whipsaw at entry, compounded by rounding asymmetry:**
  for a short futures entry, `_round_for_sell` (futures.py:344)
  rounds the entry UP. The very first prints at or below the rounded
  entry trigger SL=entry immediately. This is symmetric to the long
  case, but the rounding asymmetry compounds it. Documented; user
  picked "industrial standard" knowing this.
- **Square-off priority:** at/after configured square_off, the
  existing `AUTO_SQUARE_OFF` code path (options.py:246–256,
  futures.py:207–219) closes everything OPEN regardless of SL
  variant. Variant D does not change this. The
  `status == "OPEN"` guard in the close functions prevents
  double-close if `SL_TRAIL` and `AUTO_SQUARE_OFF` race in the same
  tick (they don't — square-off short-circuits).
- **Rolling out alongside A/B/C:** D is opt-in via config; A/B/C
  unchanged. No flag-day risk.
- **Rung tie-breaking:** if spot exactly equals a rung,
  current_rung is that rung (`>=` semantics for long, `<=` for short).

### Done when

- `stoploss.active = "D"` validates and runs in both options and
  futures strategies.
- A new long trade entered with variant D shows
  `trail_sl_price = entry_price` after the **first in-hours
  snapshot refresh** (not at the literal instant of entry — the trail
  fields are written by `update_open_trades_mfe`, which runs on the
  next refresh).
- As a synthetic LTP walks past T1, T2, T3 in fixture-driven tests,
  `trail_sl_price` ratchets to BUY_WA, T1, T2 monotonically; a
  pullback below current rung does NOT lower `trail_sl_price`.
- An adverse move that drops spot through `trail_sl_price` produces
  exit reason `SL_TRAIL`.
- A regression replay of A, B, C variants on the same fixture
  produces byte-identical close behaviour to the pre-Phase-3 build.
- New test cases live at `tests/test_strategy_trail.py` (verified
  not to exist yet — `tests/` currently contains only
  `test_kotak_api.py`, `test_smoke.py`, `test_storage.py`) with at
  minimum: `test_trail_initial_breakeven`, `test_trail_ratchets_up`,
  `test_trail_does_not_lower`, `test_trail_fires_on_pullback`,
  `test_trail_gated_by_in_hours`, `test_trail_none_guard`,
  `test_abc_variants_unchanged`.

---

## Cross-phase concerns

### Config compatibility

Existing `config.yaml` files keep working through all three phases:
- Phase 1: new optional level picks (T4/T5/S4/S5) added to the
  enum; existing picks remain valid.
- Phase 2: no config schema change.
- Phase 3: new value `"D"` added to `stoploss.active` enum;
  default stays `"C"`. Existing configs unchanged.

### Test surface

For each phase we run:
- A unit-test sanity pass on the affected pure-math modules.
- A live-VPS smoke test on the dashboard — page renders, no 500s.
- Phase 3 specifically: synthetic OPEN trade in the ledger with
  mock quotes that walk LTP up the ladder; assert `trail_sl_price`
  ratchets monotonically. Tests live at `tests/test_strategy_trail.py`.

### Deploy plan

Phase boundary = a deployable commit. After each phase:
1. Local visual + smoke check.
2. Push to VPS (`/home/kotak/kotak-dashboard`), restart
   `kotak.service` per `deploy/README.md`.
3. Verify `/api/snapshot-stats` shows healthy refreshes.
4. Move to next phase.

### Rollback

- Phase 1: revert the touched files. Trade rows written during the
  phase carry T4/T5 reached values which become meaningless on
  rollback but don't break anything.
- Phase 2: paper ledger is additive. Revert removes the page+route
  but leaves `data/paper_ledger.json` on disk (harmless).
- Phase 3: flip `stoploss.active` back to A/B/C and revert the
  variant-D code paths. OPEN trades' `trail_*` fields become dormant.

---

## Out of scope

- Migrating either ledger to sqlite/parquet.
- Real-time SSE/WebSocket push (the snapshot pattern at 2 s suffices).
- Per-rung custom config (D doesn't take "trail by N rungs" as a knob
  — it's hardcoded to "1 rung behind", per user's spec).
- Trailing SL on options-LTP (the spot-rung trail is what the user
  asked for; instrument-LTP trail would be a separate variant E).
- Showing paper-shadow rejected entries on the existing /blockers
  page — the paper log page is the single home for paper records.

---

## Open questions

None — all clarifying questions resolved:
- Trailing rule = option (a), one rung behind. *(prior session)*
- Paper trade = same config/logic as live. *(prior session)*
- Initial SL = industrial standard = breakeven (entry price).
  *(prior session)*
- Paper trades do NOT appear on /blockers tab; that tab is for live
  money trades only. *(2026-04-27 user confirmation)*
- Paper book is **fully independent** — paper buys/sells/squares-off
  on its own ledger regardless of whether live succeeded. Kill
  switch freezes live but not paper. Per-day caps applied per ledger
  independently. *(2026-04-27 user confirmation: "buy happens but
  real money blicked by zero cost, paper trade want happes buy and
  that sell time comes we cant sell the real money becoz we was noy
  bought the stock, but in paper we want sell, we wnat square all,
  all are wants happes")*

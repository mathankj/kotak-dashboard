# Trailing SL + Paper Book + Ladder ±5 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three independent features to the kotak-autologin trading dashboard: (1) widen Gann ladder from S3..T3 to S5..T5; (2) add a fully-independent paper-book that runs the same strategy in parallel without sending real orders; (3) add stoploss variant D that trails one rung behind the LTP's current Gann rung.

**Architecture:** Phase 1 widens existing data structures and UI (no logic change). Phase 2 introduces a parallel ledger + a parallel tick function that reuses extracted decision helpers from the live strategies. Phase 3 adds variant D to the existing exit-reason chain and extends `update_open_trades_mfe` to maintain the trail SL field.

**Tech Stack:** Python 3 (Flask, pytest). JSON-file storage for ledgers (`backend/storage/_safe_io` atomic-write + file_lock). HTML templates rendered server-side. SnapshotStore producer thread for hot-cache reads.

**Spec:** `docs/superpowers/specs/2026-04-27-trailing-paper-l5-design.md`

**Conventions for this plan:**
- Always run `python -m pytest tests/ -v` before declaring a step done.
- Each phase ends with a deploy checkpoint (push to VPS, restart `kotak.service`, verify `/api/snapshot-stats`). Do NOT proceed to the next phase until the previous is live and clean.
- The repo is git-tracked; commit after each green task. Use Conventional Commits (`feat:`, `refactor:`, `test:`, `docs:`).
- File paths are absolute-from-repo-root (`backend/...`, `frontend/...`). Read each file before editing it (Edit tool requires this).
- The `else: # "C"` → `elif active == "C":` conversion (Phase 3 Task 14) is load-bearing for SL correctness. Do not skip.

---

## File Map

### Created
- `backend/storage/paper_ledger.py` — Phase 2. Read/write paper ledger JSON. Mirror of `backend/storage/trades.py`.
- `backend/strategy/paper_book.py` — Phase 2. Paper-side state machine (`paper_options_tick`, `paper_futures_tick`, `_paper_execute_entry`, `_paper_execute_exit`).
- `frontend/templates/paper_trades.html` — Phase 2. Paper-book ledger page (clone of `trade_ledger.html` with PAPER BOOK banner).
- `tests/test_gann_levels.py` — Phase 1. Unit tests for `gann_levels` and `compute_target_level_reached` at the new ±5 ladder.
- `tests/test_paper_book.py` — Phase 2. Tests for paper independence, kill-switch divergence, square-off.
- `tests/test_strategy_trail.py` — Phase 3. Tests for variant D ratchet, None-guard, in-hours gate.

### Modified
- `backend/strategy/gann.py` — Phase 1. Extend SELL_LEVELS / BUY_LEVELS to ±5; extend LEVEL_COLORS, *_LEVEL_ORDER; fix "Beyond T3"/"Beyond S3" clamps; update module docstring.
- `backend/config_loader.py` — Phase 1 (extend VALID_CE_TARGETS / VALID_PE_TARGETS) + Phase 3 (extend VALID_STOPLOSS to include "D").
- `frontend/templates/gann.html` — Phase 1. Four new columns; CSS palette; JS arrays.
- `frontend/templates/config.html` — Phase 1 (target dropdown literal lists at ~lines 319/329) + Phase 3 (D radio for stoploss-active).
- `backend/strategy/options.py` — Phase 2 (extract `_compute_entry_signal`) + Phase 3 (variant D in `_check_exit_reason`, convert `else` to `elif`).
- `backend/strategy/futures.py` — Phase 2 (extract `_compute_futures_entry_signal`) + Phase 3 (variant D in `_check_futures_exit_reason`, convert `else` to `elif`).
- `backend/strategy/common.py` — Phase 3. Extend `update_open_trades_mfe` to compute `trail_high_rung` + `trail_sl_price`; gate behind `_auto_in_hours`.
- `backend/snapshot.py` — Phase 3 (only if needed; `update_open_trades_mfe` is already called at line 138 inside `_build_gann_payload` — no change needed unless adding logging).
- `app.py` — Phase 2. Extend `TABS` with "Paper Log" entry; add `/paper-trades` route + `/paper-trades.xlsx` route; extend `_strategy_ticker_loop` to also call `paper_options_tick` / `paper_futures_tick`.
- `frontend/templates/trade_ledger.html` — Phase 3. Show `trail_sl_price` for OPEN rows when variant D is active (nice-to-have).

---

## PHASE 1 — Gann Ladder ±5

Goal: extend the ladder from S3..T3 to S5..T5. Pure data/UI widening; no strategy semantics change.

### Task 1: Lock-in tests for new ladder math

**Files:**
- Create: `tests/test_gann_levels.py`

- [ ] **Step 1.1: Write the failing tests.**

```python
"""Unit tests for the extended Gann ladder (S5..T5)."""
import math

from backend.strategy.gann import (
    BUY_LEVELS, SELL_LEVELS,
    BUY_LEVEL_ORDER, SELL_LEVEL_ORDER,
    LEVEL_COLORS,
    gann_levels, compute_target_level_reached,
)


def test_levels_contain_t4_t5_s4_s5():
    assert "T4" in BUY_LEVELS
    assert "T5" in BUY_LEVELS
    assert "S4" in SELL_LEVELS
    assert "S5" in SELL_LEVELS


def test_level_orders_extended():
    assert "T5" in BUY_LEVEL_ORDER
    assert "S5" in SELL_LEVEL_ORDER


def test_colors_cover_new_levels():
    for k in ("S5", "S4", "T4", "T5"):
        assert k in LEVEL_COLORS


def test_gann_levels_emits_t5_at_correct_price():
    # T5 corresponds to n=+8: price = (sqrt(open) + 8*0.0625)^2
    open_p = 25000.0
    levels = gann_levels(open_p)
    expected_t5 = round((math.sqrt(open_p) + 8 * 0.0625) ** 2, 2)
    assert levels["buy"]["T5"] == expected_t5


def test_gann_levels_emits_s5_at_correct_price():
    open_p = 25000.0
    levels = gann_levels(open_p)
    expected_s5 = round((math.sqrt(open_p) + (-8) * 0.0625) ** 2, 2)
    assert levels["sell"]["S5"] == expected_s5


def test_compute_target_level_reached_emits_t4():
    # Price between T3 and T4 should label "T3"; between T4 and T5 should
    # label "T4". (Highest rung still met.)
    open_p = 25000.0
    levels = gann_levels(open_p)
    px_between_t4_and_t5 = (levels["buy"]["T4"] + levels["buy"]["T5"]) / 2.0
    reached = compute_target_level_reached("B", open_p,
                                            px_between_t4_and_t5, levels)
    assert reached == "T4"


def test_compute_target_level_reached_beyond_t5_not_t3():
    # Regression: was "Beyond T3" — must now be "Beyond T5".
    open_p = 25000.0
    levels = gann_levels(open_p)
    far_above = levels["buy"]["T5"] + 100.0
    reached = compute_target_level_reached("B", open_p, far_above, levels)
    assert reached == "Beyond T5"


def test_compute_target_level_reached_beyond_s5_not_s3():
    open_p = 25000.0
    levels = gann_levels(open_p)
    far_below = levels["sell"]["S5"] - 100.0
    reached = compute_target_level_reached("S", open_p, far_below, levels)
    assert reached == "Beyond S5"
```

- [ ] **Step 1.2: Run tests to verify they fail.**

Run: `python -m pytest tests/test_gann_levels.py -v`
Expected: 8 failures — `KeyError: 'T4'` / `'T5'` / etc., and the "Beyond T5" tests fail with the current "Beyond T3" string.

- [ ] **Step 1.3: Commit the failing tests.**

```bash
git add tests/test_gann_levels.py
git commit -m "test(gann): add failing tests for S5..T5 ladder"
```

### Task 2: Extend gann.py constants and math

**Files:**
- Modify: `backend/strategy/gann.py`

- [ ] **Step 2.1: Read the file.** (Edit tool requires this.)

- [ ] **Step 2.2: Update `SELL_LEVELS` and `BUY_LEVELS`.**

Replace:
```python
SELL_LEVELS = ["S3", "S2", "S1", "SELL_WA", "SELL"]
BUY_LEVELS  = ["BUY", "BUY_WA", "T1", "T2", "T3"]
```
With:
```python
SELL_LEVELS = ["S5", "S4", "S3", "S2", "S1", "SELL_WA", "SELL"]
BUY_LEVELS  = ["BUY", "BUY_WA", "T1", "T2", "T3", "T4", "T5"]
```

- [ ] **Step 2.3: Extend `LEVEL_COLORS`.**

Add four entries to the dict — pick palette tones that extend the existing ramp (deeper red below S3, deeper green above T3):
```python
LEVEL_COLORS = {
    "S5": "#7F0000", "S4": "#8E1818",
    "S3": "#B71C1C", "S2": "#C62828", "S1": "#D32F2F",
    "SELL_WA": "#FF9800", "SELL": "#EF9A9A",
    "BUY": "#A5D6A7", "BUY_WA": "#FF9800",
    "T1": "#81C784", "T2": "#66BB6A", "T3": "#388E3C",
    "T4": "#2E7D32", "T5": "#1B5E20",
}
```

- [ ] **Step 2.4: Extend `BUY_LEVEL_ORDER` and `SELL_LEVEL_ORDER`.**

```python
BUY_LEVEL_ORDER  = ["BUY", "BUY_WA", "T1", "T2", "T3", "T4", "T5"]
SELL_LEVEL_ORDER = ["SELL", "SELL_WA", "S1", "S2", "S3", "S4", "S5"]
```

- [ ] **Step 2.5: Update `gann_levels()` formula loop.**

The current loop uses `n = -(6 - i)` for SELL and `n = i + 2` for BUY,
which assumed 5-element lists indexed 0..4. With 7-element lists those
formulas now yield n ∈ [-8..-2] for SELL (S5=-8, S4=-7, ..., SELL=-2)
and n ∈ [+2..+8] for BUY (BUY=+2, BUY_WA=+3, ..., T5=+8). The formula
is correct as-written for the new lists; verify by manual trace before
moving on. **No code change needed in `gann_levels()` body.**

- [ ] **Step 2.6: Fix the "Beyond T3" / "Beyond S3" clamps.**

In `compute_target_level_reached`, replace:
```python
        t3 = buy.get("T3")
        if t3 is not None and max_min_price > t3:
            reached = "Beyond T3"
```
With:
```python
        t5 = buy.get("T5")
        if t5 is not None and max_min_price > t5:
            reached = "Beyond T5"
```
And similarly for the SELL side: `s3 = sell.get("S3")` → `s5 = sell.get("S5")`, `"Beyond S3"` → `"Beyond S5"`.

- [ ] **Step 2.7: Update the module docstring.**

Replace the level-naming comment:
```
  S3=-6, S2=-5, S1=-4, SELL_WA=-3, SELL=-2  (below open)
  BUY=+2, BUY_WA=+3, T1=+4, T2=+5, T3=+6   (above open)
```
With:
```
  S5=-8, S4=-7, S3=-6, S2=-5, S1=-4, SELL_WA=-3, SELL=-2  (below open)
  BUY=+2, BUY_WA=+3, T1=+4, T2=+5, T3=+6, T4=+7, T5=+8    (above open)
```

- [ ] **Step 2.8: Run tests.**

Run: `python -m pytest tests/test_gann_levels.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 2.9: Run the full smoke test suite.**

Run: `python -m pytest tests/ -v`
Expected: all PASS (no regressions).

- [ ] **Step 2.10: Commit.**

```bash
git add backend/strategy/gann.py tests/test_gann_levels.py
git commit -m "feat(gann): extend ladder to S5..T5"
```

### Task 3: Extend config_loader target validators

**Files:**
- Modify: `backend/config_loader.py`

- [ ] **Step 3.1: Read the file.**

- [ ] **Step 3.2: Extend the target enums (lines ~120–121).**

Replace:
```python
VALID_CE_TARGETS = {"T1", "T2", "T3", "BUY_WA"}
VALID_PE_TARGETS = {"S1", "S2", "S3", "SELL_WA"}
```
With:
```python
VALID_CE_TARGETS = {"T1", "T2", "T3", "T4", "T5", "BUY_WA"}
VALID_PE_TARGETS = {"S1", "S2", "S3", "S4", "S5", "SELL_WA"}
```

**Do NOT widen `VALID_BUY_LEVELS` or `VALID_SELL_LEVELS`** — entry/SL pickers stay BUY/BUY_WA, SELL/SELL_WA per spec.

- [ ] **Step 3.3: Run smoke tests.**

Run: `python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 3.4: Commit.**

```bash
git add backend/config_loader.py
git commit -m "feat(config): allow T4/T5/S4/S5 as target picks"
```

### Task 4: Extend gann.html — table columns, CSS, JS arrays

**Files:**
- Modify: `frontend/templates/gann.html`

- [ ] **Step 4.1: Read the file.** (It's large; you may need offset/limit reads.)

- [ ] **Step 4.2: Add four CSS rules.**

Find the heatmap palette block (around lines 46–63 — `td.lvl-S3` ... `td.lvl-T3`). Add:
```css
td.lvl-S5 { background: #7F0000; color: #fff; }
td.lvl-S4 { background: #8E1818; color: #fff; }
td.lvl-T4 { background: #2E7D32; color: #fff; }
td.lvl-T5 { background: #1B5E20; color: #fff; }
```
And extend the `:is(td.lvl-S3, td.lvl-S2, ..., td.lvl-T3)` font-weight enumeration block (around lines 58–63) to include the four new classes.

- [ ] **Step 4.3: Update JS arrays.**

Find `SELL_LVLS` and `BUY_LVLS` arrays (around lines 267–268). Update to:
```js
const SELL_LVLS = ["S5", "S4", "S3", "S2", "S1", "SELL_WA", "SELL"];
const BUY_LVLS  = ["BUY", "BUY_WA", "T1", "T2", "T3", "T4", "T5"];
```
Find `LTP_LEVEL_CLASSES` (around lines 277–280) and add the four new class names.

- [ ] **Step 4.4: Add four `<th>` columns in the table header.**

Locate the `<thead>` row that lists S3, S2, S1, SELL_WA, SELL on the left and BUY, BUY_WA, T1, T2, T3 on the right. Insert `<th>S5</th><th>S4</th>` immediately before `<th>S3</th>`, and `<th>T4</th><th>T5</th>` immediately after `<th>T3</th>`.

- [ ] **Step 4.5: Verify whether row cells are rendered dynamically or statically — then act accordingly.**

In `gann.html`, the row template likely iterates over `SELL_LVLS` / `BUY_LVLS` in JS (e.g. `for (const lvl of BUY_LVLS) { ... append <td> ... }` around line 347). If so, **the JS array updates from Step 4.3 are sufficient — all 14 cells render automatically.** Do NOT add static `<td>` cells in that case; you'll create duplicates.

Read 60–80 lines around the row-rendering JS first. Decision tree:
- If rows are built dynamically from `SELL_LVLS`/`BUY_LVLS`: no further markup change needed.
- If rows are built from static `<td>` cells in a `<tr>` template: add four `<td class="ltp" data-level="...">` cells matching the existing attribute pattern (read the existing T3 cell to copy verbatim).

- [ ] **Step 4.6: Local smoke test.**

Start the app locally if possible: `python app.py` and open http://localhost:5000/. Verify the Gann page renders 14 level columns without JS console errors.

If a local Kotak login is unavailable, run smoke tests instead:
Run: `python -m pytest tests/ -v`
Expected: all PASS (templates not parsed by tests, but at minimum imports must still work).

- [ ] **Step 4.7: Commit.**

```bash
git add frontend/templates/gann.html
git commit -m "feat(ui): add S5/S4/T4/T5 columns to Gann ladder"
```

### Task 5: Extend config.html target-level dropdowns

**Files:**
- Modify: `frontend/templates/config.html`

- [ ] **Step 5.1: Read the file.**

- [ ] **Step 5.2: Extend the CE target dropdown literal list (around line 319).**

Find the literal Python-ish list `['T1','T2','T3','BUY_WA']` (or however it's templated — could be Jinja `{% for opt in ['T1','T2','T3','BUY_WA'] %}`). Change to `['T1','T2','T3','T4','T5','BUY_WA']`.

- [ ] **Step 5.3: Extend the PE target dropdown literal list (around line 329).**

Same pattern: `['S1','S2','S3','SELL_WA']` → `['S1','S2','S3','S4','S5','SELL_WA']`.

- [ ] **Step 5.4: Verify no other dropdowns were widened.**

Search the file for `['BUY','BUY_WA']` and `['SELL','SELL_WA']` — those (entry, market_open, variant_a/b/c) must stay unchanged.

- [ ] **Step 5.5: Run smoke tests.**

Run: `python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5.6: Local manual check (if possible).**

Open `/config` in the browser. Confirm that:
- Target CE dropdown lists T1/T2/T3/T4/T5/BUY_WA.
- Target PE dropdown lists S1/S2/S3/S4/S5/SELL_WA.
- Entry / market_open / variant A/B/C dropdowns still list ONLY BUY/BUY_WA and SELL/SELL_WA.
- Saving the existing config.yaml unchanged still validates.
- Saving with target.ce_level = "T5" persists and validates.

- [ ] **Step 5.7: Commit.**

```bash
git add frontend/templates/config.html
git commit -m "feat(config-ui): expose T4/T5/S4/S5 as target picks"
```

### Task 6: Phase 1 deploy checkpoint

- [ ] **Step 6.1: Push to remote.**

```bash
git push
```

- [ ] **Step 6.2: SSH to VPS and pull + restart.**

```bash
# On VPS:
cd /home/kotak/kotak-dashboard && git pull && sudo systemctl restart kotak
sudo systemctl is-active kotak
```

- [ ] **Step 6.3: Verify health.**

```bash
curl -s http://localhost:5000/api/snapshot-stats | python -m json.tool
```
Expected: `refresh_count` increments on subsequent calls; `errors` all zero.

Also load `/` in the browser. Confirm the Gann table shows all 14 columns and the heatmap paints correctly when the LTP crosses a new rung.

- [ ] **Step 6.4: Stop here if anything is broken.** Roll back Phase 1 commits if needed before starting Phase 2.

---

## PHASE 2 — Paper Book (independent parallel ledger)

Goal: a parallel virtual book that runs the same strategy logic as live, has its own ledger, its own page+Excel, and operates independently (kill switch freezes live but not paper).

### Task 7: Paper-ledger storage module

**Files:**
- Create: `backend/storage/paper_ledger.py`

- [ ] **Step 7.1: Write the file.**

```python
"""Paper trade ledger JSON store.

data/paper_ledger.json. Mirror of trades.py — atomic writes, per-path
file lock — for the parallel paper book added in Phase 2 of the
trailing-paper-l5 spec.

The paper book is fully independent of the live trade ledger. No
cross-pointers; no migration of any legacy file. Naming is
deliberately distinct from the legacy `data/paper_trades.json` that
trades.py already migrates away from.
"""
import os

from backend.storage._safe_io import atomic_write_json, file_lock, read_json

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
LEDGER_FILE = os.path.join(_REPO_ROOT, "data", "paper_ledger.json")

# Ensure the data dir exists at import — trades.py only does this
# inside its migration helper, which won't run for us.
os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)


def read_paper_ledger():
    return read_json(LEDGER_FILE, [])


def write_paper_ledger(trades):
    try:
        with file_lock(LEDGER_FILE):
            atomic_write_json(LEDGER_FILE, trades)
    except Exception:
        pass


def next_paper_id(trades):
    """Return the next sequential paper id as a string. Independent
    of the live ledger's id space."""
    mx = 0
    for t in trades:
        try:
            n = int(t.get("id", "0"))
            if n > mx:
                mx = n
        except (TypeError, ValueError):
            pass
    return str(mx + 1)
```

- [ ] **Step 7.2: Add a smoke import test.**

Append to `tests/test_smoke.py`:

```python
def test_import_paper_ledger():
    """Phase 2: paper_ledger module must import standalone."""
    from backend.storage.paper_ledger import (  # noqa: F401
        read_paper_ledger, write_paper_ledger, next_paper_id,
    )
```

- [ ] **Step 7.3: Run smoke tests.**

Run: `python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 7.4: Commit.**

```bash
git add backend/storage/paper_ledger.py tests/test_smoke.py
git commit -m "feat(storage): add paper_ledger module"
```

### Task 8: Refactor — extract `_compute_entry_signal` from options strategy

Goal: pull the entry-side detection out of `option_auto_strategy_tick`'s inline body so both the live tick and the paper tick can call it.

**Files:**
- Modify: `backend/strategy/options.py`

- [ ] **Step 8.1: Read `option_auto_strategy_tick` carefully.**

Read `backend/strategy/options.py` lines 223 onward — focus on the per-index loop where `side` (entry direction) is computed from `entry_cfg`, spot, prev_spot, and the level resolutions.

- [ ] **Step 8.2: Add a new pure helper above `option_auto_strategy_tick`.**

The existing inline code at options.py:343-361 has a subtle three-state stamp behavior that **must be preserved**:
- market_open_path enabled, side computed = None (in-channel) → stamp NOW (so we don't re-evaluate market-open every tick).
- market_open_path enabled, side computed = "CE"/"PE" → DEFER stamp (so a missing opt_ltp at this exact tick won't burn the slot — caller stamps later, only after committing to entry).
- market_open_path disabled → stamp NOW (forces fall-through to crossing on subsequent ticks).
- already_evaluated_open=True (only crossing branch runs) → no stamp (open_evaluated isn't relevant for crossing).

The helper returns `(side, stamp_now)` so caller can stamp at the right moment:

```python
def _compute_entry_signal(idx_name, spot, prev_spot, levels, cfg,
                          already_evaluated_open):
    """Pure decision: which side (if any), and should the caller stamp
    `open_evaluated` immediately?

    Returns ("CE"|"PE"|None, stamp_now: bool).

    stamp_now=True means: caller should stamp open_evaluated[idx]=today
    NOW. stamp_now=False means: caller defers (used when side IS set on
    the market-open path, since we want to wait until opt_ltp is
    available before burning the open-evaluation slot).

    No I/O, no ledger reads, no order placement — just the
    market-open-path / crossing-path logic that previously lived
    inline in the live tick. Shared by live and paper books.
    """
    entry_cfg = cfg["entry"]
    mo_buy_lvl  = config_loader.resolve_buy_level (levels, entry_cfg["market_open_buy_level"])
    mo_sell_lvl = config_loader.resolve_sell_level(levels, entry_cfg["market_open_sell_level"])
    cr_buy_lvl  = config_loader.resolve_buy_level (levels, entry_cfg["crossing_buy_level"])
    cr_sell_lvl = config_loader.resolve_sell_level(levels, entry_cfg["crossing_sell_level"])

    side = None
    stamp_now = False
    if not already_evaluated_open:
        if entry_cfg["market_open_path"]:
            if mo_buy_lvl is not None and spot > mo_buy_lvl:
                side = "CE"
            elif mo_sell_lvl is not None and spot < mo_sell_lvl:
                side = "PE"
            # Stamp NOW only if no signal — defer if side is set.
            stamp_now = (side is None)
        else:
            # Path A disabled — stamp NOW so subsequent ticks fall
            # through to the crossing branch.
            stamp_now = True
    elif prev_spot is not None and entry_cfg["crossing_path"]:
        if cr_buy_lvl is not None and prev_spot <= cr_buy_lvl < spot:
            side = "CE"
        elif cr_sell_lvl is not None and prev_spot >= cr_sell_lvl > spot:
            side = "PE"

    return side, stamp_now
```

(Adapt to whatever the existing inline code's exact semantics are — read carefully. The signature MUST be a pure function: no global mutation, no ledger reads.)

- [ ] **Step 8.3: Replace the inline block in `option_auto_strategy_tick` with a call to `_compute_entry_signal`.**

Inside the per-index loop, where `side` is currently computed inline, delete the inline computation (lines 343-368, the entire if/elif block that resolves `option_type`) and replace with:
```python
side, stamp_now = _compute_entry_signal(
    idx_name, spot, prev_spot, levels, cfg,
    already_evaluated_open=already_evaluated_open,  # use the existing local
)
if stamp_now:
    _option_auto_state["open_evaluated"][idx_name] = today_str
# (When stamp_now is False AND side is set, the existing code at
#  ~line 379 stamps right before the _execute_entry call. Leave that
#  stamp untouched — it handles the deferred case.)
```

Verify the existing line ~379 stamp (`_option_auto_state["open_evaluated"][idx_name] = today_str` placed just before _execute_entry) is NOT removed — it's the deferred-stamp branch.

- [ ] **Step 8.4: Run smoke tests.**

Run: `python -m pytest tests/ -v`
Expected: all PASS. (No behaviour change — pure refactor.)

- [ ] **Step 8.5: Commit.**

```bash
git add backend/strategy/options.py
git commit -m "refactor(options): extract _compute_entry_signal"
```

### Task 9: Refactor — extract `_compute_futures_entry_signal`

**Files:**
- Modify: `backend/strategy/futures.py`

- [ ] **Step 9.1: Read `future_auto_strategy_tick` (lines 185 onward).**

- [ ] **Step 9.2: Add `_compute_futures_entry_signal` helper.**

Same shape as Step 8.2 but returns `("BUY"|"SELL"|None, evaluated_mo)`. The futures inline code at lines 286–299 is the source — port verbatim into the helper.

- [ ] **Step 9.3: Replace the inline block with a call to the helper.** Same pattern as Step 8.3.

- [ ] **Step 9.4: Run smoke tests + commit.**

```bash
python -m pytest tests/ -v
git add backend/strategy/futures.py
git commit -m "refactor(futures): extract _compute_futures_entry_signal"
```

### Task 10: Paper book module — write failing tests first

**Files:**
- Create: `tests/test_paper_book.py`

- [ ] **Step 10.1: Write the failing tests.**

```python
"""Tests for the paper book — verifying it operates independently
of the live ledger. (Phase 2 of the trailing-paper-l5 spec.)"""
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def isolated_paper_ledger(tmp_path, monkeypatch):
    """Point paper_ledger.LEDGER_FILE at a temp file for the test."""
    from backend.storage import paper_ledger as pl
    fake = tmp_path / "paper_ledger.json"
    monkeypatch.setattr(pl, "LEDGER_FILE", str(fake))
    return fake


def test_paper_book_imports():
    """Module must import standalone."""
    from backend.strategy.paper_book import (  # noqa: F401
        paper_options_tick, paper_futures_tick,
    )


def test_paper_entry_recorded_when_signal_fires(isolated_paper_ledger):
    """A synthetic entry signal must produce one OPEN paper row."""
    from backend.storage.paper_ledger import read_paper_ledger
    from backend.strategy.paper_book import _paper_execute_entry

    row = {
        "id": "1", "scrip": "NIFTY", "asset_type": "future",
        "underlying": "NIFTY", "order_type": "BUY",
        "entry_price": 25000.0, "qty": 75,
        # ... (full row shape per spec)
    }
    _paper_execute_entry(row)
    rows = read_paper_ledger()
    assert len(rows) == 1
    assert rows[0]["status"] == "OPEN"
    assert rows[0]["mode"] == "PAPER_BOOK"
    assert rows[0]["kotak_entry_order_id"] is None


def test_paper_exit_closes_open_row(isolated_paper_ledger):
    """A synthetic exit closes the matching paper row."""
    from backend.storage.paper_ledger import (
        read_paper_ledger, write_paper_ledger,
    )
    from backend.strategy.paper_book import _paper_execute_exit

    open_row = {
        "id": "1", "scrip": "NIFTY", "asset_type": "future",
        "underlying": "NIFTY", "order_type": "BUY",
        "entry_price": 25000.0, "entry_ts": 1000.0, "qty": 75,
        "status": "OPEN", "mode": "PAPER_BOOK",
    }
    write_paper_ledger([open_row])
    _paper_execute_exit(open_row, ltp=25100.0, reason="TARGET_T1")
    rows = read_paper_ledger()
    assert rows[0]["status"] == "CLOSED"
    assert rows[0]["exit_reason"] == "TARGET_T1"
    assert rows[0]["pnl_points"] == 100.0


def test_paper_independent_when_live_blocked(isolated_paper_ledger):
    """If place_order_safe returns BLOCKED, paper still gets an OPEN row.
    This is the user's stated requirement — paper buys even when live
    has zero margin."""
    # This is a higher-level integration test; flesh out once the
    # paper_options_tick is wired. Mark xfail until Task 12 lands.
    pytest.xfail("written ahead of paper_options_tick")


def test_paper_skips_kill_switch_freeze(isolated_paper_ledger):
    """Kill switch must NOT freeze paper. Paper continues to trade."""
    pytest.xfail("written ahead of paper_options_tick")


def test_paper_per_day_cap_independent(isolated_paper_ledger):
    """Paper count is independent of live count."""
    pytest.xfail("written ahead of paper_options_tick")


def test_paper_square_off_independent(isolated_paper_ledger):
    """End-of-day square-off closes paper OPENs even if live has none."""
    pytest.xfail("written ahead of paper_options_tick")
```

- [ ] **Step 10.2: Run tests to verify failures.**

Run: `python -m pytest tests/test_paper_book.py -v`
Expected: import failures (ModuleNotFoundError on `backend.strategy.paper_book`); xfails are expected.

- [ ] **Step 10.3: Commit the failing tests.**

```bash
git add tests/test_paper_book.py
git commit -m "test(paper_book): add failing tests for paper independence"
```

### Task 11: Paper book module — minimal implementation

**Files:**
- Create: `backend/strategy/paper_book.py`

- [ ] **Step 11.1: Write the module skeleton.**

```python
"""Paper book — runs the live strategy logic against its own ledger.

Operates fully independently of the live trade ledger. Never sends
real orders. Kill switch does not freeze paper. Per-day caps are
counted per ledger.

Spec: docs/superpowers/specs/2026-04-27-trailing-paper-l5-design.md
(Phase 2).
"""
import threading

from backend import config_loader
from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
from backend.storage.paper_ledger import (
    read_paper_ledger, write_paper_ledger, next_paper_id,
)
from backend.strategy.common import (
    _auto_at_or_after_squareoff, _auto_in_hours, _auto_close,
)
from backend.utils import now_ist


_paper_state = {
    "options_lock": threading.Lock(),
    "futures_lock": threading.Lock(),
    "options_open_evaluated": {},
    "futures_open_evaluated": {},
    "options_last_spot": {},
    "futures_last_spot": {},
}


# ---------- low-level paper writes ----------
def _paper_execute_entry(row):
    """Insert an OPEN paper row. `row` MUST be a fully-populated dict
    (same schema as a live trade-ledger row). Caller assigns id; we
    stamp mode/status/kotak_*_order_id."""
    rows = read_paper_ledger()
    row = dict(row)  # never mutate the caller's dict
    row["mode"] = "PAPER_BOOK"
    row["status"] = "OPEN"
    row["kotak_entry_order_id"] = None
    row["kotak_exit_order_id"] = None
    if "id" not in row or not row["id"]:
        row["id"] = next_paper_id(rows)
    rows.insert(0, row)
    write_paper_ledger(rows)
    return row


def _paper_execute_exit(open_row, ltp, reason):
    """Close a paper OPEN row at `ltp` with the given reason."""
    rows = read_paper_ledger()
    for t in rows:
        if t.get("id") == open_row.get("id") and t.get("status") == "OPEN":
            _auto_close(t, float(ltp), now_ist(), reason)
            t["mode"] = "PAPER_BOOK"
            t["kotak_exit_order_id"] = None
            break
    write_paper_ledger(rows)


# ---------- high-level ticks ----------
def paper_options_tick(option_data, option_index_meta, gann_quotes):
    """Paper analogue of option_auto_strategy_tick.
    No `client` param — never sends orders. Reuses the SAME entry
    signal + exit reason functions as the live tick (imported from
    backend.strategy.options) — single source of truth for strategy
    logic."""
    # Implementation in Task 12 — first ship the skeleton + tests pass.
    pass


def paper_futures_tick(future_data, gann_quotes):
    """Paper analogue of future_auto_strategy_tick. See above."""
    # Implementation in Task 12.
    pass
```

- [ ] **Step 11.2: Run tests.**

Run: `python -m pytest tests/test_paper_book.py -v`
Expected: import test PASS; entry/exit tests PASS; the four xfail tests still xfail.

- [ ] **Step 11.3: Commit.**

```bash
git add backend/strategy/paper_book.py
git commit -m "feat(paper_book): module skeleton with low-level entry/exit"
```

### Task 12: Paper book — full options + futures tick implementation

**Files:**
- Modify: `backend/strategy/paper_book.py`

- [ ] **Step 12.1: Implement `paper_options_tick`.**

Mirror the structure of `option_auto_strategy_tick` from `backend/strategy/options.py` but:
- Read/write `paper_ledger` instead of `trade_ledger`.
- Use `_paper_state["options_lock"]` etc. instead of the live lock.
- Reuse the extracted `_compute_entry_signal` from `backend.strategy.options` (import lazily to avoid circular).
- Reuse `_check_exit_reason` from `backend.strategy.options`.
- Square-off branch closes everything OPEN in the paper ledger.
- Skip Kotak position verify (paper has no Kotak position).
- Never call `place_order_safe`.
- On entry, build the same row dict shape as `_execute_entry` builds
  for live, then call `_paper_execute_entry(row)` directly.

**Per-day cap — paper-side, independent of live:**
The live tick uses `_can_open_more(idx_name, counts)` from options.py
where `counts` is derived from the live ledger. Paper must NOT reuse
that helper (it's closed over live counts). Inside paper_options_tick,
build paper-side counts directly:

```python
paper_rows = read_paper_ledger()
today = now_ist().strftime("%Y-%m-%d")
paper_counts = {}  # idx_name -> count of TODAY's paper entries
for r in paper_rows:
    if r.get("asset_type") == "option" and r.get("date") == today:
        u = r.get("underlying")
        if u:
            paper_counts[u] = paper_counts.get(u, 0) + 1

def _paper_can_open_more(idx_name):
    cap = config_loader.per_day_cap(idx_name)
    return paper_counts.get(idx_name, 0) < cap
```

Use `_paper_can_open_more(idx_name)` as the gate before paper entry —
mirrors the live `_can_open_more` shape but reads paper counts only.

- [ ] **Step 12.2: Implement `paper_futures_tick`.** Same pattern, mirroring `future_auto_strategy_tick`. Reuse `_compute_futures_entry_signal` and `_check_futures_exit_reason` from `backend.strategy.futures` (lazy import).

- [ ] **Step 12.3: Flip the four xfails in tests/test_paper_book.py to real assertions.**

Replace each `pytest.xfail("...")` with the actual test body. Use mocks/monkeypatch to feed synthetic quotes to the paper tick. For `test_paper_independent_when_live_blocked`, run `paper_options_tick(synthetic_data)` directly — it doesn't even know about the live path, so "live blocked" is effectively a no-op for paper.

- [ ] **Step 12.4: Run tests.**

Run: `python -m pytest tests/test_paper_book.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 12.5: Run the full test suite.**

Run: `python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 12.6: Commit.**

```bash
git add backend/strategy/paper_book.py tests/test_paper_book.py
git commit -m "feat(paper_book): full options+futures tick implementations"
```

### Task 13: Wire paper ticks into the strategy ticker loop

**Files:**
- Modify: `app.py`

- [ ] **Step 13.1: Read `_strategy_ticker_loop` (around line 779).**

- [ ] **Step 13.2: Import the paper ticks at the top of app.py.**

Add near the existing `from backend.strategy.options import ...` block:
```python
from backend.strategy.paper_book import (
    paper_options_tick, paper_futures_tick,
)
```

- [ ] **Step 13.3: Add paper tick calls inside `_strategy_ticker_loop`.**

After the live `option_auto_strategy_tick(...)` call:
```python
try:
    paper_options_tick(data, meta, gann_quotes)
except Exception as e:
    print(f"[ticker] paper options tick failed: "
          f"{type(e).__name__}: {e}")
```

After the live `future_auto_strategy_tick(...)` call (inside the `if config_loader.futures_enabled():` block):
```python
try:
    paper_futures_tick(fut_data, gann_quotes)
except Exception as e:
    print(f"[ticker] paper futures tick failed: "
          f"{type(e).__name__}: {e}")
```

- [ ] **Step 13.4: Run smoke tests.**

Run: `python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 13.5: Commit.**

```bash
git add app.py
git commit -m "feat(ticker): also drive paper book ticks"
```

### Task 14: Paper trades page + Excel export route

**Files:**
- Create: `frontend/templates/paper_trades.html`
- Modify: `app.py`

- [ ] **Step 14.1: Read `frontend/templates/trade_ledger.html` and `app.py` around the existing `/trades` route (line 364) and `/trades.xlsx` route (line 386).**

(Note: the live trade-log route is `/trades`, not `/trade-ledger`. Tab key `"trades"`. Don't grep for `/trade-ledger` — won't find anything.)

- [ ] **Step 14.2: Create `frontend/templates/paper_trades.html`.**

Clone `trade_ledger.html` and:
- Change the page title to "Paper Book".
- Add a prominent banner at the top: `<div class="banner banner-paper">PAPER BOOK — These trades are virtual. The kill switch does NOT freeze the paper book.</div>` (style as needed).
- Change every references to "trades" in the data context to "paper_trades" (or whatever variable the new route passes).
- Change the xlsx export link to `/paper-trades.xlsx`.
- Pass `active="paper_trades"` to the tabs.

- [ ] **Step 14.3: Add the `/paper-trades` route to app.py.**

Locate the existing `/trades` route at app.py:364. Add immediately after:
```python
@app.route("/paper-trades")
def paper_trades_page():
    from backend.storage.paper_ledger import read_paper_ledger
    rows = read_paper_ledger()
    # Same stats computation as /trade-ledger but on the paper ledger.
    stats = compute_stats(rows)
    return render_template(
        "paper_trades.html",
        tabs=TABS, active="paper_trades",
        trades=rows, stats=stats,
    )
```

- [ ] **Step 14.4: Add the `/paper-trades.xlsx` route.**

Clone the existing `/trades.xlsx` route at app.py:386. Swap data source to `read_paper_ledger`. Filename: `paper_ledger_<YYYYMMDD>.xlsx`.

- [ ] **Step 14.5: Extend `TABS` (around app.py:100).**

Add a new entry:
```python
{"key": "paper_trades", "url": "/paper-trades", "label": "Paper Log"},
```
(Order in the list is the on-page order. Place it next to the existing trade-ledger tab.)

- [ ] **Step 14.6: Run smoke tests.**

Run: `python -m pytest tests/ -v`
Expected: all PASS (test_smoke imports app.py — must not crash).

- [ ] **Step 14.7: Local manual check (if possible).**

- Open `/paper-trades` — page renders, banner visible, table present (may be empty initially).
- Click `paper-trades.xlsx` — file downloads.
- Tab nav shows "Paper Log".

- [ ] **Step 14.8: Commit.**

```bash
git add frontend/templates/paper_trades.html app.py
git commit -m "feat(ui): /paper-trades page + xlsx export + tab"
```

### Task 15: Phase 2 deploy checkpoint

- [ ] **Step 15.1: Push.**
```bash
git push
```

- [ ] **Step 15.2: VPS pull + restart.**
```bash
# On VPS:
cd /home/kotak/kotak-dashboard && git pull && sudo systemctl restart kotak
sudo systemctl is-active kotak
```

- [ ] **Step 15.3: Verify.**
- `curl -s http://localhost:5000/api/snapshot-stats` healthy.
- `/paper-trades` page loads.
- After one in-hours tick that fires an entry signal: both `data/trade_ledger.json` and `data/paper_ledger.json` should have new rows. Tail both files and watch.

- [ ] **Step 15.4: Stop here if anything is broken.**

---

## PHASE 3 — Trailing SL Variant D

Goal: add a stoploss variant that trails one rung behind the LTP's current Gann rung. Initial SL = entry price (breakeven). Triggers off **spot** (not fut_ltp / opt_ltp) for the comparison; closes at instrument LTP.

### Task 16: Failing tests for variant D

**Files:**
- Create: `tests/test_strategy_trail.py`

- [ ] **Step 16.1: Write the tests.**

```python
"""Tests for stoploss variant D — trailing along the Gann ladder.

Spec: docs/superpowers/specs/2026-04-27-trailing-paper-l5-design.md
(Phase 3).
"""
from unittest.mock import patch
import pytest


def test_trail_initial_breakeven():
    """A fresh OPEN trade with no trail_sl_price set yet must NOT
    trigger SL_TRAIL — the variant-D branch must None-guard."""
    from backend.strategy.options import _check_exit_reason
    open_t = {
        "option_type": "CE", "entry_price": 100.0,
        "trail_sl_price": None,
    }
    cfg_active_d = {"stoploss": {"active": "D"}, "target": {"ce_level": "T1", "pe_level": "S1"}}
    with patch("backend.strategy.options.config_loader.get",
               return_value=cfg_active_d):
        # spot well below entry should NOT fire
        result = _check_exit_reason(open_t, opt_ltp=80.0, spot=24500.0,
                                    buy_lvl=None, sell_lvl=None,
                                    ce_target_lvl=None,
                                    pe_target_lvl=None)
        assert result != "SL_TRAIL"


def test_trail_ratchets_up():
    """update_open_trades_mfe must ratchet trail_sl_price upward
    monotonically as spot crosses higher rungs."""
    from backend.strategy.common import update_open_trades_mfe
    # Test body: insert an OPEN trade in a temp ledger; call
    # update_open_trades_mfe with synthetic quotes; assert
    # trail_sl_price ratcheted to the rung BEHIND the current rung.
    pytest.skip("flesh out after Task 18 implements the ratchet")


def test_trail_does_not_lower():
    """A pullback below current rung must NOT lower trail_sl_price."""
    pytest.skip("flesh out after Task 18")


def test_trail_fires_on_pullback():
    """Spot dropping back through trail_sl_price must produce SL_TRAIL."""
    pytest.skip("flesh out after Task 17")


def test_trail_gated_by_in_hours():
    """update_open_trades_mfe must NOT update trail_sl_price outside
    market hours / weekends."""
    pytest.skip("flesh out after Task 18")


def test_trail_none_guard():
    """trail_sl_price=None must not trigger SL_TRAIL (covers initial
    one-tick window between entry and first ratchet)."""
    pytest.skip("merged into test_trail_initial_breakeven")


def test_abc_variants_unchanged():
    """Variants A, B, C must produce identical exit decisions to the
    pre-Phase-3 build on a fixed fixture."""
    pytest.skip("regression — implement once D ships")
```

- [ ] **Step 16.2: Run tests to verify expected failures/skips.**

Run: `python -m pytest tests/test_strategy_trail.py -v`
Expected: `test_trail_initial_breakeven` FAILs (because `_check_exit_reason` doesn't yet accept variant D in the validator and falls through `else: # "C"`); other tests skip cleanly.

- [ ] **Step 16.3: Commit.**

```bash
git add tests/test_strategy_trail.py
git commit -m "test(trail): add failing tests for variant D"
```

### Task 17: Validator — accept "D"

**Files:**
- Modify: `backend/config_loader.py`

- [ ] **Step 17.1: Read.**

- [ ] **Step 17.2: Extend `VALID_STOPLOSS` (line 117).**

```python
VALID_STOPLOSS = {"A", "B", "C", "D"}
```

- [ ] **Step 17.3: Run smoke tests + commit.**

```bash
python -m pytest tests/ -v
git add backend/config_loader.py
git commit -m "feat(config): accept stoploss variant D in validator"
```

### Task 18: Convert `else: # "C"` → `elif active == "C":` in BOTH strategies

This is the load-bearing safety fix called out in the spec — D must NOT fall into C's branch.

**Files:**
- Modify: `backend/strategy/options.py` (line 146)
- Modify: `backend/strategy/futures.py` (line 135)

- [ ] **Step 18.1: In options.py, locate `else: # "C"` at line 146.** Replace with:
```python
    elif active_sl == "C":
```

- [ ] **Step 18.2: In futures.py, locate `else: # "C"` at line 135.** Replace with:
```python
    elif active == "C":
```

- [ ] **Step 18.3: Update the docstrings.**

In options.py around line 105: `(A | B | C)` → `(A | B | C | D)`.
In futures.py around lines 21–25: same update + add a paragraph describing variant D briefly:
```
  D) Trailing along the Gann ladder — SL trails one rung behind the
     spot's current rung. Initial SL = entry price. Triggers on spot
     crossing trail_sl_price; close fills at fut_ltp.
```

- [ ] **Step 18.4: Run smoke tests + commit.**

```bash
python -m pytest tests/ -v
git add backend/strategy/options.py backend/strategy/futures.py
git commit -m "refactor(strategy): make C branch explicit (prep for D)"
```

### Task 19: Add variant D branch to options exit-reason

**Files:**
- Modify: `backend/strategy/options.py`

- [ ] **Step 19.1: After the `elif active_sl == "C":` block, add:**

```python
    elif active_sl == "D":
        # Trailing along Gann ladder. trail_sl_price is set by
        # update_open_trades_mfe on each in-hours snapshot refresh
        # (~2s cadence). Until the first refresh after entry it may
        # be None — guard explicitly.
        # Direction: CE = bullish-bet (long premium), exit when spot
        # reverses DOWN through trail. PE = bearish-bet (long
        # premium on a put), exit when spot reverses UP through
        # trail. Both branches are "long the option premium" but
        # walk opposite spot ladders.
        trail = open_t.get("trail_sl_price")
        if trail is not None and spot is not None:
            if side == "CE" and spot <= trail:
                return "SL_TRAIL"
            if side == "PE" and spot >= trail:
                return "SL_TRAIL"
```

- [ ] **Step 19.2: Run trail tests.**

Run: `python -m pytest tests/test_strategy_trail.py::test_trail_initial_breakeven -v`
Expected: PASS (None-guard hit).

- [ ] **Step 19.3: Commit.**

```bash
git add backend/strategy/options.py
git commit -m "feat(options): add stoploss variant D — trailing"
```

### Task 20: Add variant D branch to futures exit-reason

**Files:**
- Modify: `backend/strategy/futures.py`

- [ ] **Step 20.1: After the `elif active == "C":` block, add:**

```python
    elif active == "D":
        trail = open_t.get("trail_sl_price")
        if trail is not None and spot is not None:
            if is_long and spot <= trail:
                return "SL_TRAIL"
            if (not is_long) and spot >= trail:
                return "SL_TRAIL"
```

- [ ] **Step 20.2: Run smoke tests + commit.**

```bash
python -m pytest tests/ -v
git add backend/strategy/futures.py
git commit -m "feat(futures): add stoploss variant D — trailing"
```

### Task 21: Extend `update_open_trades_mfe` to maintain the trail

**Files:**
- Modify: `backend/strategy/common.py`

- [ ] **Step 21.1: Read the current function (lines 65–94).**

- [ ] **Step 21.2: Critical context — quote-lookup keying.**

`update_open_trades_mfe` is called with `quotes_by_symbol = data` from snapshot.py:138 (`_build_gann_payload`). `data` comes from `fetch_quotes()` which keys by SPOT symbols (e.g. `"NIFTY"`, `"BANKNIFTY"`) — NOT by option keys (`"NIFTY 25000 CE"`) or future keys.

Therefore for OPTION trades, `q = quotes_by_symbol.get(t["scrip"])` is **already None** today and the function `continue`s at line 75 — option trades don't get MFE updates either. (Pre-existing limitation; not introduced by this plan.)

To make trail SL work for both options AND futures, the trail block must look up the **spot quote separately** based on `t["underlying"]` (which all entries — option and future — populate). Use `INDEX_OPTIONS_CONFIG[underlying]["spot_symbol_key"]` to resolve the spot key. Branch ladder direction on bias (`option_type` for options, `order_type` for futures), NOT solely on `order_type` (which would mis-direct PE trades that are recorded as `order_type="BUY"` but bet bearish).

- [ ] **Step 21.3: Refactor the function body — extract the variant-D block so it runs even when `q` is None for options.**

The new structure:

```python
def update_open_trades_mfe(quotes_by_symbol):
    """For every OPEN trade, update max_min_target_price /
    target_level_reached / (variant D) trail_sl_price."""
    trades = read_trade_ledger()
    changed = False
    cfg = config_loader.get()
    trail_active = cfg["stoploss"]["active"] == "D"
    in_hours = _auto_in_hours(now_ist())

    for t in trades:
        if t.get("status") != "OPEN":
            continue

        # ---- existing MFE block (unchanged for trades where q exists) ----
        q = quotes_by_symbol.get(t["scrip"])
        if q:
            ltp = q.get("ltp")
            if ltp is not None:
                prev_mfe = t.get("max_min_target_price")
                if t["order_type"] == "BUY":
                    new_mfe = ltp if prev_mfe is None else max(prev_mfe, ltp)
                else:
                    new_mfe = ltp if prev_mfe is None else min(prev_mfe, ltp)
                if new_mfe != prev_mfe:
                    t["max_min_target_price"] = round(new_mfe, 2)
                    changed = True
                side_bs = "B" if t["order_type"] == "BUY" else "S"
                reached = compute_target_level_reached(
                    side_bs, t["entry_price"], new_mfe, q.get("levels"))
                if reached and reached != t.get("target_level_reached"):
                    t["target_level_reached"] = reached
                    changed = True

        # ---- Variant D: trail SL — gated, walks SPOT ladder ----
        # Only in-hours; trail_sl_price is load-bearing for SL correctness
        # and must not ratchet on stale weekend prints.
        if not (trail_active and in_hours):
            continue
        try:
            spot_q = _resolve_spot_quote(t, quotes_by_symbol)
            if not spot_q:
                continue
            spot = spot_q.get("ltp")
            spot_levels = spot_q.get("levels") or {}
            if spot is None:
                continue
            new_trail, new_high = _compute_trail_for_trade(
                t, spot, spot_levels)
            if new_trail is None:
                continue
            prev = t.get("trail_sl_price")
            # Ratchet direction depends on bias:
            #   bullish bias (CE / future BUY) — trail rises monotonically
            #   bearish bias (PE / future SELL) — trail falls monotonically
            is_bullish = _trade_is_bullish(t)
            if prev is None \
                    or (is_bullish and new_trail > prev) \
                    or ((not is_bullish) and new_trail < prev):
                t["trail_sl_price"] = round(float(new_trail), 2)
                t["trail_high_rung"] = new_high
                changed = True
        except Exception as e:
            # Snapshot-thread error swallowing was flagged in the spec.
            # Log explicitly so a malformed quote doesn't silently
            # disarm the trail SL.
            print(f"[trail] update failed for trade {t.get('id')}: "
                  f"{type(e).__name__}: {e}")

    if changed:
        write_trade_ledger(trades)
```

- [ ] **Step 21.4: Add the three helpers above the function.**

```python
def _trade_is_bullish(t):
    """CE option = bullish; PE option = bearish; future BUY = bullish; future SELL = bearish."""
    if t.get("asset_type") == "option":
        return t.get("option_type") == "CE"
    return t.get("order_type") == "BUY"


def _resolve_spot_quote(t, quotes_by_symbol):
    """Look up the SPOT quote for a trade. Options trades store
    `t["scrip"]` as the option key, so we resolve via
    INDEX_OPTIONS_CONFIG[underlying]["spot_symbol_key"]. Futures
    trades may also be keyed by the futures-instrument symbol, so we
    use the same indirection for consistency."""
    from backend.kotak.instruments import INDEX_OPTIONS_CONFIG
    underlying = t.get("underlying")
    if not underlying:
        return None
    cfg = INDEX_OPTIONS_CONFIG.get(underlying) or {}
    spot_key = cfg.get("spot_symbol_key")
    if not spot_key:
        return None
    return quotes_by_symbol.get(spot_key)


def _compute_trail_for_trade(t, spot, spot_levels):
    """Returns (new_trail_price, new_high_rung_name) for variant D, or
    (None, None) if the ladder can't be resolved."""
    from backend.strategy.gann import BUY_LEVEL_ORDER, SELL_LEVEL_ORDER
    is_bullish = _trade_is_bullish(t)
    entry_price = t.get("entry_price")
    if is_bullish:
        ladder = [(n, (spot_levels.get("buy") or {}).get(n))
                  for n in BUY_LEVEL_ORDER]
        ladder = [(n, p) for n, p in ladder if p is not None]
        # current_idx = highest rung with spot >= price(rung)
        current_idx = -1
        for i, (_n, p) in enumerate(ladder):
            if spot >= p:
                current_idx = i
        if current_idx < 0:
            # Below first rung — initial breakeven (entry).
            return (entry_price, None)
        if current_idx == 0:
            return (entry_price, ladder[0][0])
        return (ladder[current_idx - 1][1], ladder[current_idx][0])
    else:
        ladder = [(n, (spot_levels.get("sell") or {}).get(n))
                  for n in SELL_LEVEL_ORDER]
        ladder = [(n, p) for n, p in ladder if p is not None]
        current_idx = -1
        for i, (_n, p) in enumerate(ladder):
            if spot <= p:
                current_idx = i
        if current_idx < 0:
            return (entry_price, None)
        if current_idx == 0:
            return (entry_price, ladder[0][0])
        return (ladder[current_idx - 1][1], ladder[current_idx][0])
```

- [ ] **Step 21.5: Add the necessary imports at the top of common.py.**

Currently common.py imports `from backend import config_loader`,
`from backend.storage.trades import read_trade_ledger, write_trade_ledger`,
and `from backend.strategy.gann import compute_target_level_reached`.
Add:

```python
from backend.utils import now_ist
```

(`config_loader` is already imported. `BUY_LEVEL_ORDER`/`SELL_LEVEL_ORDER`
and `INDEX_OPTIONS_CONFIG` are imported lazily inside the helpers above
to keep the import graph clean.)

- [ ] **Step 21.6: Flesh out the deferred trail tests.**

Replace `pytest.skip(...)` in `test_trail_ratchets_up`,
`test_trail_does_not_lower`, `test_trail_fires_on_pullback`,
`test_trail_gated_by_in_hours` with real assertions. Use
monkeypatching to:
- Point trade-ledger storage at a temp file.
- Mock `_auto_in_hours` to return True for in-hours tests, False for the gating test.
- Feed synthetic `quotes_by_symbol` dicts keyed by the actual `spot_symbol_key` per `INDEX_OPTIONS_CONFIG` (verify at `backend/kotak/instruments.py` — e.g. NIFTY's spot_symbol_key is `"NIFTY 50"`, not `"NIFTY"`). Use that exact string as the dict key, with `ltp` and `levels` walking up/down the ladder.
- Insert a synthetic OPEN trade with `underlying: "NIFTY"`, `asset_type: "future"` (or `"option"` + `option_type: "CE"`/`"PE"`). The trade's `underlying` is the index name (`"NIFTY"`); the lookup helper translates it to the spot key.

Cover both bullish and bearish ratchet directions. Verify `trail_high_rung` is set to the highest rung crossed.

- [ ] **Step 21.7: Run trail tests.**

Run: `python -m pytest tests/test_strategy_trail.py -v`
Expected: all PASS.

- [ ] **Step 21.8: Run full suite.**

Run: `python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 21.9: Commit.**

```bash
git add backend/strategy/common.py tests/test_strategy_trail.py
git commit -m "feat(common): maintain trail_sl_price for variant D"
```

### Task 22: A/B/C regression fixture

**Files:**
- Modify: `tests/test_strategy_trail.py`

- [ ] **Step 22.1: Implement `test_abc_variants_unchanged`.**

Build a minimal fixture: a stored config with stoploss.active in {A, B, C}, an OPEN trade dict, and synthetic spot/ltp values. Call `_check_exit_reason` and assert the returned reason is exactly what the pre-Phase-3 code would have returned (manually trace the existing variants A/B/C to derive expected values).

- [ ] **Step 22.2: Run + commit.**

```bash
python -m pytest tests/test_strategy_trail.py -v
git add tests/test_strategy_trail.py
git commit -m "test(trail): regression fixture proves A/B/C unchanged"
```

### Task 23: UI — add D radio to /config page

**Files:**
- Modify: `frontend/templates/config.html`

- [ ] **Step 23.1: Locate the stoploss-active radio group** at `frontend/templates/config.html` lines 224, 252, 280. The existing radios use `name="stoploss.active"` (DOT, not underscore) — the dot-namespace convention is how the server-side parser keys the form. **Do NOT use `name="stoploss_active"`** — the value will silently fail to persist.

- [ ] **Step 23.2: Add a fourth radio.**

Read 30 lines around the existing variant-C `<label>` block (line 280 onward) to copy the exact wrapper structure and styling. The new radio should mirror that shape. Minimum:

```html
<label>
  <input type="radio" name="stoploss.active" value="D"
         {% if cfg.stoploss.active=='D' %}checked{% endif %}>
  <span style="flex:1">
    <span class="name">Variant D — Trailing along Gann ladder</span>
    <span class="desc">SL trails one rung BEHIND the spot's current rung.
      Initial SL = entry price (breakeven). Triggers on spot crossing
      trail SL; close fills at instrument LTP.</span>
  </span>
</label>
```

(Variant D has no per-variant level pickers — trail price comes from the live spot rung, not a configured level. So no `.pickers` block is needed.)

- [ ] **Step 23.3: Local manual check.**
- Open `/config`, confirm D is selectable.
- Save with D, reload — D persists.
- Save back to C — persists.

- [ ] **Step 23.4: Commit.**

```bash
git add frontend/templates/config.html
git commit -m "feat(config-ui): add D radio for trailing stoploss"
```

### Task 24: UI — show trail_sl_price on OPEN rows (nice-to-have)

**Files:**
- Modify: `frontend/templates/trade_ledger.html`
- Modify: `frontend/templates/paper_trades.html`

- [ ] **Step 24.1: Add a "Trail SL" column** that renders `t.trail_sl_price` for OPEN rows (and "—" for CLOSED rows). Only show the column when `cfg.stoploss.active == "D"` (Jinja conditional in the column header + each cell).

- [ ] **Step 24.2: Mirror the same change in paper_trades.html.**

- [ ] **Step 24.3: Commit.**

```bash
git add frontend/templates/trade_ledger.html frontend/templates/paper_trades.html
git commit -m "feat(ui): show Trail SL column when variant D active"
```

### Task 25: Phase 3 deploy checkpoint

- [ ] **Step 25.1: Push.**
```bash
git push
```

- [ ] **Step 25.2: VPS pull + restart.**
```bash
# On VPS:
cd /home/kotak/kotak-dashboard && git pull && sudo systemctl restart kotak
sudo systemctl is-active kotak
```

- [ ] **Step 25.3: In-hours validation (run during market hours).**
- Set `stoploss.active = "D"` via /config.
- Wait for an entry to fire.
- After ~2s tail `data/trade_ledger.json` and confirm the new row has
  `trail_sl_price` populated (= entry_price).
- Watch successive snapshots — `trail_sl_price` should ratchet as spot
  crosses higher Gann rungs.
- If spot retraces below `trail_sl_price`, exit reason should be
  `SL_TRAIL`.

- [ ] **Step 25.4: Out-of-hours validation.**
- After 15:15 IST (or on weekend), confirm `trail_sl_price` does NOT
  ratchet on stale ticks. Sample by reading a recent OPEN row before
  and after a few snapshot refreshes — value must be stable.

- [ ] **Step 25.5: Roll back instructions if anything is broken.**
Set `stoploss.active = "C"` via /config to disarm variant D
immediately. Then `git revert` the Phase 3 commits to fully back out.

---

## Final wrap-up

- [ ] **Step W.1: Update memory.**

Add an entry to
`C:\Users\matha\.claude\projects\C--Users-matha-temp\memory\MEMORY.md`
linking to a new
`C:\Users\matha\.claude\projects\C--Users-matha-temp\memory\project_kotak_paper_book.md`
that briefly documents:
- Paper book lives at `data/paper_ledger.json`.
- Driven from `_strategy_ticker_loop` alongside the live tick.
- Kill switch does not freeze paper.
- Trail SL variant D = one rung behind LTP's current rung; initial
  SL = entry price; triggers on spot, fills at instrument LTP.

- [ ] **Step W.2: Final smoke check on prod.**

```bash
curl -s http://localhost:5000/api/snapshot-stats
```
Expected: refresh_count steadily incrementing, errors all zero, all
three payloads present.

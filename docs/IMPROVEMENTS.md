# Improvements — A-to-Z Findings (Read-only, no fixes applied)

Generated: 2026-04-28. Single consolidated list combining four phases:

1. **Competitor research** → `docs/COMPETITOR_REVIEW.md`
2. **Code structure audit** → `docs/STRUCTURE_REVIEW.md`
3. **App inventory** → `docs/APP_INVENTORY.md`
4. **Live UI audit + perf test** → `docs/UI_AUDIT.md` (screenshots in `docs/ui_audit/`)
   and `docs/PERF_REPORT.md` (full numbers).

Status: nothing has been changed in the codebase. This is the prioritized
list. Apply only what you actually want — most items are independent.

---

## TL;DR — top 8 gaps in priority order

| # | Finding | Severity | Phase | Plain-English |
| - | ------- | -------- | ----- | ------------- |
| 1 | Trade Log lacks `entry_reason`, `trigger_lvl`, `trigger_spot`, `live_ltp`, `live_spot` columns that Paper Log already has — schema gap, not just UI | High | Inventory + UI | After a real trade you can't tell *why* it fired. Paper trade pages are strictly more informative than real trade pages. |
| 2 | Theme split: Gann/Options/Futures use **light theme**, every other page uses **dark theme** — same app, two visual languages | High | UI | Looks like two different products pasted together. |
| 3 | Broker passthrough pages (`/`, `/positions`, `/orders`, `/trade-book`, `/limits`) display raw Kotak JSON keys with no curation, no number formatting, no color | High | Inventory + UI | Columns and titles look like a debug panel, not a dashboard. |
| 4 | Slow base-html pages: `/trade-book` p95 694ms, `/limits` 487ms, `/orders` 427ms, `/` 570ms — every render hits Kotak REST | Medium | Perf | Those tabs feel sluggish vs. /gann (15ms), /options (16ms), /futures (16ms). |
| 5 | Global lock in `backend/strategy/options.py:308` and `futures.py:256` wraps entire tick body including REST round-trips — head-of-line blocking when Kotak is slow | Medium | Structure | One slow Kotak response stalls every other index in the same tick. |
| 6 | No global P&L summary in header on most pages (only `/gann` has the "Active 0 Closed 6 P&L +6.60" pill) | Medium | UI vs competitors | Every Indian broker (Kite, Dhan, Groww) keeps today's net P&L always visible. |
| 7 | WS sub churn: `[quote_feed] future subs changed (3 -> 3), resubscribing` every 2s causing socket disconnects (observed in app.log) | Medium | Structure | Comparing logically-identical lists as different keeps tearing the WS down. |
| 8 | Audit page Details column is free-form `key=value` text — no structured filtering by event-type or scrip | Low | UI | Forensics is harder than it should be. |

Detailed findings below. Each has a phase tag, screenshot reference where
relevant, and a competitor pattern cross-reference where one exists.

---

## A. Data model and schema

### A.1 Trade Log column gap (HIGH)
**Phase:** Inventory + UI — see `docs/ui_audit/10-trades.png` vs `11-paper-trades.png`
**Finding:** Real Trade Log records are missing `entry_reason`,
`instrument_token`, `exchange_segment`. Paper Book records have all of these.
The corresponding columns are also absent from the `/trades` template even
where the underlying data could be derived (e.g. `trigger_level`, `trigger_spot`).
**Why it matters:** Post-mortem on a live trade is strictly weaker than on a
paper trade. You can see *that* the bot bought NIFTY 23900 PE but not
*because of which level fired* without grepping `data/audit.log`.
**Cross-ref:** None of the surveyed Indian brokers ship this field either —
this is greenfield UX and should remain a Ganesh-specific edge. Don't drop
the columns from paper.

### A.2 Live ledger should write `entry_reason` going forward (HIGH)
**Phase:** Structure
**Finding:** `backend/strategy/options.py` and `futures.py` know the
`entry_reason` (path A market-open vs path B live-crossing, level name) at
entry time but only persist it for the paper book.
**Why it matters:** Without writing it now, no amount of UI work can
backfill historical real trades.
**Action (proposed, not applied):** Add `entry_reason` to the live ledger
record at entry; surface in `/trades`; show "—" for older rows.

### A.3 Paper-vs-Real column drift in UI (HIGH)
**Phase:** UI — `paper_trades.html` has 20 columns, `trade_ledger.html` has 13.
**Finding:** `Asset`, `Entry Reason`, `Trigger Lvl`, `Trigger Spot`,
`Live LTP`, `Live Spot` exist in paper UI only.
**Action:** Once A.2 is in place, mirror the columns. Both ledgers should
present identical schema so paper-mode calibration translates directly to
live confidence.

---

## B. Visual consistency

### B.1 Theme split: light vs dark (HIGH)
**Phase:** UI — `docs/ui_audit/07-gann.png` (light) vs `01-holdings.png` (dark)
**Finding:** `gann.html`, `options.html`, `futures.html` ship with a light
background (`#ffffff` / very pale), red SELL / green BUY heatmap. Every
other page (`base.html`, `paper_trades.html`, `blockers.html`, `audit.html`,
`config.html`, `trade_ledger.html`, etc.) is dark (`#0f1419`).
**Why it matters:** Switching tabs feels like switching products. Header
sizing, stat-pill placement, even the `Kotak Neo Dashboard` title vs
`Gann Trader • Live` title differ.
**Cross-ref:** Kite, Dhan, Groww all use a single theme per session and
provide a global theme toggle. Sensibull is light-only, Kite has both —
neither mixes.
**Action:** Pick one. Most likely dark, since the dense ledgers and audit
pages already are dark and they're the bulk of the surface.

### B.2 Header inconsistency (MEDIUM)
**Phase:** UI
**Finding:** Light pages put `Active: 0 Closed: 6 P&L: +6.60` + clock + UCC chip
in the header. Dark pages put only the LIVE/STOP/Refresh trio. Title styling
differs (`Gann Trader` vs `Kotak Neo Dashboard`).
**Action:** Move stats + clock + UCC into `_safety_header.html` so every
page gets them. Drop per-page H1 in favor of one global app shell.

### B.3 Tab order doesn't match user mental model (LOW)
**Phase:** UI
**Finding:** Tab order today: Gann, Options, Futures, Holdings, Positions,
Trade Log, Paper Log, Blockers, Config, Audit, Login History.
**Cross-ref:** Kite and Dhan group by activity (Watchlist | Orders |
Positions | Holdings | Funds | More). Audit and Login History are buried in
"More" menus.
**Action:** Group as Live (Gann/Options/Futures), Books (Trade Log/Paper Log),
Risk (Blockers/Audit/STOP), Account (Holdings/Positions/Limits/History/Config).

---

## C. Page density and broker passthrough

### C.1 Raw broker JSON dump on 5 pages (HIGH)
**Phase:** UI + Inventory — `docs/ui_audit/01-holdings.png`, `02-positions.png`,
`03-orders.png`, `04-trade-book.png`, `05-limits.png`
**Finding:** `base.html` auto-generates columns from whatever Kotak's API
returns. So column titles look like `nseScripCode`, `instrumentName`,
`avgPrice`, `realisedPnl` — broker field names, not user-facing.
**Cross-ref (Kite Positions):** Instrument | Qty | Avg | LTP | P&L | Day chg | M2M.
Six clean columns, day-chg color-coded, P&L grouped by underlying.
**Action proposed:** Replace `render(...)` with explicit per-page column
maps. Curate to ~6 columns per table, format numbers with thousand-separators
and 2dp, color-paint P&L, hide tokens.

### C.2 Position grouping by underlying missing (MEDIUM)
**Phase:** UI vs Kite
**Finding:** Kite groups all NIFTY F&O legs (across strikes and expiries)
under one "NIFTY" group with a combined P&L. We list every line item flat.
**Cross-ref:** [Kite position grouping](https://zerodha.com/z-connect/business-updates/introducing-position-grouping-and-filters-on-kite-web).
**Action:** Add an underlying-group toggle on `/positions` and `/trades`.

### C.3 Number formatting (MEDIUM)
**Phase:** UI
**Finding:** Holdings/Positions/Limits show raw floats: `12345.6789`. No
₹ prefix, no thousand separators, no fixed decimals.
**Action:** Helper `inr(x)` → `₹12,345.68`; apply across all broker pages.

---

## D. Performance

Anchor file: `docs/PERF_REPORT.md`. p95 numbers cited below are from the
warm scenario (n=20 sequential, local Flask).

### D.1 Slow base-html pages (MEDIUM)
**Finding:** Every render of `/`, `/positions`, `/orders`, `/trade-book`,
`/limits` calls Kotak REST inside the request. p95 latencies:
- `/trade-book` 694 ms, max 818 ms
- `/` (holdings) 570 ms
- `/limits` 487 ms
- `/orders` 427 ms
- `/positions` 235 ms

Compare with snapshot-backed pages (essentially free):
- `/gann` 23 ms p95
- `/options` 26 ms p95
- `/futures` 155 ms p95
- `/paper-trades` 31 ms p95

**Action proposed:** Either (a) extend SnapshotStore to cache holdings/
positions/orders/trade-book/limits at 10s producer cadence, or (b) cache
inside the page handler with a 5s TTL. Either drops p95 below 50 ms.

### D.2 `/api/future-prices` is the slow snapshot (LOW)
**Finding:** p95 175 ms. ~5x the option-prices and gann-prices endpoints
even though the payload is smaller (1 KB vs 89 B vs 3.6 KB).
**Hypothesis:** The producer probably rebuilds futures payload from a
non-cached path. Worth profiling `_snapshot.future_payload()`.

### D.3 `/api/recent-blocks` heavy tail (LOW)
**Finding:** p50 20 ms but p95 180 ms. This polls every 3s on every page
(toaster). Tail spikes correlate with file-read on
`data/blocked_attempts.jsonl`.
**Action:** Memoize last-N read in process; invalidate on append from
`backend/storage/blocked.py`.

### D.4 WS sub churn (MEDIUM)
**Phase:** Structure (observed in app.log) + Live console
**Finding:** `[quote_feed] future subs changed (3 -> 3), resubscribing`
prints every 2 seconds. The producer recomputes the future-subs list each
tick; even when logically equal, ordering or list-vs-tuple differences make
`new == self._future_subs` fail in `set_future_subs` at
`backend/kotak/quote_feed.py`.
**Symptom:** Periodic `[quote_feed] socket closed` and reconnect cycles
seen in `data/app.log`.
**Action proposed:** Compare as `frozenset` (or sorted tuple) before
deciding to resubscribe.

### D.5 Global lock head-of-line block (HIGH for live)
**Phase:** Structure — `backend/strategy/options.py:308`, `futures.py:256`
**Finding:** Tick body wraps `client.limits`, `client.positions`,
`client.place_order` plus ledger fsync inside a single global lock.
**Symptom:** During market hours, one slow Kotak round-trip stalls every
index for the same tick *and* the next tick.
**Action proposed:** Move REST round-trips outside the lock; lock only
the ledger read-modify-write.

### D.6 Read-modify-write race on trade ledger (MEDIUM)
**Phase:** Structure
**Finding:** `read_trade_ledger()` then later `write_trade_ledger()` in
options.py:309 / 532 / 665. Manual `/api/place-order` path can interleave.
**Risk:** Duplicate trade-id under concurrent load.
**Action:** Single `update_trade_ledger(fn)` that holds the write lock
across read + mutate + write.

### D.7 Snapshot vs ticker REST duplication (LOW)
**Finding:** Both the SnapshotStore producer and the strategy ticker call
`fetch_quotes` / option chain at overlapping intervals. ~30% duplicated
work.
**Action:** Have the ticker subscribe to the snapshot rather than re-fetching.

---

## E. Live updates and freshness

### E.1 WS freshness off-hours misleading (LOW)
**Finding:** During off-hours probe (10s window), `last_tick_age` grows
linearly from 3s to 12s — meaning no broker ticks. UI shows `● live` but
the cache is stale.
**Cross-ref:** Kite shows `STREAMING` only when ticks are arriving; falls
back to `OFFLINE` after N seconds idle.
**Action:** Add a freshness pill `live (1s) | stale (12s) | offline` in
the safety header and grey-out P&L when stale.

### E.2 No degradation pattern when WS drops (MEDIUM)
**Phase:** Structure
**Finding:** When the WS reconnects (5–10s), pages keep painting last-known
LTP. No visible UI hint that prices are stale.
**Cross-ref:** Sensibull and Dhan badge each cell with a tiny "ws-age"
indicator if > 2s old.

---

## F. UX patterns from competitors we don't have

### F.1 No keyboard shortcuts (LOW)
**Cross-ref:** Kite Terminal Mode, Dhan Super Order both have `?` cheatsheet.
**Action:** A few quick wins: `g g` → Gann, `g o` → Options, `g f` →
Futures, `g s` → STOP, `?` → cheatsheet modal.

### F.2 No watchlist concept (MEDIUM)
**Finding:** SCRIPS list is hard-coded in config; user can't pin/unpin
indices for the day.
**Cross-ref:** Kite multi-watchlist, Dhan rearrangeable list. We don't
need rich, but a single "today's universe" with quick add/remove would
materially help.

### F.3 No inline Greeks in option chain (LOW)
**Cross-ref:** Angel One option chain shows Δ Θ Γ V per strike row inline.
**Finding:** Our `/options` table is just LTP / Chg %. Greeks are computable
from existing data plus IV.
**Action:** Add Δ at minimum next to LTP for the ATM±2 strikes.

### F.4 No dashboard-level filters on Audit / Blockers (LOW)
**Phase:** UI — `docs/ui_audit/13-audit.png`
**Finding:** Audit page has only date filter. No event-type chips, no
scrip filter, no source filter.
**Action:** Add `<select>`s for event-type and source above the table.

### F.5 No mobile responsive layout (LOW unless mobile use is real)
**Phase:** UI — viewport tested at 1440×900 only.
**Finding:** Dense tables (paper-trades 20 columns, gann 14 columns) will
not work on phone.
**Cross-ref:** Groww has a Normal/Expanded toggle on positions specifically
for this — single icon flips between sparse mobile-card density and full
table density.
**Action:** Worth doing only if Ganesh actually uses the dashboard from a
phone. Confirm before building.

---

## G. Empty / loading / error states

### G.1 Empty states are present and well-written (POSITIVE)
**Phase:** UI
**Finding:** "No paper trades yet. Once the strategy ticker fires, paper
rows will appear here in parallel with live trades." (paper-trades),
"No blocked attempts yet. (Live-updates every 3s.)" (blockers), "No audit
events yet." (audit).
**Verdict:** This is good — better than most retail dashboards. Don't
regress.

### G.2 Holdings empty state is generic (LOW)
**Finding:** "No records found." doesn't explain when the user should
expect rows. (Today's holdings show after T+2; off-hours are normal.)
**Action:** Match the paper/blockers tone — explain *why* it's empty.

### G.3 Login error state hidden after auto-login success (POSITIVE)
**Finding:** The green "Auto-login successful" banner on `/` is reassuring.
On error, the red banner is equally clear.

---

## H. Safety / risk UX

### H.1 Kill switch UI is solid (POSITIVE)
**Phase:** UI — `docs/ui_audit/16-stop.png`
**Finding:** Two-click confirm, banner showing current state, optional
reason field. This is comparable to or better than Kite's segment-level
Kill Switch flow.

### H.2 No drawdown-trigger auto-halt (MEDIUM)
**Cross-ref:** Kite's roadmap calls this out as a future Kill Switch
trigger.
**Action:** Optional — auto-engage kill switch if today's net P&L drops
below configurable threshold.

### H.3 Blockers page is the right idea but kind/source labels too terse (LOW)
**Phase:** UI — `docs/ui_audit/12-blockers.png`
**Finding:** `Source: auto_options` is visible but you can't tell *which*
NIFTY/BANKNIFTY/SENSEX engine and *which* expiry from the row.
**Action:** Add an expiry/index column or expand `kind` into
`auto_options_NIFTY_28APR`.

---

## I. Code health, tests, observability

(Pulled from `docs/STRUCTURE_REVIEW.md` — see that file for full evidence.)

### I.1 No unit tests for strategy logic (MEDIUM)
**Finding:** `tests/` has perf scripts but no unit tests on the entry/
exit decision tree in `backend/strategy/options.py`.
**Action:** Even three tests around path A vs path B vs same-tick block
would catch the next regression.

### I.2 Audit log never rotated (LOW, by design)
**Finding:** `backend/safety/audit.py` is intentionally never rotated.
After a few months on a live VPS this could exceed 100 MB.
**Action:** Document the manual archive procedure
(`mv audit.log audit.YYYY-MM.log`) inside the file's docstring (already
there) and surface a size hint in `/audit` header.

### I.3 Snapshot-stats endpoint underused (LOW)
**Finding:** `/api/snapshot-stats` exposes producer build-ms histogram
but is not surfaced in any page.
**Action:** Add a tiny diagnostics widget at the bottom of `/config` or a
new `/health` page.

---

## J. Quick wins (under 30 minutes each, if you want to start somewhere)

These are the items where the diff is small and the reward is visible:

1. Pick a single theme (delete light theme blocks from gann/options/futures
   inline `<style>`s). [B.1]
2. Move LIVE pill + clock + UCC + today's net P&L into
   `_safety_header.html`. [B.2]
3. Add `entry_reason` write to live ledger at the entry-emit site. [A.2]
4. Fix WS sub churn by sorting before equality check in
   `set_future_subs` / `set_option_subs`. [D.4]
5. Add `frozenset` comparison guard around all three sub-set methods. [D.4]
6. Curate columns on `/positions` to 6 hand-picked names. [C.1]
7. Format numbers with `inr()` helper. [C.3]
8. Add `last_tick_age` pill to safety header. [E.1]

The bigger items (snapshot caching for broker pages, lock decomposition,
schema migration, watchlist, theme unification) are real projects and
should be plan-mode'd before touching.

---

## Phase-by-phase artifact map

| Phase                    | Artifact                              |
| ------------------------ | ------------------------------------- |
| Competitor research      | `docs/COMPETITOR_REVIEW.md`           |
| Code structure audit     | `docs/STRUCTURE_REVIEW.md`            |
| App inventory            | `docs/APP_INVENTORY.md`               |
| Perf test (script)       | `tests/perf/run_perf.py`              |
| Perf test (numbers)      | `docs/PERF_REPORT.md`, `.json`        |
| UI audit (screenshots)   | `docs/ui_audit/01..16-*.png`          |
| **This consolidated list** | `docs/IMPROVEMENTS.md`              |

Nothing in the codebase has been modified during this audit.

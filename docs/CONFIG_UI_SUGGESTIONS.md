# /config UI — suggestions & open ideas

Captured during Phase 3d audit (2026-04-29). Page works correctly today
(31/31 round-trip + validation + hot-reload checks pass), so these are
quality-of-life improvements, not bugs.

Ordered roughly by impact / cost ratio.

---

## P1 — High value, low effort

### 1. Halt-status banner at top
If `data/HALTED.flag` or any `data/HALTED_<engine>.flag` is set, show a
red strip at the top of /config with reason + timestamp. Currently the
operator can accidentally tweak settings while a halt is active and
forget that NEW entries are blocked.

### 2. "Unsaved changes" indicator
Track field-dirty state; show "● Unsaved changes" next to the Save
button when anything differs from the last-loaded config. Prevents
"did I save?" anxiety. Browser `beforeunload` warning when dirty.

### 3. Inline validation errors
Validator errors currently surface as a flat text blob at the bottom.
Better: highlight the offending field(s) in red and show the error
message under the input. Map validator paths
(`reverse_engine.lots.NIFTY`) → DOM elements.

### 4. "Reverse engine is OFF" overlay
When `reverse_logic.enabled=false`, dim the entire R2-R7 card stack
with a "Reverse engine is currently OFF — toggle in Logic Engines card
to activate" overlay. Today the cards look identical whether the
engine is live or disabled.

### 5. Per-day-cap shows today's count
Next to each cap input show `(2 / 5 used today)` so the operator can
see how close to the cap they are without leaving /config.

### 6. Drawdown live P&L badge
On the Drawdown card (R7), show today's combined P&L (current and
reverse separately). Color-code: green when positive, yellow approaching
threshold, red breached. Re-uses `_today_pnl_by_engine_cached`.

---

## P2 — High value, medium effort

### 7. Tabs for current vs reverse
Page currently scrolls forever — 6 cards × 2 engines = 12 cards.
Replace with two tabs at top: `[Current Engine]` `[Reverse Engine]`.
The Logic Engines + Apply To + Trading Window + per-index + drawdown
cards stay shared above the tabs. Reduces scroll, makes engine
identity unambiguous.

### 8. "Copy from current → reverse" button
On each R-card, a button that pulls the corresponding current-engine
value into the reverse fields. Useful when the operator wants the
reverse engine to mirror current as a starting point. With a
confirmation prompt to prevent accidental overwrites.

### 9. Side-by-side compare view
Toggle between "Stacked" (today) and "Compare" (two-column: current
left, reverse right). Compare mode exposes drift between engines at a
glance — important when the whole point is to A/B-test reverse vs
current.

### 10. Field tooltips
Hover help on every Gann level pick (BUY / BUY_WA / T1-T5 / SELL /
SELL_WA / S1-S5) and SL variant (A/B/C/D) explaining what they mean.
Ganesh isn't reading the source; the UI must self-explain.

### 11. Trading-window as time pickers
`market_start` and `square_off` are currently free-text. Use
`<input type="time">` with `step=60` and validate format on blur.

### 12. Confirmation dialog for high-risk toggles
When enabling `reverse_logic.enabled` (first time today) or switching
`stoploss.active` (any engine), show a confirm dialog: "This will
change live behavior. Continue?". Same ceremony idea as the STOP
button.

---

## P3 — Quality polish

### 13. Reset-to-defaults per card
A small "Reset" button per card that pulls the engine's defaults from
DEFAULTS in `config_loader.py`. Useful for "I broke this card, get me
back to known-good".

### 14. Diff view since last save
When dirty, show a "Show changes" link that diffs current form values
against the loaded config. Operators want to review before they Save.

### 15. JSON export / import
Buttons to download current config as JSON and upload to restore.
Lets Ganesh keep a known-good snapshot before experimenting.

### 16. Color-coded engine badge consistency
Reverse Engine divider is orange (`#fed7aa` border). Apply the same
orange consistently:
- `<span>(reverse engine)</span>` suffix on R-card titles
- Engine column in /trades, /paper-trades, /blockers
- Audit-log engine badges
- /config "Reverse" tab when introduced

Current engine = blue (`#2563eb` already used for primary actions).

### 17. Keyboard shortcuts
- `Ctrl+S` → Save (preventDefault on browser save)
- `Esc` → Cancel/revert dirty fields
- `?` → Show shortcut help

### 18. Loading state on Save
Disable Save button + spinner while POST is in flight. Today, rapid
double-clicks could trigger duplicate POSTs.

### 19. Mobile responsive layout
`.row { grid-template-columns: 200px 1fr }` breaks on phones. Switch
to single-column stacked layout below 768px (matches F.5 table fix).

### 20. Currency formatting
Show `₹1,500` in inputs for drawdown and `variant_a_drop_rs` (with
comma thousand-separators). On focus, switch to plain number for
editing.

---

## P4 — Nice-to-haves

### 21. Audit-log link from each card
"Last changed: 2026-04-29 14:32 by web (3 fields changed) — view
audit". Pulls from audit.log entries with `event=CONFIG_SAVE`.

### 22. Schema version pin
Show config schema version somewhere (e.g. footer). When config_loader
introduces a new field, current saved configs auto-upgrade — but a
visible version pin lets the operator sanity-check after upgrades.

### 23. "Test fire" button (paper only)
A button on each engine that simulates a synthetic crossing and shows
what the entry signal + SL trail would compute right now. Pure
read-only — never writes a row. Useful for "is my config actually
going to trade?".

### 24. Live preview of next entry signal
Tiny status box per index showing what the engine would do on the
next tick: `NIFTY: spot=24987 ↑BUY=24950 — would fire CE entry`.
Updates every snapshot tick. Great for sanity-checking before
enabling reverse_logic.

---

## Test coverage to add (later)

- Selenium / Playwright test for full /config form round-trip
- Test that disabling reverse_logic stops new reverse rows from
  appearing in the ledger within one tick
- Test that flipping `stoploss.active` from C to D for current engine
  does NOT change reverse engine's variant
- Test that POSTing a partial config envelope (without
  `reverse_engine`) preserves existing reverse_engine values

---

## Out of scope (already working well — don't break)

- Apply-to radio (paper / real / both) — clean three-button layout
- Logic Engines card — toggle + apply-to pills are clear
- Per-index paper/real toggles in the indices grid — fine as is
- Stoploss variant radio — A/B/C/D layout is clear
- Save bar sticky at the bottom — works

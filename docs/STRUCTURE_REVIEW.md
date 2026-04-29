# Kotak Auto-Login Dashboard — Structure / App-Flow Review

Generated: 2026-04-28. Audit scope: every route in `app.py` and the call
graph into `backend/`. Read-only — no code changes were made. All file:line
citations are repo-relative against the current tree.

---

## Threading model (ASCII)

```
                                ┌──────────────────────────────────────┐
                                │             Flask main thread        │
                                │   app.run(threaded=True), port 5000  │
                                └──────────────┬───────────────────────┘
                                               │ spawns N worker threads per request
                                               │
            ┌──────────────────────────────────┼──────────────────────────────────┐
            │                                  │                                  │
   /api/option-prices                 /api/paper-trades-live                 /trades, /orderlog,
   /api/gann-prices       ── O(1) ──> reads _feed + _option_quote_cache       /audit, /blockers...
   /api/future-prices       blob       + read_paper_ledger() (full file)     (file-read each req)
   (snapshot bytes)                                                          + Excel rendering
                                                                              on request thread
            │
            │   On boot (app.py:1118, 1121):
            ▼
  ┌────────────────────────────┐    ┌────────────────────────────────────┐
  │  strategy-ticker thread    │    │  SnapshotStore thread              │
  │  app.py:1017               │    │  backend/snapshot.py:324           │
  │  every 3s while in-hours:  │    │  every 2s, sequential:             │
  │   • fetch_option_quotes()  │    │   • _build_options_payload()       │
  │   • fetch_quotes()         │    │   • _build_gann_payload()          │
  │   • option_auto_strategy_  │    │   • _build_futures_payload()       │
  │     tick(...)              │    │  Each calls fetch_*_quotes() too   │
  │   • paper_options_tick()   │    │  -> overlapping REST work w/ ticker│
  │   • fetch_future_quotes()  │    │  payload protected by self._lock   │
  │   • future_auto_strategy_  │    └────────────────────────────────────┘
  │     tick()                 │
  │   • paper_futures_tick()   │
  └────────────────────────────┘
            │                           ┌────────────────────────────────┐
            │                           │  QuoteFeed (Kotak WS)          │
            └──── reads via _feed ──>   │  backend/kotak/quote_feed.py   │
                                        │  • SDK runs WS in its own      │
                                        │    daemon thread (run_forever) │
                                        │  • Our wrapper thread loops    │
                                        │    every 2s to detect option-  │
                                        │    sub deltas (ATM drift)      │
                                        │  • Single self._lock around    │
                                        │    every read AND every tick   │
                                        │    (one cache, ~50-200 keys)   │
                                        └────────────────────────────────┘

  Plus on-demand:
   • _preload_option_universe warm thread (app.py:353-358) — spawned the
     first time /api/option-prices is hit before the F&O universe is warm.
     Daemon, one-shot, dies after preload finishes.

  Locks in flight:
   • _feed_started["lock"]               (one-shot init guard)
   • _option_auto_state["lock"]          (entire option tick body — global)
   • _future_auto_state["lock"]          (entire futures tick body)
   • _paper_state["options_lock"]        (paper options tick)
   • _paper_state["futures_lock"]        (paper futures tick)
   • SnapshotStore._lock                 (3 payload tuples)
   • QuoteFeed._lock                     (cache + status + errors list)
   • QuoteFeed._subs_lock                (4 sub-list slots)
   • file_lock(<path>) per ledger file   (read-modify-write serialisation)
   • _stats_lock (kotak api stats)
   • RateLimiter._lock / CircuitBreaker._lock
```

Thread total in steady state: Flask main + Flask worker pool (per
request, ≤ default ~16) + ticker + SnapshotStore + QuoteFeed wrapper +
SDK's WS run_forever thread = roughly 5 long-lived background threads
plus N transient request threads.

---

## Strategy tick anatomy (where the 3s goes)

Each tick of `_strategy_ticker_loop` (app.py:1017) does roughly:

| step | work | typical cost | notes |
|------|------|--------------|-------|
| 1 | `fetch_option_quotes()` (quotes.py:304) | cache hit < 1 ms; cache miss = 2 × `client.quotes` REST + `build_all_option_tokens()` over 3 indices × 11 strikes × 2 sides | ATM drift triggers `_feed.set_option_subs` (cheap) |
| 2 | `fetch_quotes()` (quotes.py:106) | cache hit < 1 ms; miss = 2 × `client.quotes` for 9 SCRIPS | called every tick — `_quote_cache` TTL=2s so most ticks hit cache |
| 3 | `ensure_client()` | ~0 ms after first login (cached) | |
| 4 | `option_auto_strategy_tick` (options.py:289) | dominated by `read_trade_ledger()` (full JSON parse) → reads twice (line 309 then again at 532/665) → `_fetch_available_cash` calls `client.limits` REST per entry/exit | for each entry/exit: extra `safe_call(client.positions)` via `verify_open_position`; that's a 2nd REST call per exit; on healthy days this whole tick is sub-50 ms with no signals, but every entry/exit blocks the loop on 2-3 REST round-trips (~200-500 ms each) |
| 5 | `paper_options_tick` (paper_book.py:102) | reads paper ledger twice (line 124 + 91 in `_paper_execute_exit` + 74 in entry); ledger writes are full-file fsync rewrites | |
| 6 | `fetch_future_quotes()` (quotes.py:431) | as for option quotes; cache hit < 1 ms | only fired if `real_futures_enabled() OR paper_futures_enabled()` |
| 7 | `future_auto_strategy_tick` (futures.py:239) | mirror of step 4 with same costs | |
| 8 | `paper_futures_tick` (paper_book.py:288) | mirror of step 5 | |

In a quiet tick (no entries, no exits, all caches warm) the whole loop
body should complete in single-digit milliseconds. The 3s sleep at
app.py:1077 is by far the biggest term.

When something fires (new entry or exit) the dominant cost becomes
**REST round-trips**: limits + positions + place_order. Three calls × 3
indices × (entry|exit) is plausible — and they are serialised under the
`_option_auto_state["lock"]` so one slow Kotak response delays every
other index too.

The SnapshotStore producer (snapshot.py:324) runs independently every
2s and re-calls `fetch_quotes()` / `fetch_option_quotes()` /
`fetch_future_quotes()` itself. Because the TTL caches are shared, it
piggy-backs on whatever the strategy ticker just fetched — but the
windows are 3s vs 2s, so on most cycles BOTH threads will perform the
REST refresh. See finding F-1.

---

## Findings

Severity legend: **High** = correctness or live-money risk; **Med** =
clear performance / latency win; **Low** = polish / future-proofing.

1. **[Med] Snapshot producer + strategy ticker race the same TTL caches**
   `backend/snapshot.py:331-333` and `app.py:1027-1062`. SnapshotStore
   refreshes every 2 s; the strategy ticker every 3 s. The shared
   2 s TTL on `_quote_cache` / `_option_quote_cache` /
   `_future_quote_cache` (quotes.py:28) means roughly ~33% of ticks see
   one thread expire the cache and trigger a full REST pair while the
   other was about to read it. Two `client.quotes(...)` round-trips
   instead of one. Fix: bump the TTL to 5s and have the producer thread
   own the refresh; the strategy ticker should call a `read_only` variant
   that never refreshes.

2. **[Med] `_fetch_available_cash` calls `client.limits` on every entry AND every exit**
   `backend/strategy/options.py:511, 642`, `backend/strategy/futures.py:420, 565`.
   Each fired signal eats one extra REST round-trip just to populate the
   margin pre-check. Margin barely changes within a 3s tick. Fix: cache
   the limits response on a 30s TTL inside `_fetch_available_cash`, or
   move it onto the SnapshotStore producer and read from there.

3. **[Med] `verify_open_position` calls `client.positions` on every LIVE exit**
   `backend/safety/positions.py:49`, called from
   `backend/strategy/options.py:614` and `backend/strategy/futures.py:525`.
   Same critique as F-2: positions endpoint can be cached for a few
   seconds, since the exit gate is a soft "did Kotak still see this
   open?" check. Without caching, an SL_TRAIL exit on three indices in
   the same tick is three serial positions calls under the global
   strategy lock.

4. **[Med] `read_trade_ledger()` parsed twice per option tick**
   `backend/strategy/options.py:309` (header), then again at line 532
   inside `_execute_entry` and 665 inside `_execute_exit`. Same in
   futures.py:257 vs 440/588. The second read is intentional (avoid
   clobbering concurrent writes) but the file is fully reparsed each
   time. With dozens of trades the cost is small; with thousands it
   isn't, and it grows monotonically. Fix: keep an `mtime`-keyed cache
   in `backend/storage/trades.py` parallel to `config_loader._cache`.

5. **[Med] `read_paper_ledger()` re-parsed per tick AND per LTP poll**
   `backend/strategy/paper_book.py:124, 91, 303`. The `/api/paper-trades-live`
   endpoint at `app.py:470` ALSO parses it on every poll. Browser polls
   this every couple of seconds — at 100 paper rows this is fine; at
   10 000 it's not. Same fix as F-4: mtime cache.

6. **[Med] `read_blocked_page` reads the entire JSONL on every page load**
   `backend/storage/blocked.py:152-170`. For a quiet system this is
   fine; for an event-storm day where the file grows to MB-sized, the
   page poller (`/api/blocked-list`) and the page itself will both
   re-read and re-parse the whole file every time. Fix: tail-then-page
   strategy or keep an in-memory deque of last N records.

7. **[Med] `read_audit_page` has the same shape**
   `backend/safety/audit.py:98-114`. Audit log "is NEVER rotated" by
   policy (audit.py:14-17), so this is a slowly-growing file that the
   `/audit` page reads in full on each request. Symptom is identical to
   F-6.

8. **[Med] Excel exports stream the whole ledger from disk on the request thread**
   `app.py:556-600` (paper) and `app.py:626-672` (live). For each row we
   do `wb.append`, set 3-4 styles, and finally `wb.save` to a temp
   file, then `open(out, "rb").read()`. Two file I/Os and an unbounded
   loop on the request thread. Fix: build the workbook in-memory with
   `BytesIO`, skip the temp file, and gate behind a "since=YYYY-MM-DD"
   query so default export is just today's rows.

9. **[Low] `update_open_trades_mfe` writes both ledgers on every snapshot rebuild**
   `backend/strategy/common.py:242-248`, called from
   `backend/snapshot.py:138`. If `_apply_mfe_and_trail` returns True
   (variant D bumps trail almost every tick), that's a full
   `atomic_write_json` + `os.fsync` of `trade_ledger.json` AND
   `paper_ledger.json` every 2s. The fsync is the costly part. Fix:
   keep an in-memory MFE state and persist on a slower cadence (e.g.
   coalesce writes every ~10s, OR only flush on status transitions).

10. **[Med] `atomic_write_json` does `os.fsync` on every write**
    `backend/storage/_safe_io.py:53`. Combined with F-9, every
    successful trail-bump is a hard fsync — on a Contabo VPS with a
    spinning-disk-emulated SSD that's a 5-15 ms flush each time. Fix:
    leave fsync only on terminal state changes (entry, exit) and skip
    it for MFE/trail updates.

11. **[High] Strategy tick holds a single global lock around all I/O**
    `backend/strategy/options.py:308` (`with _option_auto_state["lock"]:`)
    wraps the whole body — including REST calls (limits, positions,
    place_order) and disk writes. Same in futures.py:256 and
    paper_book.py:123/302. If a Kotak REST call hangs (their API does
    intermittently), the next tick of the ticker thread is also stuck
    waiting on this lock — including the ticker calling
    `option_auto_strategy_tick` for the OTHER indices. Fix: hold the
    lock only around the read-modify-write of in-memory state and the
    ledger I/O; release before REST calls. Smaller-grained per-index
    locks would also break the head-of-line blocking.

12. **[Med] `read_paper_ledger` in `/api/paper-trades-live` parses full file per poll**
    `app.py:470` calls `read_paper_ledger()` on every browser poll —
    no TTL, no in-memory cache. Same disease as F-5. Pollers default to
    something like 1-2 s in the templates (didn't read; would confirm
    via grep).

13. **[Med] `compute_stats(trades)` runs on the request thread for every `/trades` page**
    `app.py:413, 427`. Iterates the entire ledger 4 times. Fine today;
    consider precomputing on the SnapshotStore producer alongside
    `_build_gann_payload` (which already calls `compute_stats` at
    snapshot.py:151 — a duplicate computation). Fix: cache stats on the
    snapshot and read it for the page too.

14. **[High] `option_auto_strategy_tick` reads same trade ledger twice when entering/exiting**
    Already noted in F-4 — but the behaviour also permits a TOCTOU race:
    between the `read_trade_ledger()` at line 309 (gate) and the second
    `read_trade_ledger()` at line 532/665 (write), another path
    (manual ticket via `/api/place-order` writing through
    `place_order_safe`) could write a row. The current code's "look up
    by id" at line 666 patches the exit case, but the entry path at
    line 562 always `insert(0, row)` — duplicate id risk if a manual
    order also generated `next_trade_id` from a stale read. Fix: take
    a single read lock around the read-decide-write block, or make
    `next_trade_id` UUID-based.

15. **[Low] `_compute_entry_signal` recomputes 4 levels every tick per index**
    `backend/strategy/options.py:259-285`. They're cheap, but the same
    4 calls happen again later at line 359-364. Tiny win; just hoist
    the computation. Same in futures.py.

16. **[Low] `config_loader.get()` is called many times per tick**
    `backend/strategy/options.py:131, 351`, options.py inside
    `_check_exit_reason` AND inside the per-index loop, plus every
    `config_loader.lot_multiplier`/`per_day_cap`/etc. helper. Each call
    takes the lock and does a `getmtime` syscall (config_loader.py:339).
    A few hundred stat() calls per tick. Fix: take one snapshot of cfg
    at the top of the tick body, pass it down.

17. **[Low] Errors list in QuoteFeed bounded to 50 (correct)** —
    `backend/kotak/quote_feed.py:148`. Confirmed: trimmed every append.
    Memo lists in `_stats` (kotak/api.py:94) are NOT bounded — but
    they're keyed by method name (a small enum), so growth is bounded
    by the number of distinct method names. OK.

18. **[Med] QuoteFeed cache never expires entries**
    `backend/kotak/quote_feed.py:52, 161`. Whenever option subs change
    (ATM drift) we add new keys but never delete old ones. Across a
    long-running session the cache grows by ~22 keys × number of ATM
    shifts per day × number of trading days. Fix: prune entries whose
    `ts` is older than ~1 hour from the cache, or maintain a working
    set per index.

19. **[Med] `_paper_execute_exit` writes full ledger even when nothing changed**
    `backend/strategy/paper_book.py:91-98`. The `for` loop will always
    `break` on first match; the surrounding `write_paper_ledger` runs
    unconditionally. If the row id doesn't match (stale cached row),
    we fsync-rewrite the entire file with the same content. Fix: only
    write if the loop actually mutated a row.

20. **[Low] `holdings_view` / `positions_view` etc. surface broker errors as 500-level pages**
    `app.py:156-205`. They handle the exception by rendering an error
    template (good), but the entire page chrome falls back to whatever
    `render_template("base.html", ...)` does with `view_error`. Better:
    return a 200 with the cached previous data plus a yellow banner.
    Today, a flaky Kotak `holdings()` call shows a wall of traceback
    text in the page body.

21. **[Med] `/api/place-order` calls `client.limits` synchronously on the request thread**
    `app.py:853`. That's a REST round-trip after the user has already
    submitted the form — adding latency to a fast-feeling UI action.
    Fix: read available cash from the SnapshotStore (where it could be
    cached on a 30s tick), or gate the margin pre-check behind a
    feature flag the user toggles per-session.

22. **[Med] `_preload_option_universe` warm thread spawned from request handler**
    `app.py:353-358`. Idempotent guard checks `_option_universe.get("loading")`,
    but the read isn't atomic — a race between two simultaneous
    `/api/option-prices` requests could spawn two warm threads. Each
    issues 3 sequential `search_scrip` calls. Fix: use a real lock
    (like `_feed_started["lock"]` does), or precompute on app startup.

23. **[Low] `read_recent_blocked(50)` called by toaster poll**
    `backend/storage/blocked.py:188` reads the entire file just to
    return last 50. Tiny today, eventually F-6.

24. **[Med] `_build_gann_payload` reads the trade ledger every 2s for stats**
    `backend/snapshot.py:151`. Combined with `_build_options_payload` /
    `_build_futures_payload` each calling `read_trade_ledger()` too
    (snapshot.py:104, 219), the producer reads the SAME file 3 times
    per producer cycle. Fix: read once per cycle and pass the list
    into each builder.

25. **[Low] `update_open_trades_mfe` swallows exceptions silently in some paths**
    `backend/snapshot.py:138-140`: `except Exception: pass`. A bug here
    silently disarms variant-D trail-SL maintenance. Fix: log the
    exception (the inner code at common.py:228 already does, but the
    outer wrapper hides anything earlier).

26. **[Low] `dashboard` heading on Excel templates contains hard-coded col widths**
    `app.py:585-587, 658-660` — fixed array of 13 widths inlined twice.
    Cosmetic — note for future refactor.

27. **[Low] On-the-fly Excel export writes to `data/_paper_ledger_export.xlsx`**
    `app.py:590-593`. Two parallel browsers can collide on this temp
    file. Fix: use `tempfile.NamedTemporaryFile` or an in-memory
    `BytesIO` (also fixes F-8).

---

## Hot-path call graph for the busiest endpoint (`/api/option-prices`)

```
GET /api/option-prices                              app.py:338
  │
  ├─ if F&O universe NOT warm:                      app.py:349-358
  │    threading.Thread(target=_warm).start()
  │       └── _preload_option_universe()            app.py:1090
  │             └── _fetch_index_fo_universe(idx)   instruments.py:81
  │                   ├── ensure_client()
  │                   └── client.search_scrip(...)  -> Kotak REST
  │
  └─ blob, built_at, build_ms = _snapshot.options_payload()   snapshot.py:273
        └── self._lock.acquire(); return self._payloads["options"]
        ── O(1) read of bytes, ~microseconds
        Returns Response(blob, mimetype="application/json")
        plus X-Snapshot-Age-Ms / X-Snapshot-Build-Ms headers.

Producer side (background thread, snapshot.py:324):
  every 2s:
    _build_options_payload()                        snapshot.py:50
      ├── from backend.quotes import fetch_option_quotes
      ├── fetch_option_quotes()                     quotes.py:304
      │     ├── cache hit -> overlay WS LTPs and return
      │     └── miss:
      │           ├── build_all_option_tokens()     quotes.py:273
      │           │     └── for idx in 3 indices:
      │           │           build_option_chain()  quotes.py:213
      │           │             ├── fetch_quotes()  quotes.py:106 (recursion-ish: spot)
      │           │             ├── _fetch_index_fo_universe (cached per day)
      │           │             └── _parse_item_strike / _parse_item_expiry_date
      │           ├── client.quotes(qt="ohlc")      <- REST round-trip
      │           ├── client.quotes(qt="ltp")       <- REST round-trip
      │           └── _ws_overlay()                 quotes.py:68
      ├── read_trade_ledger()                       trades.py:45  (FULL file parse)
      └── json.dumps(payload, default=str).encode("utf-8")
```

Steady-state cost on a request: ~10-50 µs per HTTP GET (lock + read +
flask response). Producer cost on a refresh: ~50-200 ms when there's a
cache miss (two REST calls + JSON serde + file read). The hot-path is
healthy.

---

## Quick-win suggestions (top 5 low-risk fixes shippable today)

1. **Cache `client.limits` for ~30 s inside `_fetch_available_cash`**
   (F-2). Two-line change in both options.py and futures.py; reuses
   the existing pattern. Removes one REST round-trip per signal.

2. **Cache `client.positions` for ~5 s inside `verify_open_position`**
   (F-3). Same shape. Removes a REST hop on every variant-D exit
   storm.

3. **Skip fsync on MFE / trail-SL writes** (F-9, F-10). Add a
   `fsync=True/False` kwarg to `atomic_write_json` and pass `False`
   from `update_open_trades_mfe`. Entries / exits stay durable; trail
   bumps tolerate a crash window of one tick.

4. **Build Excel exports in `BytesIO`, drop the temp file** (F-8, F-27).
   Removes a race + a disk write from a request handler.

5. **Read trade ledger once per snapshot producer cycle** (F-24). Pass
   the list into the three builders. Eliminates 2× full file parses
   every 2 s.

---

## Bigger refactors (top 3 — needs design discussion)

1. **Lock granularity in `option_auto_strategy_tick` / `future_auto_strategy_tick`**
   (F-11, F-14). Today a single global lock wraps REST + disk + decide
   logic. Splitting into (a) decide phase under lock with snapshotted
   ledger, then (b) place order outside the lock, then (c) ledger
   write under file_lock would let three indices proceed in parallel
   when one Kotak call stalls — but it requires careful re-validation
   of the per-day cap / open-by-underlying invariants since they no
   longer hold trivially across the lock boundary. Worth an RFC.

2. **In-memory ledger model**
   (F-4, F-5, F-13, F-24). Today every write is a full `json.load → list
   ops → atomic_write_json → fsync` cycle. With paper + live engines
   running and the SnapshotStore reading the ledger for stats, the
   read-fan-out is high. A small ledger object (TradesStore singleton
   with mtime-aware cache + an `append()` / `update_in_place(id, ...)`
   API) would centralise this. Would also fix the duplicate-id race
   in F-14 cleanly. Migration risk: Ganesh reviews the ledger by
   reading the JSON file directly, so the on-disk format must stay
   identical.

3. **Snapshot vs. ticker — pick one producer**
   (F-1). Today both threads call `fetch_*_quotes()` and both can
   trigger the REST refresh. Either:
   (a) The SnapshotStore becomes the only caller of fetch_* and the
       strategy ticker reads from a `_quote_cache` (or from snapshot
       payload structured for strategy consumption).
   (b) The strategy ticker becomes the producer and writes the
       snapshot payloads itself. Either way the doubled REST calls go
       away, the off-by-one cache TTL race goes away, and we stop
       paying twice for the same data. Designing this well needs to
       account for the tick cadence (3 s strategy decisions vs 2 s UI
       freshness) — probably the snapshot stays at 2 s and the
       strategy reads from it on its own 3 s wake-up.

---

## Notes on review constraints

This audit reads the codebase only — no live data, no profiler. Cost
estimates are derived from inspection (API surface, file sizes, lock
shapes) and clearly-labelled assumptions; they should be confirmed
with a profiler (`cProfile` or `py-spy top`) before any of the bigger
refactors are scheduled.

The codebase is in good shape overall — careful separation of concerns
(quotes / snapshot / strategy / safety / storage), explicit thread
naming, atomic writes everywhere they matter, audit logging on every
order intent. The findings above are mostly performance polish; the
two High-severity items (F-11, F-14) are correctness/latency
boundaries worth landing soon.

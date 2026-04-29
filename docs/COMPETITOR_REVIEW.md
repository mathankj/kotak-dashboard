# Competitor Review: Indian Stock/Options Trading Apps

Research date: 2026-04-28. Time-boxed (~10 min). Focus: UX patterns and perf/architecture conventions
relevant to a Kotak Neo dashboard with Gann-options paper-trading. Priority deep-dives: Zerodha Kite,
Sensibull, Dhan, Groww. Lighter coverage on the rest. Sources cited inline; where information was not
found in the time budget, that gap is called out explicitly.

Note on method: WebFetch was disabled in this environment, so depth is limited to what WebSearch
summaries surface. Treat unverified specifics as "indicative" — the cited URLs are the place to
re-verify before copying any pattern.

---

## 1. Zerodha Kite (web + mobile) — the de facto reference

### UX patterns
- **Dashboard layout**: Marketwatch-first. Left rail of watchlists (multiple, named), center workspace
  with charts/order windows, top header with funds + account. Recently introduced "Kite Terminal Mode"
  which lets users freely arrange charts, watchlists, option chains, orders into a custom workspace
  rather than a fixed layout ([Kite Terminal Mode](https://zerodha.com/z-connect/business-updates/introducing-kite-terminal-mode)).
- **Positions table columns** (per support docs): Instrument, Qty, Avg, LTP, P&L, Day chg, M2M for F&O.
  Day's P&L = Current LTP − Previous day's close. Post-3:30 PM, P&L is recomputed against the day's
  settlement price, which is why values shift after close ([Kite positions P&L](https://support.zerodha.com/category/console/portfolio/holdings/articles/p-l-on-holdings-positions-page-on-kite-change-post-3-30-pm),
  [Kite User Manual marketwatch](https://kite.trade/docs/kite/marketwatch/)).
- **Position grouping & filters** (2024+ feature): Group by Underlying so e.g. all NIFTY F&O legs across
  expiries show under one group with a combined P&L beside the group header. Filters let you slice
  positions by product/segment ([Position grouping post](https://zerodha.com/z-connect/business-updates/introducing-position-grouping-and-filters-on-kite-web)).
- **Order page**: Clicking the instrument name now opens the order window inline — fewer modal hops
  ([Kite positions update](https://support.zerodha.com/category/trading-and-markets/kite-web-and-mobile/others/articles/kite-positions-update)).
- **Color system**: Standard green = up/buy/profit, red = down/sell/loss. This is the de facto Indian
  convention across all platforms surveyed ([color convention](https://rupeezy.in/blog/what-is-colour-trading)).
  Kite uses muted, low-saturation greens/reds — not neon — paired with neutral grey chrome.
- **Kill-switch / risk-halt UX**: Explicit "Kill Switch" segment-level toggle as part of the broader
  Nudge initiative. To activate, user must first exit all open positions and cancel pending orders;
  once activated, the segment is locked for **12 hours** with no override. Manual today; an automatic
  drawdown-triggered version is on their roadmap ([Kill Switch announcement](https://zerodha.com/z-connect/console/introducing-kill-switch),
  [Kill Switch FAQ](https://support.zerodha.com/category/console/segments/killswitch/articles/what-is-the-kill-switch)).
- **Empty/loading/error states, mobile patterns, typography**: Not surfaced in search snippets in the
  time budget — gap.

### Performance / architecture
- **WebSocket binary protocol**: Kite's WebSocket pushes binary frames (not JSON) for quotes; postbacks
  and non-quote updates are text. Three modes: LTP (smallest), Quote, Full. Up to **3000 instruments
  per connection**, max **3 connections per API key** ([Kite Connect WebSocket docs](https://kite.trade/docs/connect/v3/websocket/)).
- **Latency**: Average tick latency reported by Zerodha is **500–700 ms**, and the exchange itself only
  emits ~1–2 ticks/sec to retail feeds even though hundreds happen internally — so client-side
  smoothing/extrapolation is expected ([WebSocket streaming forum](https://kite.trade/forum/discussion/15511/websocket-streaming-performance)).
- **Rate limits**: 10 req/sec aggregate per API key; orders capped (numbers vary in docs: 10/sec & 400/min
  in some places, 5/sec & 200/min in others) ([API rate limits forum](https://kite.trade/forum/discussion/8577/api-rate-limits)).
- **Compression**: Not explicitly documented in the snippets surfaced — gap. The binary packet design
  itself is the compression strategy (typecast bytes → struct).
- **Background tab behavior, page-load metrics**: Not found in budget — gap.

### Inspiration
- **UX**: Position grouping by underlying with a combined P&L on the group header. For our Gann-options
  use case (multiple legs per underlying across strikes/expiries), this is exactly the right primitive.
- **Architecture**: Binary tick frames + a 3-mode subscription (LTP / Quote / Full). Maps cleanly to our
  hot-cache SnapshotStore — different consumers can subscribe at different fidelities without us
  serializing the full payload every tick.

---

## 2. Sensibull — options-first, the strategy benchmark

### UX patterns
- **Top nav**: Strategy Builder, Option Chain, Market Analysis, Portfolio. Linear flow: analyze →
  build → execute ([Sensibull guide 2025](https://www.teqmocharts.com/2025/07/how-to-use-sensibull-for-options.html)).
- **Option chain**: Strikes around ATM with Call data on the left, Put data on the right; ATM strike is
  visually highlighted. Per-strike: LTP, OI, OI Change, IV, Volume. Headline metrics displayed on the
  chain page itself: PCR, IV Percentile, Max Pain, India VIX ([Sensibull live charts](https://web.sensibull.com/live-options-charts?tradingsymbol=NIFTY),
  [Option chain tutorial](https://blog.sensibull.com/2018/07/09/option-chain-tutorial/)).
- **Strategy builder**: Multi-leg construction with one-click execution. Live payoff diagram + per-leg
  Greeks + breakeven and probability-of-profit ([Sensibull strategy builder](https://web.sensibull.com/option-strategy-builder)).
- **Mobile parity**: Mobile app explicitly designed to mirror web — same metrics, same layout idiom
  ([Play Store listing](https://play.google.com/store/apps/details?id=com.sensibull.mobile&hl=en_IN)).
- **Caveat surfaced in their own docs**: They warn that PCR / Max Pain / IV Percentile are weak signals
  unless backed by high OI and volume, and only meaningful after the first ~5 days of an expiry. This
  is unusually honest UX copy and worth emulating in tooltips.

### Performance / architecture
- Latency targets, WebSocket details, push/poll mix: not surfaced in search results — gap.
- Sensibull is broker-agnostic and embedded by Angel One and ICICI Direct as a partner widget
  ([Angel One Sensibull](https://www.angelone.in/sensibull), [ICICI Direct Sensibull](https://www.icicidirect.com/futures-and-options/products/sensibull)),
  so the platform is built to be embeddable — implies a clean iframe/SSO contract.

### Inspiration
- **UX**: Honest warning copy on weak signals (PCR/MaxPain/IVP) — directly applicable to Gann level
  confidence scoring. Show the level, but show the caveat.
- **Architecture**: The embeddable-widget posture (Angel/ICICI both host Sensibull inside their UIs)
  is a model for how our dashboard could be split into composable panels rather than one monolithic page.

---

## 3. Dhan — F&O-native, fastest-moving competitor

### UX patterns
- **Dedicated Options Trader app**, separate from the main Dhan app. >1M users. Tagline #MadeForTrade
  with a "Glass UI" design language ([Options Trader by Dhan](https://dhan.co/options-trader/),
  [Play Store](https://play.google.com/store/apps/details?id=com.dhanoptions.live&hl=en_US)).
- **Position Analyzer**: Dedicated panel that aggregates position-level Greeks, breakevens, and
  payoff for the whole book — not just per-leg.
- **Custom Strategy Builder** (mobile + web): Multi-leg across expiries, one-shot execution. Surfaces
  Max P/L, breakevens, **POP (Probability of Profit)**, risk/reward, live Greeks, margin required, and
  payoff curve all on the build screen.
- **Super Order** (their bracket-order rebrand): One order = entry + target + stop-loss, with optional
  trailing SL. Validity up to **365 days** for delivery/NRML/intraday/MTF. Entry leg goes to the
  exchange immediately; target/SL legs are stored on Dhan's servers and pushed to the exchange only
  when triggered. Target & SL operate on **OCO** (one-cancels-other) logic. All three legs are
  modifiable while pending ([Super Order folder](https://knowledge.dhan.co/support/solutions/folders/82000698241),
  [Super Order API docs](https://dhanhq.co/docs/v2/super-order/),
  [Bracket order help](https://dhan.co/support/orders-and-positions/order-types/what-is-bracket-order-how-to-place-bracket-order-on-dhan/)).
- **Option chain enhancements**: Strike-wise PCR (not just aggregate), SLBM data inline, total-P&L
  sum at the top of the positions tab.
- **Mobile-first**: Strategy builder is fully usable on phone, not a cut-down version.

### Performance / architecture
- The Super Order architecture is the interesting bit: SL/target legs live **server-side**, monitored
  in real-time, and only forwarded to the exchange on trigger. This is exactly what we're doing with
  our paper ledger's trailing-SL variant D — server-side decision, exchange-side fill.
- Specific latency numbers, WebSocket details: not surfaced — gap.

### Inspiration
- **UX**: Surface margin, POP, max P/L, and breakevens on the same screen as the strategy build —
  don't make the user click into a "preview." For a Gann setup with 2–3 legs this is critical.
- **Architecture**: Server-side OCO storage of SL+target legs with exchange-side fill on trigger. This
  is the canonical pattern; our paper book should mirror it 1:1 so the live cutover is mechanical.

---

## 4. Groww — mobile-first, retail UX leader

### UX patterns
- **Expanded Positions View**: A toggle (single icon tap) on the positions screen flips between
  "Normal" (sparse, card-like rows) and "Expanded" (dense, table-like) views. Same data, two
  densities, user choice per session ([Expanded positions view](https://groww.in/updates/expanded-positions-view-on-groww)).
  This directly answers the 8–12-column-on-mobile question: don't pick one; let the user toggle.
- **PnL on chart**: Position line + PnL label drawn directly on both the contract chart and the
  underlying chart. Tap the line to close the full position from the chart ([Groww Charts](https://groww.in/updates/groww-charts)).
- **F&O P&L methodology**: Average buy price stays constant after entry; P&L computed against original
  buy price rather than rolling settlement, "so you can clearly see total profit/loss at any point"
  ([F&O futures P&L update](https://groww.in/updates/equity-futures-pnl-update)). Useful clarity for
  paper-trade ledger semantics — match this convention.
- **915 (their pro terminal)**: Separate product for power users with a customizable PnL dashboard,
  straddle charts, live charts ([915 PnL dashboard](https://915.groww.in/tools/pnl-dashboard)). Two-tier
  product strategy: simple app for retail, 915 for active F&O traders.
- **Color/typography specifics**: Not surfaced in budget — gap.

### Performance / architecture
- **Groww Charts** is explicitly marketed as "natively built, mobile-first, high-performance" for F&O
  on phones — implying a custom canvas/native renderer rather than an off-the-shelf TradingView embed
  on mobile ([Groww Charts](https://groww.in/updates/groww-charts)).
- Specific latency/load-time numbers: not found — gap.

### Inspiration
- **UX**: The Normal/Expanded view toggle. Same data, two information densities, single tap. Best
  answer to the dense-table-on-mobile problem in this entire survey.
- **Architecture**: Two-tier product (Groww app vs 915 terminal) lets each surface optimize for its
  audience without compromise. Maps to our paper-vs-live, or beginner-dashboard-vs-trader-terminal split.

---

## 5. Upstox Pro — clean, fast, recently revamped

### UX patterns
- **Pro Web 3.0** (2025) and revamped option chain in Feb 2025: "cleaner, faster, easier to navigate."
  Per-strike data points: Spot LTP, Futures LTP, PCR, Max Pain, Lot Size, Days-to-Expiry — all
  visible without leaving the chain ([Upstox option chain refresh](https://upstox.com/market-talk/tools-to-power-your-trades-and-investments-february-2025/),
  [Pro Web 3.0](https://upstox.com/market-talk/introducing-pro-web-3-0/)).
- **Strategy Chain**: Preset strategy templates + Greeks + PCR + Max Pain + India VIX all on one
  surface. Multi-leg orders placed as a single action ([Chain feature](https://upstox.com/market-talk/chain-feature-upstox-options-traders/)).
- **Chart-integrated trading**: REVERSE icon on chart flips a position long↔short in one click;
  Instant Order places a market order directly from the TradingView chart.
- **20+ named watchlists**, HTML-based platform (no installer).

### Inspiration
- **UX**: Spot LTP + Futures LTP + days-to-expiry on the option chain itself. Saves a context switch.
- **Architecture**: HTML-only, no installer — everything they ship is web. Aligns with our browser-first stance.

---

## 6. Angel One — broad coverage, dense option chain

### UX patterns
- **SpeedPro** desktop terminal + Super App mobile/web ([SpeedPro](https://www.angelone.in/platform-and-tools/angel-speedpro)).
- **Option chain density**: Per strike — LTP, LTP change, OI, OI Change %, Price change, Volume, IV,
  **Delta, Theta, Vega, Gamma** — all in a single horizontal row. Calls and puts in one fold, scrollable
  vertically through strikes ([Angel One trading platform](https://www.angelone.in/trade-platform)).
- Direct buy/sell from the chain row for OTM/ITM strikes — no order modal hop.

### Inspiration
- **UX**: Greeks (Δ Θ Γ V) inline on the chain, not behind a click. For Gann options this is essential —
  the level fires, you need delta exposure visible in the same glance as the strike.

---

## 7. Fyers — heavy customization

### UX patterns
- **Customizable option chain**: Pick ±N strikes around ATM (up to 30 each side), filter by
  expiry/CE/PE, toggle hiding zero-volume and zero-OI strikes. Includes Δ Θ Γ Vega Rho per strike
  ([Fyers option chain customization](https://support.fyers.in/portal/en/kb/articles/how-do-i-customise-the-options-chain-view-in-fyers)).
- **Straddle view** as a built-in chain mode ([Fyers Web](https://fyers.in/web/options/option-chain)).
- **Layout exports**: Export current tab data (orders/positions/holdings/funds) from the dashboard menu.
- **Funds Available** pinned to dashboard chrome.
- **Chart-position toggle**: Show/hide positions overlay on chart per user pref.

### Inspiration
- **UX**: User-controlled strike range (±N) on the option chain. Don't hardcode "show 10 strikes" —
  let the trader pick. Critical when Gann levels can sit far from ATM.

---

## 8. ICICI Direct, 5paisa, Motilal Oswal — quick scan

Time budget did not allow deep dives. Summary from comparison reviews
([DematDive comparison](https://dematdive.com/mobile-trading-apps-2/),
[Investorgain Motilal](https://www.investorgain.com/trading-platform/groww/47/)):

- **ICICI Direct**: Conservative, research-heavy, multiple watchlists, integrated bank UX.
- **5paisa**: Minimalist, low-cost retail focus, has its own options strategy builder.
- **Motilal Oswal**: Multiple separate apps (MO Investor, MO Trader, MO Trader Web, MO Trader EXE) —
  segmentation by user type rather than feature toggles within one app. Heavy research content.

None surface a feature-set distinctive enough to copy that the four leaders above don't already cover better.

---

## 9. Opstra (Definedge) — analytics, not execution-first

### UX patterns
- **Strategy Builder, Backtester, Simulator, IV Charts, Volatility Surface, Volatility Skew, Strategy
  Charts** ([Opstra](https://opstra.definedge.com/options),
  [Opstra strategy builder](https://opstra.definedge.com/strategy-builder)).
- **EOD IV charts** to monitor implied vol over time per underlying.
- **Options Dashboard** screen: filter F&O stocks by criteria, scan the universe.
- **No specific Gann visualizer found** in search snippets — gap. Opstra's strength is statistical
  options analytics (vol surface, skew, backtest), not chart-overlay technical levels. If we want
  Gann-level rendering inspiration we won't find it here; we will need to build that primitive
  ourselves or look at TradingView/ChartIQ.

### Inspiration
- **UX**: Volatility skew and IV-percentile charts as first-class dashboard tiles. Even for a Gann
  strategy, IV regime should be visible — it changes which Gann level is tradeable as a credit spread
  vs a debit spread.

---

## Patterns worth stealing (synthesis)

A short, opinionated list ranked by ROI for our Kotak Neo + Gann + paper-trading dashboard.

### Top-tier (steal these now)

1. **Groww's Normal/Expanded view toggle on positions** — single icon, two densities. Solves the
   8–12-column mobile problem cleanly. Far better than horizontal scroll or row-expand.
2. **Kite's position grouping by underlying with a combined P&L header** — exactly right for multi-leg
   Gann setups across strikes/expiries.
3. **Dhan's Super Order architecture** — entry on exchange, SL+target stored server-side with OCO,
   pushed only on trigger. This is how our paper ledger's trailing-SL variant D should be modeled,
   so the eventual live cutover is mechanical. (We're already close per
   `project_kotak_paper_book` memory — formalize the OCO semantics.)
4. **Dhan's strategy-build surface showing margin + POP + max P/L + breakevens + Greeks on the build
   screen** — no preview-modal hop. For a 2–3-leg Gann options entry this is non-negotiable.
5. **Angel One's inline Greeks on the option chain row** — Δ Θ Γ Vega per strike, not behind a click.
6. **Sensibull's honest caveat copy** ("PCR/MaxPain only meaningful after first ~5 days, with high OI") —
   wrap our Gann level confidence with the same posture. Show the level, show the caveat.

### Second-tier (consider)

7. **Fyers' user-controlled ±N strike range** on the option chain.
8. **Upstox's spot LTP + futures LTP + days-to-expiry on the chain itself**.
9. **Kite Terminal Mode-style draggable workspace** — eventually, but not for v1.
10. **Groww's chart-overlay PnL line + tap-to-close** — applicable when we add chart view.
11. **Kite's Kill Switch with mandatory 12-hour lock-out** — for live trading phase, not paper.

### Architecture / perf

1. **Kite's binary tick protocol with 3 fidelity modes (LTP / Quote / Full)** — different consumers,
   different bandwidth. Maps to our hot-cache SnapshotStore: cache once at full fidelity, derive
   light projections per consumer.
2. **Kite's documented latency posture (500–700 ms tick, 10 req/sec)** — set our own SLAs in the same
   ballpark. We already beat this at the cache layer (1000–2000× per memory) but the user-perceived
   end-to-end is what counts.
3. **Dhan's server-side SL/target storage** — already in our roadmap; this confirms the pattern.
4. **Sensibull-style embeddability** — design our panels (option chain, position grid, Gann overlay)
   as composable widgets rather than tightly coupled to one page shell.

### Gaps in the survey (call out honestly)

- No app surveyed exposes a **Gann-level overlay** as a first-class feature. This is whitespace —
  we don't have direct copy-paste inspiration. Closest analogues are TradingView's drawing tools and
  Opstra's volatility surface, neither of which we can directly mimic.
- **Paper-trade vs live-trade column parity** was not directly addressed by any source in the time
  budget. Implication: this is also whitespace — no one in the Indian market is actively marketing a
  paper-trade UX, so we have no public reference. The right answer per Ganesh's memory is column
  parity by default — this competitor review supports that bet (no one disagrees because no one
  has thought about it).
- **Empty/loading/error states, page-load metrics, background-tab behavior, exact typography/font
  choices** were not surfaced for any app in the time budget. These would need direct app inspection
  (set up cookies, log in, screenshot) — out of scope here.

---

## Sources

- [Zerodha Kite product page](https://zerodha.com/products/kite/)
- [Kite Terminal Mode announcement](https://zerodha.com/z-connect/business-updates/introducing-kite-terminal-mode)
- [Kite position grouping & filters](https://zerodha.com/z-connect/business-updates/introducing-position-grouping-and-filters-on-kite-web)
- [Kite positions update (order window inline)](https://support.zerodha.com/category/trading-and-markets/kite-web-and-mobile/others/articles/kite-positions-update)
- [Kite positions P&L post 3:30 PM](https://support.zerodha.com/category/console/portfolio/holdings/articles/p-l-on-holdings-positions-page-on-kite-change-post-3-30-pm)
- [Kite User Manual marketwatch](https://kite.trade/docs/kite/marketwatch/)
- [Kite Connect WebSocket docs](https://kite.trade/docs/connect/v3/websocket/)
- [Kite WebSocket streaming performance forum](https://kite.trade/forum/discussion/15511/websocket-streaming-performance)
- [Kite API rate limits forum](https://kite.trade/forum/discussion/8577/api-rate-limits)
- [Zerodha Kill Switch announcement](https://zerodha.com/z-connect/console/introducing-kill-switch)
- [Zerodha Kill Switch FAQ](https://support.zerodha.com/category/console/segments/killswitch/articles/what-is-the-kill-switch)
- [Sensibull strategy builder](https://web.sensibull.com/option-strategy-builder)
- [Sensibull live options charts](https://web.sensibull.com/live-options-charts?tradingsymbol=NIFTY)
- [Sensibull option chain tutorial](https://blog.sensibull.com/2018/07/09/option-chain-tutorial/)
- [Sensibull strategy builder update post](https://blog.sensibull.com/2023/05/10/update-in-strategy-builder-option-chain/)
- [Sensibull beginner guide 2025](https://www.teqmocharts.com/2025/07/how-to-use-sensibull-for-options.html)
- [Angel One Sensibull integration](https://www.angelone.in/sensibull)
- [ICICI Direct Sensibull integration](https://www.icicidirect.com/futures-and-options/products/sensibull)
- [Dhan Options Trader product page](https://dhan.co/options-trader/)
- [Dhan Options Trader Web](https://dhan.co/options-trader-web/)
- [Dhan trading features](https://dhan.co/trading-features/)
- [Dhan Super Order knowledge base](https://knowledge.dhan.co/support/solutions/folders/82000698241)
- [DhanHQ Super Order API docs](https://dhanhq.co/docs/v2/super-order/)
- [Dhan bracket order help](https://dhan.co/support/orders-and-positions/order-types/what-is-bracket-order-how-to-place-bracket-order-on-dhan/)
- [Groww Expanded Positions View](https://groww.in/updates/expanded-positions-view-on-groww)
- [Groww Charts (mobile-first)](https://groww.in/updates/groww-charts)
- [Groww Equity F&O P&L update](https://groww.in/updates/equity-futures-pnl-update)
- [Groww 915 PnL dashboard](https://915.groww.in/tools/pnl-dashboard)
- [Upstox Pro Web 3.0 announcement](https://upstox.com/market-talk/introducing-pro-web-3-0/)
- [Upstox tools update Feb 2025 (option chain refresh)](https://upstox.com/market-talk/tools-to-power-your-trades-and-investments-february-2025/)
- [Upstox option chain feature](https://upstox.com/market-talk/chain-feature-upstox-options-traders/)
- [Angel One SpeedPro](https://www.angelone.in/platform-and-tools/angel-speedpro)
- [Angel One trade platform](https://www.angelone.in/trade-platform)
- [Fyers Web option chain](https://fyers.in/web/options/option-chain)
- [Fyers option chain customization KB](https://support.fyers.in/portal/en/kb/articles/how-do-i-customise-the-options-chain-view-in-fyers)
- [Fyers Web 1.5 announcement](https://fyers.in/community/blogs-gdppin8d/post/introducing-web-1-5-enhancing-your-trading-experience-y4fLCRRlDTrJyPY)
- [Opstra options analytics](https://opstra.definedge.com/options)
- [Opstra strategy builder](https://opstra.definedge.com/strategy-builder)
- [Indian color convention reference](https://rupeezy.in/blog/what-is-colour-trading)
- [DematDive 5paisa/ICICI/Motilal comparison](https://dematdive.com/mobile-trading-apps-2/)

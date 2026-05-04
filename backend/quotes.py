"""Quote-fetching pipeline: REST + WebSocket overlay.

Two public entry points used by routes:
  - fetch_quotes()        -> {symbol: {ltp, open, low, high, levels}}, error
  - fetch_option_quotes() -> {key: {...}}, per-index meta, error

Both use a 2-second TTL cache and overlay WebSocket-streamed LTPs on top of
the REST snapshot when the WS tick is fresher than WS_FRESH_SECONDS. Module-
level _quote_cache, _option_quote_cache, _feed, _feed_started preserve their
original semantics — they used to live in app.py.
"""
import threading
import time

from backend.kotak.client import ensure_client
from backend.kotak.instruments import (
    SCRIPS, INDEX_OPTIONS_CONFIG,
    _fetch_index_fo_universe, _parse_item_strike, _parse_item_expiry_date,
    _fetch_nearest_index_future,
)
from backend.kotak.quote_feed import QuoteFeed
from backend.strategy.gann import gann_levels
from backend.utils import now_ist


# ---- TTL cache ----
_quote_cache = {"data": {}, "ts": 0, "error": None}
# D.7 — TTL is intentionally larger than the snapshot producer's refresh
# interval (2 s). The snapshot always fetches with force=True so it ALWAYS
# refreshes on its 2 s tick; the strategy ticker (3 s tick) and any ad-hoc
# request-time fetch then ride on the cache and avoid duplicating the REST
# round-trip. Earlier value of 2.0 left a gap where a tick falling between
# snapshot refreshes did its own REST fetch (~30% duplicated work).
QUOTE_TTL = 3.5  # seconds

# ---- WebSocket QuoteFeed ----
# REST polling stays as fallback; WS overlays fresher LTP when available.
_feed = QuoteFeed(client_provider=ensure_client)
_feed_started = {"flag": False, "lock": threading.Lock()}
WS_FRESH_SECONDS = 5.0   # WS tick considered fresh if newer than this

# ---- Index-spot open-price REST fallback ----
# Kotak's WS feed is broken for the NSE-CM "Nifty 50" symbol: it ships
# op=0.0 in every tick, while h/lo/c are valid (BANKNIFTY and SENSEX
# work fine on the same WS pipeline — broker-side bug specific to that
# one symbol). Without a valid `open` we cannot anchor the Gann ladder.
# Recovery: once per day per affected symbol, fetch the official open
# via the same broker over REST `quotes(quote_type="ohlc")`. This is
# real broker data — same source as WS, just a different transport.
# Gated to >= 09:30 IST because Kotak's REST OHLC returns yesterday's
# candle pre-market and during the first ~15 minutes of the session.
# A small retry budget handles the case where REST itself is still
# serving yesterday's candle (detected by open == prev-close); we back
# off and try again on the next snapshot tick.
_index_open_rest_cache = {}   # {symbol: {date, open, tries, last_try_ts}}
INDEX_OPEN_REST_GATE_HOUR = 9
INDEX_OPEN_REST_GATE_MIN = 30
INDEX_OPEN_REST_MAX_TRIES = 12
INDEX_OPEN_REST_RETRY_SECS = 30.0

# Option chain cache (keyed by "<INDEX> <STRIKE> CE/PE")
_option_quote_cache = {"ts": 0.0, "data": {}, "error": None, "meta": {}}

# Futures cache: {idx_name: {trading_symbol, token, exchange, expiry, lot_size, ltp}}
_future_quote_cache = {"ts": 0.0, "data": {}, "error": None}


def _ensure_feed_started():
    """Start the WS feed once; safe to call from any request thread."""
    if _feed_started["flag"]:
        return
    with _feed_started["lock"]:
        if _feed_started["flag"]:
            return
        # Subscribe to all SCRIPS at startup. Indices vs equities split by
        # the `tradeable` flag in instruments.py.
        idx_subs, scrip_subs = [], []
        for s in SCRIPS:
            entry = {"instrument_token": s["token"],
                     "exchange_segment": s["exchange"]}
            if s.get("tradeable"):
                scrip_subs.append(entry)
            else:
                idx_subs.append(entry)
        _feed.set_index_subs(idx_subs)
        _feed.set_scrip_subs(scrip_subs)
        _feed.start()
        _feed_started["flag"] = True
        print(f"[quote_feed] started: {len(idx_subs)} indices, "
              f"{len(scrip_subs)} scrips")


def _ws_overlay(out_dict, key_to_token_exch):
    """Overlay WS LTP + OHLC + previous-close onto out_dict in place.

    Background (yesterday's-open bug):
      Kotak's REST `quotes(quote_type="ohlc")` returns the PREVIOUS DAY
      candle until well into the new session. WS ticks carry today's
      intraday op/lo/h/c from the first print. We used to overlay WS
      on top of REST OHLC, which inverted on cache hits and showed
      yesterday's data. Fixed by making WS the SOLE source of OHLC —
      REST OHLC is no longer fetched at all (see fetch_quotes /
      fetch_option_quotes / fetch_future_quotes).

    LTP:
      - WS LTP fresher than WS_FRESH_SECONDS  -> always wins
      - WS LTP stale but REST LTP is None     -> still use WS (better
        than nothing during cold-start window)
      - WS LTP stale and REST LTP populated   -> keep REST LTP

    OHLC + close:
      - Always copied from WS when present. The `rec` starts with
        open/low/high/close = None (no REST OHLC anymore), so any WS
        value wins. If WS has no value (cold-start), the field stays
        None and the UI renders an empty cell — that is the correct
        "honest" state, not yesterday's data dressed as today's.
      - Gann levels recomputed when `open` changes OR when the ladder
        hasn't been built yet. Without this, a level dict with the
        wrong anchor survives across requests.

    `ws_age` is added for diagnostics. Returns count of overlays applied.
    """
    overlaid = 0
    now = time.time()
    for key, (exch, token) in key_to_token_exch.items():
        tick = _feed.get(exch, token)
        if not tick or tick.get("ltp") is None:
            continue
        age = now - tick.get("ts", 0)
        rec = out_dict.get(key)
        if not rec:
            continue
        rest_ltp = rec.get("ltp")
        is_fresh = age <= WS_FRESH_SECONDS
        if is_fresh or rest_ltp is None:
            rec["ltp"] = tick["ltp"]
            rec["ws_age"] = round(age, 2)
            prev_open = rec.get("open")
            # WS is the sole OHLC source — copy whatever it has.
            # `c` is Kotak's previous-day close (used for change% on
            # options and as off-hours LTP fallback in the UI).
            for src, dst in (("op", "open"), ("lo", "low"),
                             ("h", "high"), ("c", "close")):
                if tick.get(src) is not None:
                    rec[dst] = tick[src]
            new_open = rec.get("open")
            if new_open and (new_open != prev_open
                             or not rec.get("levels", {}).get("buy")):
                rec["levels"] = gann_levels(new_open)
            overlaid += 1
    return overlaid


def _recover_index_open_via_rest(symbol, exchange, token, prev_close):
    """Once-per-day REST OHLC recovery for index spots whose WS feed
    ships op=0/None. Returns the recovered open price (float) or None.

    Real broker data — same Kotak API as WS, just a different transport.
    Time-gated to >= 09:30 IST to dodge the well-known
    "REST OHLC returns yesterday's candle pre-market" issue. If REST
    itself returns yesterday's candle (detected by open == prev_close
    within 0.01), back off and retry on the next snapshot tick. Caches
    the recovered open for the rest of the trading day so we make at
    most one successful REST call per symbol per day."""
    today = now_ist().strftime("%Y-%m-%d")
    nowts = time.time()
    state = _index_open_rest_cache.get(symbol)
    if not state or state.get("date") != today:
        state = {"date": today, "open": None,
                 "tries": 0, "last_try_ts": 0.0}
        _index_open_rest_cache[symbol] = state
    if state.get("open"):
        return state["open"]
    ist = now_ist()
    if (ist.hour, ist.minute) < (INDEX_OPEN_REST_GATE_HOUR,
                                 INDEX_OPEN_REST_GATE_MIN):
        return None
    if state["tries"] >= INDEX_OPEN_REST_MAX_TRIES:
        return None
    if nowts - state.get("last_try_ts", 0.0) < INDEX_OPEN_REST_RETRY_SECS:
        return None
    state["tries"] += 1
    state["last_try_ts"] = nowts
    try:
        client = ensure_client()
        r = client.quotes(
            instrument_tokens=[{"instrument_token": token,
                                "exchange_segment": exchange}],
            quote_type="ohlc")
    except Exception as e:
        print(f"[quote_feed] REST OHLC fetch failed for {symbol}: "
              f"{type(e).__name__}: {e}")
        return None
    items = []
    if isinstance(r, list):
        items = r
    elif isinstance(r, dict):
        for k in ("data", "result", "quotes"):
            v = r.get(k)
            if isinstance(v, list):
                items = v
                break
        if not items and any(k in r for k in ("ohlc", "open",
                                              "exchange_token")):
            items = [r]
    if not items or not isinstance(items[0], dict):
        return None
    it = items[0]
    raw_open = it.get("open")
    if raw_open is None:
        ohlc = it.get("ohlc")
        if isinstance(ohlc, dict):
            raw_open = ohlc.get("open")
    try:
        open_v = float(raw_open) if raw_open not in (None, "", "0") else None
    except (TypeError, ValueError):
        open_v = None
    if not open_v or open_v <= 0:
        return None
    # Reject yesterday's candle: REST sometimes still serves yesterday's
    # OHLC during the first session window. open == prev_close means it
    # has not refreshed yet — back off and retry.
    if prev_close and abs(open_v - float(prev_close)) < 0.01:
        return None
    state["open"] = open_v
    print(f"[quote_feed] recovered {symbol} open={open_v} via REST OHLC "
          f"(WS shipped op=0)")
    return open_v


def _apply_index_open_rest_fallback(out_dict):
    """For each SCRIPS entry whose WS overlay left open=0/None, attempt
    a REST OHLC recovery. Builds the Gann ladder when a value is
    recovered. Idempotent — safe to call on every fetch_quotes path."""
    for s in SCRIPS:
        rec = out_dict.get(s["symbol"])
        if not rec:
            continue
        cur_open = rec.get("open")
        if cur_open and cur_open > 0:
            continue
        recovered = _recover_index_open_via_rest(
            s["symbol"], s["exchange"], s["token"], rec.get("close"))
        if recovered:
            rec["open"] = recovered
            rec["levels"] = gann_levels(recovered)


def fetch_quotes(force=False):
    """Fetch quotes for all SCRIPS via Kotak. Returns (dict, error_str)."""
    now = time.time()
    if not force and (now - _quote_cache["ts"]) < QUOTE_TTL and _quote_cache["data"]:
        # Cache hit: overlay fresh WS LTPs onto the cached dict so callers
        # see sub-second prices between REST refreshes (every QUOTE_TTL).
        try:
            _ws_overlay(_quote_cache["data"],
                        {s["symbol"]: (s["exchange"], s["token"]) for s in SCRIPS})
        except Exception as e:
            print(f"[quote_feed] overlay (stocks, cached) failed: "
                  f"{type(e).__name__}: {e}")
        try:
            _apply_index_open_rest_fallback(_quote_cache["data"])
        except Exception as e:
            print(f"[quote_feed] open-rest fallback (cached) failed: "
                  f"{type(e).__name__}: {e}")
        return _quote_cache["data"], _quote_cache["error"]

    try:
        client = ensure_client()
    except Exception as e:
        _quote_cache["error"] = f"login: {e}"
        return _quote_cache["data"], _quote_cache["error"]

    out = {}
    tokens = [{"instrument_token": s["token"], "exchange_segment": s["exchange"]}
              for s in SCRIPS]

    def _call(qt):
        try:
            r = client.quotes(instrument_tokens=tokens, quote_type=qt)
        except Exception as e:
            return None, f"{qt}: {type(e).__name__}: {e}"
        # Empty / nothing-to-report (common off-hours): silent, not an error.
        if r is None or r == "" or r == {} or r == []:
            return [], None
        if isinstance(r, dict) and "fault" in r:
            return None, f"{qt}: {r['fault'].get('message', 'fault')}"
        if isinstance(r, list):
            return r, None
        # Some Kotak responses wrap the list under a 'data' key.
        if isinstance(r, dict):
            for key in ("data", "result", "quotes"):
                v = r.get(key)
                if isinstance(v, list):
                    return v, None
            # Single-row dict with quote fields: wrap in a list.
            if any(k in r for k in ("ohlc", "ltp", "exchange_token")):
                return [r], None
        return [], None  # unknown shape, but treat as silent no-data

    # REST `quote_type="ohlc"` is intentionally NOT called: it returns
    # the PREVIOUS DAY candle until well into the new session, which
    # caused the yesterday's-open bug. WS is the sole source for
    # open/low/high/close — see _ws_overlay docstring.
    ltp_items, last_err = _call("ltp")

    # Index responses by (exchange, exchange_token) — Kotak echoes these back.
    def index_by_key(items):
        idx = {}
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            key = (str(it.get("exchange", "")).strip().lower(),
                   str(it.get("exchange_token", "")).strip().lower())
            idx[key] = it
        return idx

    ltp_idx = index_by_key(ltp_items)

    for s in SCRIPS:
        key = (s["exchange"].lower(), str(s["token"]).lower())
        ltp_it = ltp_idx.get(key, {})
        ltp_v = None
        try:
            ltp_v = float(ltp_it.get("ltp")) if ltp_it.get("ltp") not in (None, "", "0") else None
        except (TypeError, ValueError):
            pass
        # OHLC fields start as None — they are filled by _ws_overlay below
        # from the WS feed (today's session data). If WS has no value
        # (cold-start), they stay None and the UI shows an empty cell.
        out[s["symbol"]] = {
            "symbol": s["symbol"],
            "token": s["token"],
            "ltp": ltp_v,
            "open": None,
            "low": None,
            "high": None,
            "close": None,
            "levels": {"sell": {}, "buy": {}},
        }

    # Overlay fresh WS LTPs.
    try:
        _ensure_feed_started()
        _ws_overlay(out, {s["symbol"]: (s["exchange"], s["token"]) for s in SCRIPS})
    except Exception as e:
        print(f"[quote_feed] overlay (stocks) failed: {type(e).__name__}: {e}")

    # WS-bug fallback: Kotak ships op=0 in WS ticks for nse_cm|nifty 50
    # (other indices unaffected). Recover today's open via REST OHLC so
    # the Gann ladder can anchor. Idempotent + once-per-day cached.
    try:
        _apply_index_open_rest_fallback(out)
    except Exception as e:
        print(f"[quote_feed] open-rest fallback (fresh) failed: "
              f"{type(e).__name__}: {e}")

    _quote_cache["data"] = out
    _quote_cache["ts"] = now
    _quote_cache["error"] = last_err
    return out, last_err


def build_option_chain(index_name):
    """Return (rows, meta) for one index.
    rows: [{strike, ce: item_or_None, pe: item_or_None, is_atm}]
    meta: {atm, expiry, spot, error?}
    """
    cfg = INDEX_OPTIONS_CONFIG[index_name]
    meta = {"atm": None, "expiry": None, "spot": None, "error": None}
    # 1. Spot
    spot_quotes, _ = fetch_quotes()
    spot_row = spot_quotes.get(cfg["spot_symbol_key"])
    if not spot_row or spot_row.get("ltp") is None:
        meta["error"] = f"no spot for {cfg['spot_symbol_key']}"
        return [], meta
    spot = float(spot_row["ltp"])
    meta["spot"] = spot
    # 2. Universe
    items, err = _fetch_index_fo_universe(index_name)
    if err:
        meta["error"] = err
    if not items:
        return [], meta
    # 3. Nearest future expiry
    today = now_ist().date()
    parsed = []
    for it in items:
        d = _parse_item_expiry_date(it)
        if d and d >= today:
            parsed.append((d, str(it.get("pExpiryDate", "")).strip()))
    if not parsed:
        meta["error"] = "no future expiry"
        return [], meta
    nearest_date, nearest_str = min(parsed, key=lambda p: p[0])
    meta["expiry"] = nearest_str
    # 4. ATM strike + window
    step = cfg["strike_step"]
    atm = int(round(spot / step) * step)
    meta["atm"] = atm
    wanted = [atm + i * step for i in range(-cfg["atm_window"], cfg["atm_window"] + 1)]
    # 5. Lookup
    look = {}
    for it in items:
        if str(it.get("pExpiryDate", "")).strip() != nearest_str:
            continue
        s = _parse_item_strike(it)
        if s is None or s not in wanted:
            continue
        t = str(it.get("pOptionType", "")).strip().upper()
        if t in ("CE", "PE"):
            look[(s, t)] = it
    rows = []
    for s in wanted:
        rows.append({
            "strike": s,
            "ce": look.get((s, "CE")),
            "pe": look.get((s, "PE")),
            "is_atm": s == atm,
        })
    return rows, meta


def build_all_option_tokens():
    """Flat list of {key, token, exchange, ...} for all configured chains.
    Also returns per-index meta {atm, expiry, spot, error}.
    """
    all_resolved = []
    meta = {}
    for idx_name, cfg in INDEX_OPTIONS_CONFIG.items():
        rows, m = build_option_chain(idx_name)
        meta[idx_name] = m
        for row in rows:
            for t, item in (("CE", row.get("ce")), ("PE", row.get("pe"))):
                if not item:
                    continue
                token = item.get("pSymbol") or item.get("instrument_token")
                if not token:
                    continue
                all_resolved.append({
                    "key": f"{idx_name} {row['strike']} {t}",
                    "index": idx_name,
                    "strike": row["strike"],
                    "option_type": t,
                    "token": str(token),
                    "exchange": cfg["exchange_segment"],
                    "trading_symbol": item.get("pTrdSymbol", ""),
                    "expiry": m.get("expiry", ""),
                    "is_atm": row.get("is_atm", False),
                    "lot_size": item.get("lLotSize"),
                })
    return all_resolved, meta


def fetch_option_quotes(force=False):
    """Live quotes for all configured index option chains. TTL-cached.
    Returns (data_by_key, per_index_meta, error_str)."""
    now = time.time()
    if (not force
            and (now - _option_quote_cache["ts"]) < QUOTE_TTL
            and _option_quote_cache["data"]):
        # Cache hit: overlay fresh WS LTPs onto the cached dict so callers
        # see sub-second option prices between REST refreshes. Each cached
        # record already carries its own (exchange, token) so we can rebuild
        # the mapping without re-resolving the chain.
        try:
            mapping = {k: (rec["exchange"], rec["token"])
                       for k, rec in _option_quote_cache["data"].items()
                       if rec.get("exchange") and rec.get("token")}
            _ws_overlay(_option_quote_cache["data"], mapping)
        except Exception as e:
            print(f"[quote_feed] overlay (options, cached) failed: "
                  f"{type(e).__name__}: {e}")
        return (_option_quote_cache["data"],
                _option_quote_cache["meta"],
                _option_quote_cache["error"])
    insts, idx_meta = build_all_option_tokens()
    if not insts:
        err = next((m.get("error") for m in idx_meta.values() if m.get("error")), None)
        return {}, idx_meta, err or "no option instruments resolved"
    try:
        client = ensure_client()
    except Exception as e:
        return {}, idx_meta, f"login: {e}"
    tokens = [{"instrument_token": i["token"], "exchange_segment": i["exchange"]}
              for i in insts]

    def _call(qt):
        try:
            r = client.quotes(instrument_tokens=tokens, quote_type=qt)
        except Exception as e:
            return None, f"{qt}: {type(e).__name__}: {e}"
        # Empty / nothing-to-report (common off-hours): silent, not an error.
        if r is None or r == "" or r == {} or r == []:
            return [], None
        if isinstance(r, dict) and "fault" in r:
            return None, f"{qt}: {r['fault'].get('message', 'fault')}"
        if isinstance(r, list):
            return r, None
        # Some Kotak responses wrap the list under a 'data' key.
        if isinstance(r, dict):
            for key in ("data", "result", "quotes"):
                v = r.get(key)
                if isinstance(v, list):
                    return v, None
            # Single-row dict with quote fields: wrap in a list.
            if any(k in r for k in ("ohlc", "ltp", "exchange_token")):
                return [r], None
        return [], None  # unknown shape, but treat as silent no-data

    # REST OHLC intentionally not called — see fetch_quotes comment.
    # `close` (previous-day close) comes from WS `c` field via overlay.
    ltp_items, last_err = _call("ltp")

    def index_by_key(items):
        idx = {}
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            k = (str(it.get("exchange", "")).strip().lower(),
                 str(it.get("exchange_token", "")).strip().lower())
            idx[k] = it
        return idx

    ltp_idx = index_by_key(ltp_items)
    out = {}
    for i in insts:
        k = (i["exchange"].lower(), str(i["token"]).lower())
        ltp_it = ltp_idx.get(k, {})
        ltp_v = None
        try:
            ltp_v = float(ltp_it.get("ltp")) if ltp_it.get("ltp") not in (None, "", "0") else None
        except (TypeError, ValueError):
            pass
        out[i["key"]] = {
            "key": i["key"],
            "index": i["index"],
            "strike": i["strike"],
            "option_type": i["option_type"],
            "trading_symbol": i["trading_symbol"],
            "expiry": i["expiry"],
            "is_atm": i["is_atm"],
            "ltp": ltp_v,
            "close": None,         # filled by overlay from WS `c`
            "change_pct": None,    # computed below after overlay
            # Token+exchange propagated so callers (paper_book entry,
            # /api/paper-trades-live) can read the WS feed directly
            # after a strike drifts out of the ATM window.
            "token":    i["token"],
            "exchange": i["exchange"],
        }
    # Overlay fresh WS LTP/OHLC/close and refresh option subs if ATM drifted.
    try:
        _ensure_feed_started()
        opt_subs = [{"instrument_token": i["token"],
                     "exchange_segment": i["exchange"]} for i in insts]
        if _feed.set_option_subs(opt_subs):
            print(f"[quote_feed] option subs updated: {len(opt_subs)} contracts")
        _ws_overlay(out, {i["key"]: (i["exchange"], i["token"]) for i in insts})
    except Exception as e:
        print(f"[quote_feed] overlay (options) failed: {type(e).__name__}: {e}")

    # change_pct relative to previous close — done after overlay so
    # `close` is populated from WS. If LTP still missing after overlay
    # (cold start before first WS tick), fall back to previous close.
    for rec in out.values():
        ltp_v = rec.get("ltp")
        close_v = rec.get("close")
        if ltp_v is None and close_v is not None:
            rec["ltp"] = close_v
            ltp_v = close_v
        if ltp_v is not None and close_v not in (None, 0):
            try:
                rec["change_pct"] = round(((ltp_v - close_v) / close_v) * 100, 2)
            except ZeroDivisionError:
                pass

    _option_quote_cache["data"] = out
    _option_quote_cache["ts"] = now
    _option_quote_cache["meta"] = idx_meta
    _option_quote_cache["error"] = last_err
    return out, idx_meta, last_err


def fetch_future_quotes(force=False):
    """Live quotes for nearest-expiry index futures (NIFTY/BANKNIFTY/SENSEX).

    Returns ({idx_name: rec}, error_str). Each rec carries:
      trading_symbol, token, exchange, expiry, lot_size, ltp
    Same TTL + WS overlay pattern as fetch_option_quotes — futures are
    static per day so we only fetch the contract metadata once per index
    via _fetch_nearest_index_future, then poll quotes by token.
    """
    now = time.time()
    if (not force
            and (now - _future_quote_cache["ts"]) < QUOTE_TTL
            and _future_quote_cache["data"]):
        # Cache hit: overlay fresh WS LTPs so callers see sub-second futures
        # prices between REST refreshes. Each cached record carries its own
        # (exchange, token).
        try:
            mapping = {n: (rec["exchange"], rec["token"])
                       for n, rec in _future_quote_cache["data"].items()
                       if rec.get("exchange") and rec.get("token")}
            _ws_overlay(_future_quote_cache["data"], mapping)
        except Exception as e:
            print(f"[quote_feed] overlay (futures, cached) failed: "
                  f"{type(e).__name__}: {e}")
        return _future_quote_cache["data"], _future_quote_cache["error"]

    by_idx = {}
    last_err = None
    for idx_name in INDEX_OPTIONS_CONFIG.keys():
        rec, err = _fetch_nearest_index_future(idx_name)
        if err:
            last_err = err
            continue
        by_idx[idx_name] = {
            "trading_symbol": rec.get("pTrdSymbol"),
            "token":          str(rec.get("pSymbol")),
            "exchange":       rec.get("pExchSeg") or INDEX_OPTIONS_CONFIG[idx_name]["exchange_segment"],
            "expiry":         rec.get("pExpiryDate"),
            "lot_size":       int(rec.get("lLotSize") or 0) or None,
            "ltp":            None,
            "open":           None,
            "low":            None,
            "high":           None,
        }
    if not by_idx:
        return {}, last_err or "no future contracts resolved"

    try:
        client = ensure_client()
    except Exception as e:
        return by_idx, f"login: {e}"

    tokens = [{"instrument_token": v["token"], "exchange_segment": v["exchange"]}
              for v in by_idx.values()]

    def _call(qt):
        try:
            r = client.quotes(instrument_tokens=tokens, quote_type=qt)
        except Exception as e:
            return None, f"{qt}: {type(e).__name__}: {e}"
        if r is None or r == "" or r == {} or r == []:
            return [], None
        if isinstance(r, dict) and "fault" in r:
            return None, f"{qt}: {r['fault'].get('message', 'fault')}"
        if isinstance(r, list):
            return r, None
        if isinstance(r, dict):
            for key in ("data", "result", "quotes"):
                v = r.get(key)
                if isinstance(v, list):
                    return v, None
            if any(k in r for k in ("ohlc", "ltp", "exchange_token")):
                return [r], None
        return [], None

    # REST OHLC intentionally not called — see fetch_quotes comment.
    # OHLC + close come from WS via _ws_overlay below.
    ltp_items, e2 = _call("ltp")
    last_err = e2 or last_err

    def index_by_key(items):
        idx = {}
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            k = (str(it.get("exchange", "")).strip().lower(),
                 str(it.get("exchange_token", "")).strip().lower())
            idx[k] = it
        return idx

    ltp_idx = index_by_key(ltp_items)
    for idx_name, rec in by_idx.items():
        k = (rec["exchange"].lower(), str(rec["token"]).lower())
        ltp_it = ltp_idx.get(k, {})
        ltp_v = None
        try:
            ltp_v = float(ltp_it.get("ltp")) if ltp_it.get("ltp") not in (None, "", "0") else None
        except (TypeError, ValueError):
            pass
        rec["ltp"] = ltp_v
        rec["close"] = None  # filled by overlay from WS `c`
        # open/low/high were initialized to None at by_idx setup; overlay fills.

    # Overlay fresh WS LTPs and keep the future-subs slot up to date.
    try:
        _ensure_feed_started()
        fut_subs = [{"instrument_token": v["token"],
                     "exchange_segment": v["exchange"]} for v in by_idx.values()]
        if _feed.set_future_subs(fut_subs):
            print(f"[quote_feed] future subs updated: {len(fut_subs)} contracts")
        _ws_overlay(by_idx, {n: (v["exchange"], v["token"]) for n, v in by_idx.items()})
    except Exception as e:
        print(f"[quote_feed] overlay (futures) failed: {type(e).__name__}: {e}")

    _future_quote_cache["data"] = by_idx
    _future_quote_cache["ts"] = now
    _future_quote_cache["error"] = last_err
    return by_idx, last_err

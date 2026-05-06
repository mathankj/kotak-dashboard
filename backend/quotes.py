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
        # ---- LTP overlay (gated on freshness) ----
        # LTP must be fresh — a stale LTP would mislead the trader. Fall
        # through to REST LTP if the tick is stale and REST has a value.
        if is_fresh or rest_ltp is None:
            rec["ltp"] = tick["ltp"]
            rec["ws_age"] = round(age, 2)
            overlaid += 1
        # ---- OHLC overlay (NOT gated on freshness) ----
        # Today's open/low/high/close are intraday-stable: once received
        # in the session-open snapshot frame, they remain the correct
        # values for the rest of the day. Gating this copy on tick
        # freshness used to wipe OHLC within seconds of WS going quiet
        # (markets closed, weekend, brief reconnect gaps), because
        # fetch_quotes() rebuilds `out` with open/low/high=None every
        # call and depends on this overlay to refill them. Keep the
        # copy unconditional — if the tick has the field, use it.
        prev_open = rec.get("open")
        # `c` is Kotak's previous-day close (used for change% on
        # options and as off-hours LTP fallback in the UI).
        for src, dst in (("op", "open"), ("lo", "low"),
                         ("h", "high"), ("c", "close")):
            v = tick.get(src)
            if v is not None and v != 0 and v != 0.0:
                rec[dst] = v
        new_open = rec.get("open")
        if new_open and (new_open != prev_open
                         or not rec.get("levels", {}).get("buy")):
            rec["levels"] = gann_levels(new_open)
    return overlaid


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

    # REST OHLC fallback — fires only during market hours (09:16-15:30 IST)
    # and only for symbols that WS still hasn't provided an opening price for.
    # This covers the known Kotak NIFTY 50 WS bug where openingPrice is
    # absent from the session-open frame.  Before 09:16 IST, quote_type="ohlc"
    # returns the PREVIOUS DAY candle, so we skip it then.
    _rest_seed_missing_opens(out, client)

    _quote_cache["data"] = out
    _quote_cache["ts"] = now
    _quote_cache["error"] = last_err
    return out, last_err


def _rest_seed_missing_opens(out, client):
    """Fill open=None scrips from REST ohlc during market hours only."""
    ist = now_ist()
    mins = ist.hour * 60 + ist.minute
    if mins < 9 * 60 + 16 or mins >= 15 * 60 + 30:
        return  # outside window — REST gives stale data before 09:16

    missing = [s for s in SCRIPS
               if not out.get(s["symbol"], {}).get("open")]  # None or 0
    if not missing:
        return

    tokens = [{"instrument_token": s["token"], "exchange_segment": s["exchange"]}
              for s in missing]
    try:
        r = client.quotes(instrument_tokens=tokens, quote_type="ohlc")
    except Exception as e:
        print(f"[quotes] REST open seed call failed: {type(e).__name__}: {e}")
        return
    if not r:
        return
    if isinstance(r, dict):
        for key in ("data", "result", "quotes"):
            v = r.get(key)
            if isinstance(v, list):
                r = v
                break
        else:
            r = [r] if any(k in r for k in ("ohlc", "open", "exchange_token")) else []
    if not isinstance(r, list):
        return

    idx = {}
    for it in r:
        if not isinstance(it, dict):
            continue
        key = (str(it.get("exchange", "")).strip().lower(),
               str(it.get("exchange_token", "")).strip().lower())
        idx[key] = it

    for s in missing:
        key = (s["exchange"].lower(), str(s["token"]).lower())
        it = idx.get(key)
        if not it:
            continue
        # Kotak ohlc response nests the values: {"ohlc": {"open":...}}
        ohlc = it.get("ohlc") or {}
        raw = (ohlc.get("open") or ohlc.get("openPrice") or
               it.get("open") or it.get("openPrice") or it.get("op"))
        try:
            op = float(raw) if raw not in (None, "", "0", 0) else None
        except (TypeError, ValueError):
            op = None
        if not op:
            continue
        out[s["symbol"]]["open"] = op
        out[s["symbol"]]["levels"] = gann_levels(op)
        # Also seed WS cache so next call's _ws_overlay sees it without REST
        _feed.seed_index_op(s["exchange"], s["token"], op)
        print(f"[quotes] REST-seeded open for {s['symbol']}: {op}", flush=True)


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

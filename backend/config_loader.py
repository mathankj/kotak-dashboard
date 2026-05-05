"""Single source of truth for the user-tunable strategy config.

Reads `config.yaml` at the repo root. Hot-reloads on file mtime change so
edits via the /config web page (or by hand on the server) take effect on
the very next strategy tick — no service restart required.

Two entry points:
  get()           -> the parsed config dict (cached, mtime-checked)
  save(new_dict)  -> validate, atomically write yaml, invalidate cache

Schema (UNIFIED — one config drives BOTH options + futures strategies):

  engines.paper.enabled    bool                       # paper book on/off
         .paper.apply_to   options | futures | both   # what paper trades
         .real.enabled     bool                       # real engine on/off
         .real.apply_to    options | futures | both   # what real trades

  indices.NIFTY.paper      bool   # paper engine fires on NIFTY
         .NIFTY.real       bool   # real engine fires on NIFTY
         .BANKNIFTY.paper / .real
         .SENSEX.paper / .real

  Legacy: top-level `apply_to` (pre-engines schema) is migrated on load —
  paper.enabled=real.enabled=true, both apply_to set to the legacy value,
  all three indices default to {paper: true, real: true}.

  timings.market_start / square_off

  entry.market_open_path        bool
       .market_open_buy_level   BUY | BUY_WA       # which level fires bullish
       .market_open_sell_level  SELL | SELL_WA     # which level fires bearish
       .crossing_path           bool
       .crossing_buy_level      BUY | BUY_WA
       .crossing_sell_level     SELL | SELL_WA

  stoploss.active   A | B | C
          .variant_a_drop_rs        ₹ drop (option premium OR future LTP)
          .variant_a_buy_level      BUY | BUY_WA   (cosmetic for A)
          .variant_a_sell_level     SELL | SELL_WA (cosmetic for A)
          .variant_b_drop_pct       % drop
          .variant_b_buy_level      (cosmetic for B)
          .variant_b_sell_level
          .variant_c_buy_level      BUY | BUY_WA   (active — used by SHORT/PE exit)
          .variant_c_sell_level     SELL | SELL_WA (active — used by LONG/CE exit)

  target.ce_level   T1|T2|T3|BUY_WA   (also used as long_level for futures)
        .pe_level   S1|S2|S3|SELL_WA  (also used as short_level for futures)

  lots.{NIFTY|BANKNIFTY|SENSEX}        int >= 1   (multiplier on broker lot)
  per_day_cap.{NIFTY|BANKNIFTY|SENSEX} null or int > 0

  risk.max_daily_drawdown   null or int > 0   # ₹ loss; halts when
                                              # today's combined P&L
                                              # ≤ -threshold (H.2).

  futures_round_step.{NIFTY|BANKNIFTY|SENSEX}  step for futures limit price

Everything is intentionally defensive: a missing file, an unparseable
file, or a malformed key falls back to the documented DEFAULTS rather
than crashing the strategy.
"""
import copy
import os
import threading

import yaml


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_REPO_ROOT, "config.yaml")


# Defaults mirror the original hardcoded constants. Any missing/invalid
# key falls back here. Default is fully on: both engines run, both
# strategies, all three indices ticked for paper AND real.
DEFAULTS = {
    "engines": {
        "paper": {"enabled": True, "apply_to": "both"},
        "real":  {"enabled": True, "apply_to": "both"},
    },

    "indices": {
        "NIFTY":     {"paper": True, "real": True},
        "BANKNIFTY": {"paper": True, "real": True},
        "SENSEX":    {"paper": True, "real": True},
    },

    "timings": {
        "market_start": "09:15",
        "square_off":   "15:15",
    },

    "entry": {
        "market_open_path":        True,
        "market_open_buy_level":   "BUY",      # BUY | BUY_WA
        "market_open_sell_level":  "SELL",     # SELL | SELL_WA
        "crossing_path":           True,
        "crossing_buy_level":      "BUY",
        "crossing_sell_level":     "SELL",
    },

    "stoploss": {
        "active": "C",                         # A | B | C
        # Variant A — fixed Rs drop (premium for options, LTP for futures).
        "variant_a_drop_rs":     5,
        "variant_a_buy_level":   "BUY",
        "variant_a_sell_level":  "SELL",
        # Variant B — % drop.
        "variant_b_drop_pct":    30,
        "variant_b_buy_level":   "BUY",
        "variant_b_sell_level":  "SELL",
        # Variant C — opposite-Gann reversal. THESE ARE USED.
        "variant_c_buy_level":   "BUY",        # PE/SHORT exits when spot > this
        "variant_c_sell_level":  "SELL",       # CE/LONG exits when spot < this
    },

    "target": {
        "ce_level": "T1",                      # T1|T2|T3|BUY_WA  (also long for futures)
        "pe_level": "S1",                      # S1|S2|S3|SELL_WA (also short for futures)
    },

    "lots": {
        "NIFTY":     1,
        "BANKNIFTY": 1,
        "SENSEX":    1,
    },

    "per_day_cap": {
        "NIFTY":     None,                     # None = unlimited
        "BANKNIFTY": None,
        "SENSEX":    None,
    },

    # H.2 — auto-halt the kill switch if today's combined (real + paper)
    # P&L drops below -max_daily_drawdown rupees. None disables the check.
    # Configured as a positive number representing the worst loss tolerated.
    "risk": {
        "max_daily_drawdown": None,
    },

    # Futures-only retained: round step for the limit price.
    # BUY rounds DOWN to step, SELL rounds UP.
    "futures_round_step": {
        "NIFTY":     50,
        "BANKNIFTY": 100,
        "SENSEX":    100,
    },
}

VALID_APPLY_TO = {"options", "futures", "both"}
ENGINE_NAMES = ("paper", "real")
VALID_STOPLOSS = {"A", "B", "C", "D"}
VALID_BUY_LEVELS  = {"BUY", "BUY_WA"}
VALID_SELL_LEVELS = {"SELL", "SELL_WA"}
VALID_CE_TARGETS = {"T1", "T2", "T3", "T4", "T5",
                    "T6", "T7", "T8", "T9", "BUY_WA"}
VALID_PE_TARGETS = {"S1", "S2", "S3", "S4", "S5",
                    "S6", "S7", "S8", "S9", "SELL_WA"}
INDEX_NAMES = ("NIFTY", "BANKNIFTY", "SENSEX")


_cache = {
    "data": None,
    "mtime": None,
    "lock": threading.Lock(),
}


def _deep_merge(defaults, overrides):
    """Return defaults overlaid with overrides (recursive for dicts).

    Deep-copies values pulled from `defaults` so that callers mutating
    the returned dict can never reach back and corrupt the global
    DEFAULTS object — a footgun that bit us when tests mutated the
    coerced result expecting it to be private."""
    if not isinstance(overrides, dict):
        return copy.deepcopy(defaults)
    out = {}
    for k, v in defaults.items():
        if k in overrides:
            ov = overrides[k]
            if isinstance(v, dict) and isinstance(ov, dict):
                out[k] = _deep_merge(v, ov)
            else:
                out[k] = copy.deepcopy(ov)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _parse_hhmm(s):
    """Accept "09:15" -> (9, 15). Returns None if unparseable."""
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return (h, m)
    except (ValueError, AttributeError):
        pass
    return None


def _coerce(raw):
    """Apply DEFAULTS underlay + light type coercion. Never raises.

    Also strips legacy `futures.*` nested block from older config.yaml
    versions (pre-unified schema). The new schema gets the round-step
    from `futures_round_step` at the top level.
    """
    if isinstance(raw, dict):
        # Migrate: pull round_step out of legacy futures block if needed.
        legacy_fut = raw.get("futures") if isinstance(raw.get("futures"), dict) else None
        if legacy_fut and "futures_round_step" not in raw:
            rs = legacy_fut.get("round_step")
            if isinstance(rs, dict):
                raw["futures_round_step"] = rs
        # Drop legacy block — its other keys are now shared.
        raw.pop("futures", None)

        # Migrate legacy top-level `apply_to` (pre-engines schema) ->
        # both engines on, both apply_to set to the legacy value.
        # Only fire when no engines block is present so an explicit
        # engines block always wins.
        if "apply_to" in raw and "engines" not in raw:
            legacy_at = str(raw.get("apply_to", "both")).lower()
            if legacy_at not in VALID_APPLY_TO:
                legacy_at = "both"
            raw["engines"] = {
                "paper": {"enabled": True, "apply_to": legacy_at},
                "real":  {"enabled": True, "apply_to": legacy_at},
            }
        raw.pop("apply_to", None)

    merged = _deep_merge(DEFAULTS, raw or {})

    # engines — per-engine enable + apply_to
    eng = merged["engines"]
    for name in ENGINE_NAMES:
        cur = eng.get(name) or {}
        cur["enabled"] = bool(cur.get("enabled", True))
        ap = str(cur.get("apply_to", "both")).lower()
        cur["apply_to"] = ap if ap in VALID_APPLY_TO else "both"
        eng[name] = cur

    # indices — per-index per-engine bool. Coerce loose forms (true/false,
    # "yes"/"no", missing key -> True default) into clean bools.
    idxs = merged["indices"]
    for idx in INDEX_NAMES:
        cur = idxs.get(idx) or {}
        for engname in ENGINE_NAMES:
            v = cur.get(engname, True)
            cur[engname] = bool(v) if not isinstance(v, str) \
                else v.strip().lower() in ("1", "true", "yes", "on")
        idxs[idx] = cur

    # entry — booleans + level dropdowns
    e = merged["entry"]
    e["market_open_path"] = bool(e.get("market_open_path", True))
    e["crossing_path"]    = bool(e.get("crossing_path", True))
    for k, valid in (
        ("market_open_buy_level",  VALID_BUY_LEVELS),
        ("market_open_sell_level", VALID_SELL_LEVELS),
        ("crossing_buy_level",     VALID_BUY_LEVELS),
        ("crossing_sell_level",    VALID_SELL_LEVELS),
    ):
        v = str(e.get(k, "")).upper()
        e[k] = v if v in valid else DEFAULTS["entry"][k]

    # stoploss — active + numeric drops + per-variant level dropdowns
    sl = merged["stoploss"]
    sl["active"] = str(sl.get("active", "C")).upper()
    if sl["active"] not in VALID_STOPLOSS:
        sl["active"] = DEFAULTS["stoploss"]["active"]
    try:
        sl["variant_a_drop_rs"] = max(
            0.0, float(sl.get("variant_a_drop_rs", 5)))
    except (TypeError, ValueError):
        sl["variant_a_drop_rs"] = 5.0
    try:
        sl["variant_b_drop_pct"] = max(
            0.0, float(sl.get("variant_b_drop_pct", 30)))
    except (TypeError, ValueError):
        sl["variant_b_drop_pct"] = 30.0
    for k, valid in (
        ("variant_a_buy_level",  VALID_BUY_LEVELS),
        ("variant_a_sell_level", VALID_SELL_LEVELS),
        ("variant_b_buy_level",  VALID_BUY_LEVELS),
        ("variant_b_sell_level", VALID_SELL_LEVELS),
        ("variant_c_buy_level",  VALID_BUY_LEVELS),
        ("variant_c_sell_level", VALID_SELL_LEVELS),
    ):
        v = str(sl.get(k, "")).upper()
        sl[k] = v if v in valid else DEFAULTS["stoploss"][k]

    # target levels
    tgt = merged["target"]
    tgt["ce_level"] = str(tgt.get("ce_level", "T1")).upper()
    if tgt["ce_level"] not in VALID_CE_TARGETS:
        tgt["ce_level"] = DEFAULTS["target"]["ce_level"]
    tgt["pe_level"] = str(tgt.get("pe_level", "S1")).upper()
    if tgt["pe_level"] not in VALID_PE_TARGETS:
        tgt["pe_level"] = DEFAULTS["target"]["pe_level"]

    # lots: int >= 1 per index
    for idx in INDEX_NAMES:
        try:
            n = int(merged["lots"].get(idx, 1))
            merged["lots"][idx] = max(1, n)
        except (TypeError, ValueError):
            merged["lots"][idx] = 1

    # per-day cap: None or positive int per index
    for idx in INDEX_NAMES:
        v = merged["per_day_cap"].get(idx)
        if v is None or v == "" or v == "null":
            merged["per_day_cap"][idx] = None
        else:
            try:
                n = int(v)
                merged["per_day_cap"][idx] = n if n > 0 else None
            except (TypeError, ValueError):
                merged["per_day_cap"][idx] = None

    # futures round step: int >= 1 per index
    for idx in INDEX_NAMES:
        try:
            n = int(merged["futures_round_step"].get(idx, 50))
            merged["futures_round_step"][idx] = max(1, n)
        except (TypeError, ValueError):
            merged["futures_round_step"][idx] = DEFAULTS["futures_round_step"][idx]

    # H.2 — risk.max_daily_drawdown: None or positive int. Same shape as
    # per_day_cap above; same lenient coercion (empty/zero/negative -> None).
    risk = merged.get("risk") or {}
    v = risk.get("max_daily_drawdown")
    if v is None or v == "" or v == "null":
        risk["max_daily_drawdown"] = None
    else:
        try:
            n = int(v)
            risk["max_daily_drawdown"] = n if n > 0 else None
        except (TypeError, ValueError):
            risk["max_daily_drawdown"] = None
    merged["risk"] = risk

    # reverse_engine default block — main is single-engine, but the
    # rev-leak config.html template still references cfg.reverse_engine.*
    # When the loaded yaml has no reverse_engine, mirror the top-level
    # blocks so the template renders without UndefinedError. The actual
    # strategy never reads these (engine_enabled('reverse') == False).
    # Added 2026-05-05 alongside the other rev-leak shims.
    if not merged.get("reverse_engine"):
        merged["reverse_engine"] = {
            "entry":       dict(merged.get("entry") or {}),
            "stoploss":    dict(merged.get("stoploss") or {}),
            "target":      dict(merged.get("target") or {}),
            "lots":        dict(merged.get("lots") or {}),
            "per_day_cap": dict(merged.get("per_day_cap") or {}),
            "risk":        dict(merged.get("risk") or {}),
        }

    return merged


def _load_from_disk():
    """Read + parse + coerce config.yaml. Always returns a dict."""
    if not os.path.exists(CONFIG_FILE):
        return _coerce({})
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception:
        # Bad yaml — keep running on defaults rather than crashing the bot.
        return _coerce({})
    return _coerce(raw)


def get():
    """Return the live config dict. Re-reads disk if file mtime changed."""
    with _cache["lock"]:
        try:
            mtime = os.path.getmtime(CONFIG_FILE)
        except OSError:
            mtime = None
        if _cache["data"] is None or mtime != _cache["mtime"]:
            _cache["data"] = _load_from_disk()
            _cache["mtime"] = mtime
        return _cache["data"]


def validate(new):
    """Return list of human-readable errors. Empty list = ok to save."""
    errs = []
    if not isinstance(new, dict):
        return ["Config must be a dict."]

    # engines: each name has enabled (bool) + apply_to (one of VALID).
    eng = new.get("engines") or {}
    if not isinstance(eng, dict):
        errs.append("engines must be a dict.")
        eng = {}
    for name in ENGINE_NAMES:
        sub = eng.get(name) or {}
        if not isinstance(sub, dict):
            errs.append(f"engines.{name} must be a dict.")
            continue
        if not isinstance(sub.get("enabled", True), bool):
            errs.append(f"engines.{name}.enabled must be true or false.")
        ap = str(sub.get("apply_to", "both")).lower()
        if ap not in VALID_APPLY_TO:
            errs.append(f"engines.{name}.apply_to must be one of "
                        f"{sorted(VALID_APPLY_TO)}.")

    # indices: per-index per-engine bool. Reject "all-off" when at least
    # one engine is enabled — otherwise nothing would ever fire.
    idxs = new.get("indices") or {}
    any_engine_on = any(
        bool((eng.get(n) or {}).get("enabled", True)) for n in ENGINE_NAMES
    )
    any_idx_on = False
    for idx in INDEX_NAMES:
        cur = idxs.get(idx) or {}
        if not isinstance(cur, dict):
            errs.append(f"indices.{idx} must be a dict with paper/real bools.")
            continue
        for engname in ENGINE_NAMES:
            v = cur.get(engname, True)
            if not isinstance(v, bool):
                errs.append(f"indices.{idx}.{engname} must be true or false.")
        if cur.get("paper") or cur.get("real"):
            any_idx_on = True
    if any_engine_on and not any_idx_on:
        errs.append("At least one index must be enabled for at least one "
                    "engine — otherwise nothing will trade.")

    sl = (new.get("stoploss") or {})
    if str(sl.get("active", "")).upper() not in VALID_STOPLOSS:
        errs.append("stoploss.active must be one of A, B, C.")
    for k in ("variant_a_buy_level", "variant_b_buy_level", "variant_c_buy_level"):
        if str(sl.get(k, "BUY")).upper() not in VALID_BUY_LEVELS:
            errs.append(f"stoploss.{k} must be BUY or BUY_WA.")
    for k in ("variant_a_sell_level", "variant_b_sell_level", "variant_c_sell_level"):
        if str(sl.get(k, "SELL")).upper() not in VALID_SELL_LEVELS:
            errs.append(f"stoploss.{k} must be SELL or SELL_WA.")

    e = (new.get("entry") or {})
    for k in ("market_open_buy_level", "crossing_buy_level"):
        if str(e.get(k, "BUY")).upper() not in VALID_BUY_LEVELS:
            errs.append(f"entry.{k} must be BUY or BUY_WA.")
    for k in ("market_open_sell_level", "crossing_sell_level"):
        if str(e.get(k, "SELL")).upper() not in VALID_SELL_LEVELS:
            errs.append(f"entry.{k} must be SELL or SELL_WA.")

    tgt = (new.get("target") or {})
    if str(tgt.get("ce_level", "")).upper() not in VALID_CE_TARGETS:
        errs.append(f"target.ce_level must be one of "
                    f"{sorted(VALID_CE_TARGETS)}.")
    if str(tgt.get("pe_level", "")).upper() not in VALID_PE_TARGETS:
        errs.append(f"target.pe_level must be one of "
                    f"{sorted(VALID_PE_TARGETS)}.")

    for idx in INDEX_NAMES:
        try:
            n = int((new.get("lots") or {}).get(idx, 1))
            if n < 1:
                errs.append(f"lots.{idx} must be >= 1.")
        except (TypeError, ValueError):
            errs.append(f"lots.{idx} must be an integer.")

    for idx in INDEX_NAMES:
        v = (new.get("per_day_cap") or {}).get(idx)
        if v not in (None, "", "null"):
            try:
                n = int(v)
                if n <= 0:
                    errs.append(f"per_day_cap.{idx} must be positive or empty.")
            except (TypeError, ValueError):
                errs.append(f"per_day_cap.{idx} must be an integer or empty.")

    for idx in INDEX_NAMES:
        try:
            n = int((new.get("futures_round_step") or {}).get(idx, 50))
            if n < 1:
                errs.append(f"futures_round_step.{idx} must be >= 1.")
        except (TypeError, ValueError):
            errs.append(f"futures_round_step.{idx} must be an integer.")

    for key in ("market_start", "square_off"):
        v = (new.get("timings") or {}).get(key)
        if _parse_hhmm(v) is None:
            errs.append(f"timings.{key} must be HH:MM (24h).")

    return errs


def save(new):
    """Validate, atomically write yaml, invalidate cache.

    Raises ValueError with all messages joined if validation fails — the
    /api/config endpoint surfaces this back to the form.
    """
    errs = validate(new)
    if errs:
        raise ValueError("; ".join(errs))

    coerced = _coerce(new)

    # Atomic write: tmp file + replace, so a crash mid-write can't leave
    # config.yaml truncated and break the next reload.
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(coerced, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, CONFIG_FILE)

    with _cache["lock"]:
        _cache["data"] = coerced
        try:
            _cache["mtime"] = os.path.getmtime(CONFIG_FILE)
        except OSError:
            _cache["mtime"] = None
    return coerced


# ---- Convenience accessors used by the strategy ----

def trading_window():
    """Return ((start_h, start_m), (end_h, end_m)) tuples for the gating
    helpers in strategy/common.py. Falls back to defaults on bad input."""
    cfg = get()["timings"]
    start = _parse_hhmm(cfg.get("market_start")) or (9, 15)
    end   = _parse_hhmm(cfg.get("square_off"))   or (15, 15)
    return start, end


def lot_multiplier(idx_name):
    return get()["lots"].get(idx_name, 1)


def per_day_cap(idx_name):
    return get()["per_day_cap"].get(idx_name)


def max_daily_drawdown():
    """H.2 — ₹ loss threshold or None. None disables the auto-halt."""
    return (get().get("risk") or {}).get("max_daily_drawdown")


def _engine(name):
    return (get().get("engines") or {}).get(name) or {}


def paper_options_enabled():
    """True if the paper book engine should run option ticks."""
    e = _engine("paper")
    return bool(e.get("enabled", True)) and \
           e.get("apply_to", "both") in ("options", "both")


def paper_futures_enabled():
    """True if the paper book engine should run futures ticks."""
    e = _engine("paper")
    return bool(e.get("enabled", True)) and \
           e.get("apply_to", "both") in ("futures", "both")


def real_options_enabled():
    """True if the real-trade engine should run option ticks."""
    e = _engine("real")
    return bool(e.get("enabled", True)) and \
           e.get("apply_to", "both") in ("options", "both")


def real_futures_enabled():
    """True if the real-trade engine should run futures ticks."""
    e = _engine("real")
    return bool(e.get("enabled", True)) and \
           e.get("apply_to", "both") in ("futures", "both")


def index_enabled_for(engine, idx_name):
    """True if `engine` ('paper'|'real') should fire on `idx_name`.
    Missing index entries default to True (fail-open: matches old
    behaviour where every index always traded)."""
    cur = (get().get("indices") or {}).get(idx_name) or {}
    return bool(cur.get(engine, True))


def futures_round_step(idx_name):
    return get()["futures_round_step"].get(idx_name, 50)


# ---- Per-row level resolvers ----
# Each returns the actual numeric Gann level value for the chosen
# BUY/BUY_WA or SELL/SELL_WA, given the levels dict from gann_levels(open).

def resolve_buy_level(levels, choice):
    """choice in {BUY, BUY_WA}. Returns numeric level or None."""
    return (levels.get("buy") or {}).get(choice)


def resolve_sell_level(levels, choice):
    """choice in {SELL, SELL_WA}. Returns numeric level or None."""
    return (levels.get("sell") or {}).get(choice)


def engine_enabled(engine):
    """True if the named logic engine should run.
    Main branch is single-engine ("current" only). Reverse is rev-branch only.
    Added 2026-05-05 to fix AttributeError in ticker (rev-branch app.py
    leaked into main during a wholesale-copy deploy)."""
    if engine == "current":
        return True
    return False


def engine_block(engine):
    """Stub for compatibility with code paths that expect the rev-branch
    engine_block(). On main there is only the current engine; return the
    top-level config dict shape it already used pre-rev."""
    cfg = get()
    return {
        "entry":       cfg.get("entry") or {},
        "stoploss":    cfg.get("stoploss") or {},
        "target":      cfg.get("target") or {},
        "indices":     cfg.get("indices") or {},
        "lots":        cfg.get("lots") or {},
        "per_day_cap": cfg.get("per_day_cap") or {},
        "risk":        cfg.get("risk") or {},
    }


def engine_max_daily_drawdown(engine):
    """Per-engine drawdown threshold shim (2026-05-05 rev-leak fix).

    Main branch is single-engine. The rev-style ticker in app.py iterates
    ('current', 'reverse') and looks up a per-engine threshold. We delegate
    to the global max_daily_drawdown() for the 'current' engine and return
    None for any other engine name so the ticker's drawdown loop simply
    skips it (see app.py around line 1650 — None short-circuits via
    'if not eng_threshold: continue')."""
    if engine == "current":
        return max_daily_drawdown()
    return None

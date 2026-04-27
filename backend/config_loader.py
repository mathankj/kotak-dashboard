"""Single source of truth for the user-tunable strategy config.

Reads `config.yaml` at the repo root. Hot-reloads on file mtime change so
edits via the /config web page (or by hand on the server) take effect on
the very next strategy tick — no service restart required.

Two entry points:
  get()           -> the parsed config dict (cached, mtime-checked)
  save(new_dict)  -> validate, atomically write yaml, invalidate cache

Schema (UNIFIED — one config drives BOTH options + futures strategies):

  apply_to: options | futures | both     # which strategy(s) to run

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

  futures_round_step.{NIFTY|BANKNIFTY|SENSEX}  step for futures limit price

Everything is intentionally defensive: a missing file, an unparseable
file, or a malformed key falls back to the documented DEFAULTS rather
than crashing the strategy.
"""
import os
import threading

import yaml


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_REPO_ROOT, "config.yaml")


# Defaults mirror the original hardcoded constants. Any missing/invalid
# key falls back here. Note: "both" by default = both strategies trade.
DEFAULTS = {
    "apply_to": "both",                        # options | futures | both

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

    # Futures-only retained: round step for the limit price.
    # BUY rounds DOWN to step, SELL rounds UP.
    "futures_round_step": {
        "NIFTY":     50,
        "BANKNIFTY": 100,
        "SENSEX":    100,
    },
}

VALID_APPLY_TO = {"options", "futures", "both"}
VALID_STOPLOSS = {"A", "B", "C"}
VALID_BUY_LEVELS  = {"BUY", "BUY_WA"}
VALID_SELL_LEVELS = {"SELL", "SELL_WA"}
VALID_CE_TARGETS = {"T1", "T2", "T3", "T4", "T5", "BUY_WA"}
VALID_PE_TARGETS = {"S1", "S2", "S3", "S4", "S5", "SELL_WA"}
INDEX_NAMES = ("NIFTY", "BANKNIFTY", "SENSEX")


_cache = {
    "data": None,
    "mtime": None,
    "lock": threading.Lock(),
}


def _deep_merge(defaults, overrides):
    """Return defaults overlaid with overrides (recursive for dicts)."""
    if not isinstance(overrides, dict):
        return defaults
    out = {}
    for k, v in defaults.items():
        if k in overrides:
            ov = overrides[k]
            if isinstance(v, dict) and isinstance(ov, dict):
                out[k] = _deep_merge(v, ov)
            else:
                out[k] = ov
        else:
            out[k] = v
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

    merged = _deep_merge(DEFAULTS, raw or {})

    # apply_to
    merged["apply_to"] = str(merged.get("apply_to", "both")).lower()
    if merged["apply_to"] not in VALID_APPLY_TO:
        merged["apply_to"] = "both"

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

    if str(new.get("apply_to", "both")).lower() not in VALID_APPLY_TO:
        errs.append(f"apply_to must be one of {sorted(VALID_APPLY_TO)}.")

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


def apply_to():
    """Return current apply_to setting: 'options' | 'futures' | 'both'."""
    return get().get("apply_to", "both")


def options_enabled():
    """True if options strategy should run."""
    return apply_to() in ("options", "both")


def futures_enabled():
    """True if futures strategy should run."""
    return apply_to() in ("futures", "both")


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

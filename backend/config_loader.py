"""Single source of truth for the user-tunable strategy config.

Reads `config.yaml` at the repo root. Hot-reloads on file mtime change so
edits via the /config web page (or by hand on the server) take effect on
the very next strategy tick — no service restart required.

Two entry points:
  get()           -> the parsed config dict (cached, mtime-checked)
  save(new_dict)  -> validate, atomically write yaml, invalidate cache

Everything is intentionally defensive: a missing file, an unparseable
file, or a malformed key falls back to the documented DEFAULTS rather
than crashing the strategy. The /config UI surfaces validation errors
on save, but the running tick loop never goes down because of bad yaml.
"""
import os
import threading

import yaml


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_REPO_ROOT, "config.yaml")


# Defaults mirror the original hardcoded constants in strategy/options.py
# and strategy/common.py. Any missing/invalid key falls back here.
DEFAULTS = {
    "timings": {
        "market_start": "09:15",
        "square_off":   "15:15",
    },
    "entry": {
        "market_open_path": True,
        "crossing_path":    True,
    },
    "stoploss": {
        "active": "C",                       # A | B | C
        "variant_a_premium_drop_rs":  5,
        "variant_b_premium_drop_pct": 30,
    },
    "target": {
        "ce_level": "T1",                    # T1 | T2 | T3 | BUY_WA
        "pe_level": "S1",                    # S1 | S2 | S3 | SELL_WA
    },
    "lots": {
        "NIFTY":     1,
        "BANKNIFTY": 1,
        "SENSEX":    1,
    },
    "per_day_cap": {
        "NIFTY":     None,                   # None = unlimited
        "BANKNIFTY": None,
        "SENSEX":    None,
    },

    # --- FUTURES strategy (parallel to options, separate trades) ---
    # Defaults all DISABLED so the bot doesn't start trading futures
    # silently after a deploy. Ganesh enables per-index from /config
    # once he's confirmed margin headroom.
    "futures": {
        # Default ON for all 3 indices — Ganesh approved 2026-04-27 to
        # auto-trade futures alongside options out-of-the-box. He can
        # disable per-index from /config if margin gets tight.
        "enabled": {
            "NIFTY":     True,
            "BANKNIFTY": True,
            "SENSEX":    True,
        },
        "entry": {
            "market_open_path": True,
            "crossing_path":    True,
        },
        "stoploss": {
            "active": "C",                   # A | B | C
            # Variant A: futures LTP moves by ₹X against entry (longer
            # range than options because futures price is in points).
            "variant_a_drop_rs":  20,
            # Variant B: futures LTP moves by X% against entry.
            "variant_b_drop_pct": 1.0,
        },
        "target": {
            "long_level":  "T1",             # T1 | T2 | T3 | BUY_WA
            "short_level": "S1",             # S1 | S2 | S3 | SELL_WA
        },
        "lots": {
            "NIFTY":     1,                  # × broker lot size
            "BANKNIFTY": 1,
            "SENSEX":    1,
        },
        "per_day_cap": {
            "NIFTY":     None,
            "BANKNIFTY": None,
            "SENSEX":    None,
        },
        # Round step for the limit price. BUY rounds DOWN, SELL rounds UP.
        "round_step": {
            "NIFTY":     50,
            "BANKNIFTY": 100,
            "SENSEX":    100,
        },
    },
}

VALID_STOPLOSS = {"A", "B", "C"}
VALID_CE_TARGETS = {"T1", "T2", "T3", "BUY_WA"}
VALID_PE_TARGETS = {"S1", "S2", "S3", "SELL_WA"}
# Futures use the same Gann-level vocab as options (long = CE side, short = PE side).
VALID_LONG_TARGETS  = VALID_CE_TARGETS
VALID_SHORT_TARGETS = VALID_PE_TARGETS
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
    """Apply DEFAULTS underlay + light type coercion. Never raises."""
    merged = _deep_merge(DEFAULTS, raw or {})

    # Stoploss active must be A/B/C.
    sl = merged["stoploss"]
    sl["active"] = str(sl.get("active", "C")).upper()
    if sl["active"] not in VALID_STOPLOSS:
        sl["active"] = DEFAULTS["stoploss"]["active"]

    # Target levels must be in the valid set per side.
    tgt = merged["target"]
    tgt["ce_level"] = str(tgt.get("ce_level", "T1")).upper()
    if tgt["ce_level"] not in VALID_CE_TARGETS:
        tgt["ce_level"] = DEFAULTS["target"]["ce_level"]
    tgt["pe_level"] = str(tgt.get("pe_level", "S1")).upper()
    if tgt["pe_level"] not in VALID_PE_TARGETS:
        tgt["pe_level"] = DEFAULTS["target"]["pe_level"]

    # Lots: int >= 1 per index.
    for idx in INDEX_NAMES:
        try:
            n = int(merged["lots"].get(idx, 1))
            merged["lots"][idx] = max(1, n)
        except (TypeError, ValueError):
            merged["lots"][idx] = 1

    # Per-day cap: None or positive int per index.
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

    # Stoploss variant A/B numbers — coerce to numeric, clamp >=0.
    try:
        sl["variant_a_premium_drop_rs"] = max(
            0.0, float(sl.get("variant_a_premium_drop_rs", 5)))
    except (TypeError, ValueError):
        sl["variant_a_premium_drop_rs"] = 5.0
    try:
        sl["variant_b_premium_drop_pct"] = max(
            0.0, float(sl.get("variant_b_premium_drop_pct", 30)))
    except (TypeError, ValueError):
        sl["variant_b_premium_drop_pct"] = 30.0

    # Entry path booleans.
    e = merged["entry"]
    e["market_open_path"] = bool(e.get("market_open_path", True))
    e["crossing_path"]    = bool(e.get("crossing_path", True))

    # ---- FUTURES section ----
    f = merged["futures"]

    # Per-index enable flags (default OFF on missing).
    for idx in INDEX_NAMES:
        f["enabled"][idx] = bool(f["enabled"].get(idx, False))

    # Entry paths (same booleans as options).
    fe = f["entry"]
    fe["market_open_path"] = bool(fe.get("market_open_path", True))
    fe["crossing_path"]    = bool(fe.get("crossing_path", True))

    # Stoploss variant.
    fsl = f["stoploss"]
    fsl["active"] = str(fsl.get("active", "C")).upper()
    if fsl["active"] not in VALID_STOPLOSS:
        fsl["active"] = "C"
    try:
        fsl["variant_a_drop_rs"] = max(0.0, float(fsl.get("variant_a_drop_rs", 20)))
    except (TypeError, ValueError):
        fsl["variant_a_drop_rs"] = 20.0
    try:
        fsl["variant_b_drop_pct"] = max(0.0, float(fsl.get("variant_b_drop_pct", 1)))
    except (TypeError, ValueError):
        fsl["variant_b_drop_pct"] = 1.0

    # Target levels (long uses CE-side names, short uses PE-side names).
    ft = f["target"]
    ft["long_level"]  = str(ft.get("long_level",  "T1")).upper()
    if ft["long_level"] not in VALID_LONG_TARGETS:
        ft["long_level"] = "T1"
    ft["short_level"] = str(ft.get("short_level", "S1")).upper()
    if ft["short_level"] not in VALID_SHORT_TARGETS:
        ft["short_level"] = "S1"

    # Lots, per-day caps, round step (per index).
    for idx in INDEX_NAMES:
        try:
            n = int(f["lots"].get(idx, 1))
            f["lots"][idx] = max(1, n)
        except (TypeError, ValueError):
            f["lots"][idx] = 1
        v = f["per_day_cap"].get(idx)
        if v is None or v == "" or v == "null":
            f["per_day_cap"][idx] = None
        else:
            try:
                n = int(v)
                f["per_day_cap"][idx] = n if n > 0 else None
            except (TypeError, ValueError):
                f["per_day_cap"][idx] = None
        try:
            n = int(f["round_step"].get(idx, 50))
            f["round_step"][idx] = max(1, n)
        except (TypeError, ValueError):
            f["round_step"][idx] = 50

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

    sl = (new.get("stoploss") or {})
    if str(sl.get("active", "")).upper() not in VALID_STOPLOSS:
        errs.append("stoploss.active must be one of A, B, C.")

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

    for key in ("market_start", "square_off"):
        v = (new.get("timings") or {}).get(key)
        if _parse_hhmm(v) is None:
            errs.append(f"timings.{key} must be HH:MM (24h).")

    # ---- futures validation (only if section present) ----
    fut = new.get("futures")
    if isinstance(fut, dict):
        fsl = fut.get("stoploss") or {}
        if fsl and str(fsl.get("active", "")).upper() not in VALID_STOPLOSS:
            errs.append("futures.stoploss.active must be one of A, B, C.")
        ft = fut.get("target") or {}
        if ft:
            if str(ft.get("long_level", "")).upper() not in VALID_LONG_TARGETS:
                errs.append(f"futures.target.long_level must be one of "
                            f"{sorted(VALID_LONG_TARGETS)}.")
            if str(ft.get("short_level", "")).upper() not in VALID_SHORT_TARGETS:
                errs.append(f"futures.target.short_level must be one of "
                            f"{sorted(VALID_SHORT_TARGETS)}.")
        for idx in INDEX_NAMES:
            try:
                n = int((fut.get("lots") or {}).get(idx, 1))
                if n < 1:
                    errs.append(f"futures.lots.{idx} must be >= 1.")
            except (TypeError, ValueError):
                errs.append(f"futures.lots.{idx} must be an integer.")
            try:
                n = int((fut.get("round_step") or {}).get(idx, 50))
                if n < 1:
                    errs.append(f"futures.round_step.{idx} must be >= 1.")
            except (TypeError, ValueError):
                errs.append(f"futures.round_step.{idx} must be an integer.")
            v = (fut.get("per_day_cap") or {}).get(idx)
            if v not in (None, "", "null"):
                try:
                    n = int(v)
                    if n <= 0:
                        errs.append(f"futures.per_day_cap.{idx} must be positive or empty.")
                except (TypeError, ValueError):
                    errs.append(f"futures.per_day_cap.{idx} must be an integer or empty.")

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


# ---- Futures-specific accessors ----

def futures_enabled(idx_name):
    """True if Ganesh has enabled the futures strategy for this index."""
    return bool(get()["futures"]["enabled"].get(idx_name, False))


def futures_any_enabled():
    """True if at least one index has futures trading turned on. Used to
    short-circuit the futures tick when nothing is enabled."""
    en = get()["futures"]["enabled"]
    return any(en.get(i, False) for i in INDEX_NAMES)


def futures_lot_multiplier(idx_name):
    return get()["futures"]["lots"].get(idx_name, 1)


def futures_per_day_cap(idx_name):
    return get()["futures"]["per_day_cap"].get(idx_name)


def futures_round_step(idx_name):
    return get()["futures"]["round_step"].get(idx_name, 50)

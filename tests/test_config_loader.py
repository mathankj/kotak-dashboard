"""Tests for the engines + indices schema in backend.config_loader.

Covers:
  - legacy `apply_to: <v>` migrates to engines block (both on, both apply_to=v)
  - explicit `engines:` block wins over a stale `apply_to`
  - engine helpers: paper_options_enabled / real_options_enabled / etc.
  - per-engine per-index toggles via index_enabled_for
  - validation: rejects all-indices-off when any engine is enabled,
                rejects bad apply_to per engine
  - default config (no file) is fully on for both engines + all indices
"""
import os
from unittest.mock import patch

from backend import config_loader


def _coerce(raw):
    """Run the private coercer the same way _load_from_disk does."""
    return config_loader._coerce(raw)


# ---------- migration ----------
def test_legacy_apply_to_options_migrates_to_engines_block():
    """Old config with only `apply_to: options` -> both engines on,
    both engines.apply_to = options. Indices default to fully on."""
    c = _coerce({"apply_to": "options"})
    assert c["engines"]["paper"]["enabled"] is True
    assert c["engines"]["paper"]["apply_to"] == "options"
    assert c["engines"]["real"]["enabled"] is True
    assert c["engines"]["real"]["apply_to"] == "options"
    # Legacy key dropped after migration.
    assert "apply_to" not in c
    # Indices fully on by default.
    for idx in ("NIFTY", "BANKNIFTY", "SENSEX"):
        assert c["indices"][idx] == {"paper": True, "real": True}


def test_legacy_apply_to_invalid_falls_back_to_both():
    c = _coerce({"apply_to": "garbage"})
    assert c["engines"]["paper"]["apply_to"] == "both"
    assert c["engines"]["real"]["apply_to"] == "both"


def test_explicit_engines_block_wins_over_stale_apply_to():
    """If both legacy apply_to and modern engines are present, the
    engines block is authoritative — apply_to is ignored."""
    c = _coerce({
        "apply_to": "options",
        "engines": {
            "paper": {"enabled": False, "apply_to": "futures"},
            "real":  {"enabled": True,  "apply_to": "both"},
        },
    })
    assert c["engines"]["paper"] == {"enabled": False, "apply_to": "futures"}
    assert c["engines"]["real"] == {"enabled": True, "apply_to": "both"}
    assert "apply_to" not in c


# ---------- defaults ----------
def test_empty_config_defaults_fully_on():
    c = _coerce({})
    assert c["engines"]["paper"] == {"enabled": True, "apply_to": "both"}
    assert c["engines"]["real"] == {"enabled": True, "apply_to": "both"}
    for idx in ("NIFTY", "BANKNIFTY", "SENSEX"):
        assert c["indices"][idx] == {"paper": True, "real": True}


# ---------- engine helpers ----------
def _patched_get(c):
    """Drop a synthetic config into the cache and return the patcher."""
    return patch.object(config_loader, "get", return_value=c)


def test_paper_options_enabled_matrix():
    base = _coerce({})
    cases = [
        # (paper.enabled, paper.apply_to, expected_paper_options_enabled)
        (True,  "both",    True),
        (True,  "options", True),
        (True,  "futures", False),
        (False, "both",    False),
        (False, "options", False),
    ]
    for enabled, ap, expected in cases:
        c = _coerce({})
        c["engines"]["paper"] = {"enabled": enabled, "apply_to": ap}
        with _patched_get(c):
            assert config_loader.paper_options_enabled() is expected, \
                f"paper_options_enabled mismatch for enabled={enabled}, apply_to={ap}"


def test_real_futures_enabled_respects_engine_enabled():
    c = _coerce({})
    c["engines"]["real"] = {"enabled": False, "apply_to": "both"}
    with _patched_get(c):
        assert config_loader.real_options_enabled() is False
        assert config_loader.real_futures_enabled() is False


def test_engines_independent():
    """Paper engine settings must not affect real-engine helpers and vice versa."""
    c = _coerce({})
    c["engines"]["paper"] = {"enabled": False, "apply_to": "both"}
    c["engines"]["real"]  = {"enabled": True,  "apply_to": "options"}
    with _patched_get(c):
        assert config_loader.paper_options_enabled() is False
        assert config_loader.real_options_enabled() is True
        assert config_loader.real_futures_enabled() is False  # apply_to=options


# ---------- per-index toggles ----------
def test_index_enabled_for_returns_per_engine_value():
    c = _coerce({})
    c["indices"]["NIFTY"]     = {"paper": True,  "real": False}
    c["indices"]["BANKNIFTY"] = {"paper": False, "real": True}
    c["indices"]["SENSEX"]    = {"paper": False, "real": False}
    with _patched_get(c):
        assert config_loader.index_enabled_for("paper", "NIFTY") is True
        assert config_loader.index_enabled_for("real",  "NIFTY") is False
        assert config_loader.index_enabled_for("paper", "BANKNIFTY") is False
        assert config_loader.index_enabled_for("real",  "BANKNIFTY") is True
        assert config_loader.index_enabled_for("paper", "SENSEX") is False
        assert config_loader.index_enabled_for("real",  "SENSEX") is False


def test_index_enabled_for_unknown_index_fail_open():
    """An index not present in the config should default to True so the
    strategy never silently goes idle on a misconfiguration."""
    c = _coerce({})
    with _patched_get(c):
        assert config_loader.index_enabled_for("paper", "NEWINDEX") is True
        assert config_loader.index_enabled_for("real",  "NEWINDEX") is True


# ---------- validation ----------
def test_validate_rejects_all_indices_off_when_engines_on():
    new = _coerce({})
    for idx in ("NIFTY", "BANKNIFTY", "SENSEX"):
        new["indices"][idx] = {"paper": False, "real": False}
    errs = config_loader.validate(new)
    assert any("at least one index" in e.lower() for e in errs), \
        f"expected all-off error, got {errs}"


def test_validate_allows_all_indices_off_when_all_engines_off():
    """If both engines are disabled, an all-off indices grid is fine —
    the user is effectively turning the strategy off entirely."""
    new = _coerce({})
    new["engines"]["paper"]["enabled"] = False
    new["engines"]["real"]["enabled"]  = False
    for idx in ("NIFTY", "BANKNIFTY", "SENSEX"):
        new["indices"][idx] = {"paper": False, "real": False}
    errs = config_loader.validate(new)
    assert not any("at least one index" in e.lower() for e in errs), \
        f"unexpected all-off error: {errs}"


def test_validate_rejects_bad_engine_apply_to():
    new = _coerce({})
    new["engines"]["paper"]["apply_to"] = "garbage"
    errs = config_loader.validate(new)
    assert any("engines.paper.apply_to" in e for e in errs), \
        f"expected apply_to error, got {errs}"


def test_validate_rejects_non_bool_engine_enabled():
    new = _coerce({})
    new["engines"]["real"]["enabled"] = "yes"  # string, not bool
    errs = config_loader.validate(new)
    assert any("engines.real.enabled" in e for e in errs), \
        f"expected engine enabled error, got {errs}"

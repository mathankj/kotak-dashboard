"""Unit tests for the extended Gann ladder (S9..T9)."""
import math

from backend.strategy.gann import (
    BUY_LEVELS, SELL_LEVELS,
    BUY_LEVEL_ORDER, SELL_LEVEL_ORDER,
    LEVEL_COLORS,
    gann_levels, compute_target_level_reached,
)


def test_levels_contain_t9_s9_full_range():
    for k in ("T1", "T5", "T6", "T7", "T8", "T9"):
        assert k in BUY_LEVELS, k
    for k in ("S1", "S5", "S6", "S7", "S8", "S9"):
        assert k in SELL_LEVELS, k


def test_level_orders_extended():
    assert "T9" in BUY_LEVEL_ORDER
    assert "S9" in SELL_LEVEL_ORDER


def test_colors_cover_new_levels():
    for k in ("S9", "S6", "S5", "T5", "T6", "T9"):
        assert k in LEVEL_COLORS, k


def test_gann_levels_emits_t9_at_correct_price():
    # T9 corresponds to n=+12: price = (sqrt(open) + 12*0.0625)^2
    open_p = 25000.0
    levels = gann_levels(open_p)
    expected_t9 = round((math.sqrt(open_p) + 12 * 0.0625) ** 2, 2)
    assert levels["buy"]["T9"] == expected_t9


def test_gann_levels_emits_s9_at_correct_price():
    open_p = 25000.0
    levels = gann_levels(open_p)
    expected_s9 = round((math.sqrt(open_p) + (-12) * 0.0625) ** 2, 2)
    assert levels["sell"]["S9"] == expected_s9


def test_compute_target_level_reached_emits_t4():
    # Price between T4 and T5 should label "T4". (Highest rung still met.)
    open_p = 25000.0
    levels = gann_levels(open_p)
    px_between_t4_and_t5 = (levels["buy"]["T4"] + levels["buy"]["T5"]) / 2.0
    reached = compute_target_level_reached("B", open_p,
                                            px_between_t4_and_t5, levels)
    assert reached == "T4"


def test_compute_target_level_reached_beyond_t9():
    # Was "Beyond T5" pre-extension — must now be "Beyond T9" since the
    # ladder reaches further.
    open_p = 25000.0
    levels = gann_levels(open_p)
    far_above = levels["buy"]["T9"] + 100.0
    reached = compute_target_level_reached("B", open_p, far_above, levels)
    assert reached == "Beyond T9"


def test_compute_target_level_reached_beyond_s9():
    open_p = 25000.0
    levels = gann_levels(open_p)
    far_below = levels["sell"]["S9"] - 100.0
    reached = compute_target_level_reached("S", open_p, far_below, levels)
    assert reached == "Beyond S9"

"""Unit tests for the extended Gann ladder (S5..T5)."""
import math

from backend.strategy.gann import (
    BUY_LEVELS, SELL_LEVELS,
    BUY_LEVEL_ORDER, SELL_LEVEL_ORDER,
    LEVEL_COLORS,
    gann_levels, compute_target_level_reached,
)


def test_levels_contain_t4_t5_s4_s5():
    assert "T4" in BUY_LEVELS
    assert "T5" in BUY_LEVELS
    assert "S4" in SELL_LEVELS
    assert "S5" in SELL_LEVELS


def test_level_orders_extended():
    assert "T5" in BUY_LEVEL_ORDER
    assert "S5" in SELL_LEVEL_ORDER


def test_colors_cover_new_levels():
    for k in ("S5", "S4", "T4", "T5"):
        assert k in LEVEL_COLORS


def test_gann_levels_emits_t5_at_correct_price():
    # T5 corresponds to n=+8: price = (sqrt(open) + 8*0.0625)^2
    open_p = 25000.0
    levels = gann_levels(open_p)
    expected_t5 = round((math.sqrt(open_p) + 8 * 0.0625) ** 2, 2)
    assert levels["buy"]["T5"] == expected_t5


def test_gann_levels_emits_s5_at_correct_price():
    open_p = 25000.0
    levels = gann_levels(open_p)
    expected_s5 = round((math.sqrt(open_p) + (-8) * 0.0625) ** 2, 2)
    assert levels["sell"]["S5"] == expected_s5


def test_compute_target_level_reached_emits_t4():
    # Price between T3 and T4 should label "T3"; between T4 and T5 should
    # label "T4". (Highest rung still met.)
    open_p = 25000.0
    levels = gann_levels(open_p)
    px_between_t4_and_t5 = (levels["buy"]["T4"] + levels["buy"]["T5"]) / 2.0
    reached = compute_target_level_reached("B", open_p,
                                            px_between_t4_and_t5, levels)
    assert reached == "T4"


def test_compute_target_level_reached_beyond_t5_not_t3():
    # Regression: was "Beyond T3" — must now be "Beyond T5".
    open_p = 25000.0
    levels = gann_levels(open_p)
    far_above = levels["buy"]["T5"] + 100.0
    reached = compute_target_level_reached("B", open_p, far_above, levels)
    assert reached == "Beyond T5"


def test_compute_target_level_reached_beyond_s5_not_s3():
    open_p = 25000.0
    levels = gann_levels(open_p)
    far_below = levels["sell"]["S5"] - 100.0
    reached = compute_target_level_reached("S", open_p, far_below, levels)
    assert reached == "Beyond S5"

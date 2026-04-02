"""Tests for fundamental indicator scoring."""

from __future__ import annotations

import pytest

from indicators.fundamental import (
    score_eia_inventory,
    score_cot_positioning,
    score_usd,
)


# ---------------------------------------------------------------------------
# score_eia_inventory
# ---------------------------------------------------------------------------


class TestScoreEIAInventory:
    def test_draw_is_bullish(self):
        """Negative inventory change (draw) should produce a positive (bullish) score."""
        score = score_eia_inventory(-5.0)  # 5M barrel draw
        assert score > 0, f"Expected draw to be bullish (> 0), got {score}"

    def test_build_is_bearish(self):
        """Positive inventory change (build) should produce a negative (bearish) score."""
        score = score_eia_inventory(5.0)  # 5M barrel build
        assert score < 0, f"Expected build to be bearish (< 0), got {score}"

    def test_zero_change_is_neutral(self):
        """Zero inventory change should produce zero score."""
        score = score_eia_inventory(0.0)
        assert score == 0.0

    def test_scaling(self):
        """Scale should be -change * 10 (e.g. -5M draw → +50)."""
        score = score_eia_inventory(-5.0)
        assert score == pytest.approx(50.0, abs=0.1)

    def test_large_draw_clamped_at_100(self):
        """Very large draw should be clamped at +100."""
        score = score_eia_inventory(-15.0)  # Would be 150 unclamped
        assert score == 100.0

    def test_large_build_clamped_at_minus_100(self):
        """Very large build should be clamped at -100."""
        score = score_eia_inventory(15.0)  # Would be -150 unclamped
        assert score == -100.0

    def test_small_draw_proportional(self):
        """2M barrel draw → score should be +20."""
        score = score_eia_inventory(-2.0)
        assert score == pytest.approx(20.0, abs=0.1)

    def test_small_build_proportional(self):
        """2M barrel build → score should be -20."""
        score = score_eia_inventory(2.0)
        assert score == pytest.approx(-20.0, abs=0.1)


# ---------------------------------------------------------------------------
# score_cot_positioning
# ---------------------------------------------------------------------------


class TestScoreCOTPositioning:
    def test_net_long_is_bullish(self):
        """Positive net (net long) should produce a positive (bullish) score."""
        score = score_cot_positioning(60_000)
        assert score > 0, f"Expected net long to be bullish (> 0), got {score}"

    def test_net_short_is_bearish(self):
        """Negative net (net short) should produce a negative (bearish) score."""
        score = score_cot_positioning(-60_000)
        assert score < 0, f"Expected net short to be bearish (< 0), got {score}"

    def test_zero_net_is_neutral(self):
        """Zero net position should produce zero score."""
        score = score_cot_positioning(0)
        assert score == 0.0

    def test_scaling(self):
        """Scale is net / 3000 (e.g. +60k → +20)."""
        score = score_cot_positioning(60_000)
        assert score == pytest.approx(20.0, abs=0.1)

    def test_very_large_net_long_clamped(self):
        """Extreme net long should be clamped at +100."""
        score = score_cot_positioning(500_000)
        assert score == 100.0

    def test_very_large_net_short_clamped(self):
        """Extreme net short should be clamped at -100."""
        score = score_cot_positioning(-500_000)
        assert score == -100.0


# ---------------------------------------------------------------------------
# score_usd
# ---------------------------------------------------------------------------


class TestScoreUSD:
    def test_stronger_usd_is_bearish_for_oil(self):
        """Rising USD should produce a negative (bearish) score."""
        score = score_usd(current=103.0, previous=100.0)
        assert score < 0, f"Expected stronger USD to be bearish (< 0), got {score}"

    def test_weaker_usd_is_bullish_for_oil(self):
        """Falling USD should produce a positive (bullish) score."""
        score = score_usd(current=97.0, previous=100.0)
        assert score > 0, f"Expected weaker USD to be bullish (> 0), got {score}"

    def test_unchanged_usd_is_neutral(self):
        """No change in USD should produce zero score."""
        score = score_usd(current=100.0, previous=100.0)
        assert score == 0.0

    def test_zero_previous_returns_zero(self):
        """Previous=0 should return 0 to avoid division by zero."""
        score = score_usd(current=100.0, previous=0.0)
        assert score == 0.0

    def test_scaling(self):
        """1% USD rise should produce score of -30 (scale: -pct_change * 30)."""
        score = score_usd(current=101.0, previous=100.0)
        assert score == pytest.approx(-30.0, abs=0.5)

    def test_usd_drop_scaling(self):
        """1% USD drop should produce score of +30."""
        score = score_usd(current=99.0, previous=100.0)
        assert score == pytest.approx(30.0, abs=0.5)

    def test_large_usd_rise_clamped(self):
        """Very large USD rise should be clamped at -100."""
        score = score_usd(current=200.0, previous=100.0)
        assert score == -100.0

    def test_large_usd_drop_clamped(self):
        """Very large USD drop should be clamped at +100."""
        score = score_usd(current=50.0, previous=100.0)
        assert score == 100.0

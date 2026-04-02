"""Tests for technical indicator scoring."""

from __future__ import annotations

import sys
import os

# Ensure the analyzer service is importable via PYTHONPATH=services/analyzer
# but also support running with pytest from root when PYTHONPATH is set.

import pytest

from indicators.technical import (
    score_rsi,
    score_macd,
    score_ma_crossover,
    score_bollinger,
    aggregate_technical,
)


# ---------------------------------------------------------------------------
# score_rsi
# ---------------------------------------------------------------------------


class TestScoreRSI:
    def test_oversold_is_bullish(self):
        """RSI below 30 should produce a score above +50."""
        score = score_rsi(20.0)
        assert score > 50, f"Expected oversold RSI to be > 50, got {score}"

    def test_very_oversold_near_100(self):
        """RSI at 0 should produce score close to +100."""
        score = score_rsi(0.0)
        assert score >= 95, f"Expected RSI=0 score >= 95, got {score}"

    def test_overbought_is_bearish(self):
        """RSI above 70 should produce a score below -50."""
        score = score_rsi(80.0)
        assert score < -50, f"Expected overbought RSI to be < -50, got {score}"

    def test_very_overbought_near_minus_100(self):
        """RSI at 100 should produce score close to -100."""
        score = score_rsi(100.0)
        assert score <= -95, f"Expected RSI=100 score <= -95, got {score}"

    def test_neutral_near_zero(self):
        """RSI at 50 (midpoint of neutral zone) should produce a score near 0."""
        score = score_rsi(50.0)
        assert -15 <= score <= 15, f"Expected RSI=50 near 0, got {score}"

    def test_clamped_to_bounds(self):
        """Score must always stay within [-100, 100]."""
        for rsi_val in [0, 15, 30, 50, 70, 85, 100]:
            s = score_rsi(float(rsi_val))
            assert -100 <= s <= 100, f"RSI={rsi_val} score {s} out of bounds"

    def test_at_boundary_30(self):
        """RSI exactly at 30 should be at the edge of oversold (+60)."""
        score = score_rsi(30.0)
        assert score == pytest.approx(60.0, abs=1.0), f"Expected ~60, got {score}"

    def test_at_boundary_70(self):
        """RSI exactly at 70 should be at the edge of overbought (-60)."""
        score = score_rsi(70.0)
        assert score == pytest.approx(-60.0, abs=1.0), f"Expected ~-60, got {score}"


# ---------------------------------------------------------------------------
# score_macd
# ---------------------------------------------------------------------------


class TestScoreMACD:
    def test_bullish_crossover(self):
        """Positive histogram with macd > signal should be bullish (positive score)."""
        score = score_macd(macd_line=0.5, signal_line=0.2, histogram=0.3)
        assert score > 0, f"Expected bullish MACD score > 0, got {score}"

    def test_bearish_crossover(self):
        """Negative histogram with macd < signal should be bearish (negative score)."""
        score = score_macd(macd_line=-0.5, signal_line=-0.2, histogram=-0.3)
        assert score < 0, f"Expected bearish MACD score < 0, got {score}"

    def test_zero_histogram_crossover_dominates(self):
        """Zero histogram but macd above signal should still be positive."""
        score = score_macd(macd_line=0.1, signal_line=0.0, histogram=0.0)
        assert score > 0

    def test_clamped_to_bounds(self):
        """Score must always stay within [-100, 100]."""
        for ml, sl, hl in [(10.0, -10.0, 5.0), (-10.0, 10.0, -5.0), (0.0, 0.0, 0.0)]:
            s = score_macd(ml, sl, hl)
            assert -100 <= s <= 100, f"MACD score {s} out of bounds"

    def test_strong_bullish_near_100(self):
        """Very positive histogram + bullish crossover should approach +100."""
        score = score_macd(macd_line=2.0, signal_line=1.0, histogram=2.0)
        assert score >= 80, f"Expected strong bullish score >= 80, got {score}"

    def test_strong_bearish_near_minus_100(self):
        """Very negative histogram + bearish crossover should approach -100."""
        score = score_macd(macd_line=-2.0, signal_line=-1.0, histogram=-2.0)
        assert score <= -80, f"Expected strong bearish score <= -80, got {score}"


# ---------------------------------------------------------------------------
# aggregate_technical
# ---------------------------------------------------------------------------


class TestAggregateTechnical:
    def test_all_bullish_returns_positive(self):
        """All positive scores should produce a positive aggregate."""
        scores = {"rsi": 60.0, "macd": 50.0, "ma_cross": 40.0, "bbands": 30.0}
        agg = aggregate_technical(scores)
        assert agg > 0

    def test_all_bearish_returns_negative(self):
        """All negative scores should produce a negative aggregate."""
        scores = {"rsi": -60.0, "macd": -50.0, "ma_cross": -40.0, "bbands": -30.0}
        agg = aggregate_technical(scores)
        assert agg < 0

    def test_bounded_above(self):
        """Aggregate should never exceed +100."""
        scores = {"rsi": 100.0, "macd": 100.0, "ma_cross": 100.0, "bbands": 100.0}
        agg = aggregate_technical(scores)
        assert agg <= 100.0

    def test_bounded_below(self):
        """Aggregate should never go below -100."""
        scores = {"rsi": -100.0, "macd": -100.0, "ma_cross": -100.0, "bbands": -100.0}
        agg = aggregate_technical(scores)
        assert agg >= -100.0

    def test_missing_scores_handled(self):
        """None scores should be skipped and remaining weights renormalised."""
        scores_full = {"rsi": 80.0, "macd": 80.0, "ma_cross": 80.0, "bbands": 80.0}
        scores_partial = {"rsi": 80.0, "macd": None, "ma_cross": 80.0, "bbands": None}
        agg_full = aggregate_technical(scores_full)
        agg_partial = aggregate_technical(scores_partial)
        # Both should be positive and roughly the same since all present scores are equal
        assert agg_full > 0
        assert agg_partial > 0
        assert agg_partial == pytest.approx(agg_full, abs=5.0)

    def test_all_none_returns_zero(self):
        """All None scores should return 0.0."""
        scores = {"rsi": None, "macd": None, "ma_cross": None, "bbands": None}
        agg = aggregate_technical(scores)
        assert agg == 0.0

    def test_weights_applied_correctly(self):
        """RSI=100, rest=0 should give approx 25 (rsi weight = 0.25)."""
        scores = {"rsi": 100.0, "macd": 0.0, "ma_cross": 0.0, "bbands": 0.0}
        agg = aggregate_technical(scores)
        assert agg == pytest.approx(25.0, abs=1.0)

    def test_mixed_scores_bounded(self):
        """Extreme mixed scores should still be within [-100, 100]."""
        scores = {"rsi": 100.0, "macd": -100.0, "ma_cross": 100.0, "bbands": -100.0}
        agg = aggregate_technical(scores)
        assert -100 <= agg <= 100

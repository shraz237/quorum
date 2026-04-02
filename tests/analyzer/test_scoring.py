"""Tests for unified scoring."""

from __future__ import annotations

import pytest

from indicators.scoring import compute_unified_score, WEIGHTS


# ---------------------------------------------------------------------------
# compute_unified_score
# ---------------------------------------------------------------------------


class TestComputeUnifiedScore:
    def test_all_bullish_is_positive(self):
        """All positive module scores should produce a positive unified score."""
        score = compute_unified_score(technical=80.0, fundamental=60.0, sentiment_shipping=70.0)
        assert score > 0, f"Expected positive unified score, got {score}"

    def test_all_bearish_is_negative(self):
        """All negative module scores should produce a negative unified score."""
        score = compute_unified_score(technical=-80.0, fundamental=-60.0, sentiment_shipping=-70.0)
        assert score < 0, f"Expected negative unified score, got {score}"

    def test_all_zero_is_zero(self):
        """All zero module scores should produce zero unified score."""
        score = compute_unified_score(technical=0.0, fundamental=0.0, sentiment_shipping=0.0)
        assert score == pytest.approx(0.0, abs=0.01)

    def test_all_none_returns_none(self):
        """All None inputs should return None."""
        score = compute_unified_score(technical=None, fundamental=None, sentiment_shipping=None)
        assert score is None

    def test_weighted_correctly(self):
        """Verify weights: technical=0.40, fundamental=0.35, sentiment_shipping=0.25."""
        # Set up equal scores to verify total = sum of weights * score / sum of weights = score
        score = compute_unified_score(technical=100.0, fundamental=100.0, sentiment_shipping=100.0)
        assert score == pytest.approx(100.0, abs=0.01)

    def test_technical_weight(self):
        """Only technical score provided → result equals that score (renormalised)."""
        score = compute_unified_score(technical=80.0, fundamental=None, sentiment_shipping=None)
        assert score == pytest.approx(80.0, abs=0.01)

    def test_fundamental_weight(self):
        """Only fundamental score provided → result equals that score (renormalised)."""
        score = compute_unified_score(technical=None, fundamental=60.0, sentiment_shipping=None)
        assert score == pytest.approx(60.0, abs=0.01)

    def test_sentiment_weight(self):
        """Only sentiment score provided → result equals that score (renormalised)."""
        score = compute_unified_score(technical=None, fundamental=None, sentiment_shipping=40.0)
        assert score == pytest.approx(40.0, abs=0.01)

    def test_mixed_scores_weighted(self):
        """Verify exact weighted calculation with known inputs."""
        tech = 100.0   # weight 0.40 → contributes 40
        fund = 0.0     # weight 0.35 → contributes 0
        sent = 0.0     # weight 0.25 → contributes 0
        # Expected: (100*0.40 + 0*0.35 + 0*0.25) / (0.40+0.35+0.25) = 40/1.0 = 40
        score = compute_unified_score(technical=tech, fundamental=fund, sentiment_shipping=sent)
        assert score == pytest.approx(40.0, abs=0.5)

    def test_mixed_scores_two_inputs(self):
        """Two inputs with known weights should produce correct weighted average."""
        # tech=100 (w=0.40), fund=0 (w=0.35), sent=None
        # Expected: (100*0.40 + 0*0.35) / (0.40 + 0.35) = 40/0.75 = 53.33
        score = compute_unified_score(technical=100.0, fundamental=0.0, sentiment_shipping=None)
        expected = (100.0 * 0.40 + 0.0 * 0.35) / (0.40 + 0.35)
        assert score == pytest.approx(expected, abs=0.5)

    def test_clamped_at_upper_bound(self):
        """Score must not exceed +100."""
        score = compute_unified_score(technical=100.0, fundamental=100.0, sentiment_shipping=100.0)
        assert score <= 100.0

    def test_clamped_at_lower_bound(self):
        """Score must not go below -100."""
        score = compute_unified_score(technical=-100.0, fundamental=-100.0, sentiment_shipping=-100.0)
        assert score >= -100.0

    def test_extreme_mixed_scores_bounded(self):
        """Extreme mixed scores should still be within [-100, 100]."""
        score = compute_unified_score(technical=100.0, fundamental=-100.0, sentiment_shipping=100.0)
        assert -100.0 <= score <= 100.0

    def test_partial_inputs_renormalise_weights(self):
        """Missing inputs should cause weights to be renormalised, not zeroed."""
        # With only technical=50 and fundamental=50 (both equal):
        # Should produce 50, not 50*(0.40+0.35)/1.0 = 37.5
        score = compute_unified_score(technical=50.0, fundamental=50.0, sentiment_shipping=None)
        assert score == pytest.approx(50.0, abs=0.5)


# ---------------------------------------------------------------------------
# WEIGHTS constant
# ---------------------------------------------------------------------------


class TestWeightsConstant:
    def test_weights_sum_to_one(self):
        """The defined weights should sum to 1.0."""
        total = sum(WEIGHTS.values())
        assert total == pytest.approx(1.0, abs=0.001)

    def test_expected_keys(self):
        """WEIGHTS should contain the three expected module keys."""
        assert set(WEIGHTS.keys()) == {"technical", "fundamental", "sentiment_shipping"}

    def test_technical_weight_value(self):
        assert WEIGHTS["technical"] == pytest.approx(0.40)

    def test_fundamental_weight_value(self):
        assert WEIGHTS["fundamental"] == pytest.approx(0.35)

    def test_sentiment_weight_value(self):
        assert WEIGHTS["sentiment_shipping"] == pytest.approx(0.25)

"""Unified scoring: combines technical, fundamental, and sentiment scores."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Weights for unified score
WEIGHTS = {
    "technical": 0.40,
    "fundamental": 0.35,
    "sentiment_shipping": 0.25,
}


def _clamp(value: float, lo: float = -100.0, hi: float = 100.0) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Unified score computation
# ---------------------------------------------------------------------------


def compute_unified_score(
    technical: float | None,
    fundamental: float | None,
    sentiment_shipping: float | None,
) -> float | None:
    """Compute weighted unified score from module scores.

    Weights: technical=0.40, fundamental=0.35, sentiment_shipping=0.25.
    Missing (None) scores are excluded and weights are renormalised.
    Returns a score in [-100, 100] or None if all inputs are None.
    """
    inputs = {
        "technical": technical,
        "fundamental": fundamental,
        "sentiment_shipping": sentiment_shipping,
    }

    total_weight = 0.0
    weighted_sum = 0.0
    for key, weight in WEIGHTS.items():
        val = inputs.get(key)
        if val is not None:
            weighted_sum += val * weight
            total_weight += weight

    if total_weight == 0:
        return None

    return _clamp(weighted_sum / total_weight)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_latest_sentiment_score() -> float | None:
    """Compute a combined sentiment score from SentimentNews + SentimentTwitter.

    Weights: news=0.60, twitter=0.40.
    Individual record scores are in [-1.0, +1.0]; we scale to [-100, 100].
    Returns None if no sentiment data is available.
    """
    from shared.models import SessionLocal, SentimentNews, SentimentTwitter
    from sqlalchemy import select

    session = SessionLocal()
    try:
        # Last 50 news items
        news_stmt = (
            select(SentimentNews)
            .order_by(SentimentNews.timestamp.desc())
            .limit(50)
        )
        news_rows = session.execute(news_stmt).scalars().all()

        # Last 20 twitter records
        twitter_stmt = (
            select(SentimentTwitter)
            .order_by(SentimentTwitter.timestamp.desc())
            .limit(20)
        )
        twitter_rows = session.execute(twitter_stmt).scalars().all()
    finally:
        session.close()

    news_score: float | None = None
    twitter_score: float | None = None

    if news_rows:
        # Weighted average by relevance
        total_relevance = sum(r.relevance for r in news_rows)
        if total_relevance > 0:
            news_score = sum(r.score * r.relevance for r in news_rows) / total_relevance

    if twitter_rows:
        twitter_score = sum(r.score for r in twitter_rows) / len(twitter_rows)

    # Combine news + twitter
    parts: list[tuple[float, float]] = []
    if news_score is not None:
        parts.append((news_score, 0.60))
    if twitter_score is not None:
        parts.append((twitter_score, 0.40))

    if not parts:
        return None

    total_w = sum(w for _, w in parts)
    combined = sum(s * w for s, w in parts) / total_w

    # Scale from [-1.0, +1.0] to [-100, 100]
    return _clamp(combined * 100.0)


def store_scores(
    technical: float | None,
    fundamental: float | None,
    sentiment_shipping: float | None,
    unified: float | None,
) -> None:
    """Persist an AnalysisScore record to the database.

    Note: sentiment_shipping is stored in the sentiment_score column
    (shipping sentiment is the sentiment component in Phase 5).
    """
    from shared.models import SessionLocal, AnalysisScore

    now = datetime.now(timezone.utc)
    record = AnalysisScore(
        timestamp=now,
        technical_score=technical,
        fundamental_score=fundamental,
        sentiment_score=sentiment_shipping,
        shipping_score=None,  # shipping handled in Phase 9
        unified_score=unified,
    )
    session = SessionLocal()
    try:
        session.add(record)
        session.commit()
        logger.info(
            "Stored scores — technical=%.1f fundamental=%.1f sentiment=%.1f unified=%.1f",
            technical or 0.0,
            fundamental or 0.0,
            sentiment_shipping or 0.0,
            unified or 0.0,
        )
    except Exception:
        session.rollback()
        logger.exception("Failed to store analysis scores")
        raise
    finally:
        session.close()

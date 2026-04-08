"""Unified scoring: combines technical, fundamental, sentiment, and shipping scores."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Weights for unified score.
#
# Breakdown:
#   technical     0.50 — dominant; 15-min cycles stay responsive to price action
#   fundamental   0.15 — weekly data (EIA, COT); low weight to avoid staleness
#   sentiment     0.25 — news+twitter (0.60) + @marketfeed knowledge (0.40) combined
#   shipping      0.10 — AIS/PortWatch data; supplements fundamental supply picture
#
# sentiment_score stored in ScoresEvent/AnalysisScore = combined news+knowledge
# shipping_score stored in ScoresEvent/AnalysisScore = compute_shipping_score()
# ---------------------------------------------------------------------------
WEIGHTS = {
    "technical": 0.50,
    "fundamental": 0.15,
    "sentiment": 0.25,   # combined news+twitter+knowledge
    "shipping": 0.10,
}

# ---------------------------------------------------------------------------
# Freshness thresholds — if a source's newest row is older than this, its
# score is set to None and its weight is dropped to 0 in renormalisation.
# ---------------------------------------------------------------------------
FRESHNESS_THRESHOLDS: dict[str, timedelta] = {
    "technical_1H": timedelta(hours=2),     # 1H bar expected every hour
    "fundamental_eia": timedelta(days=14),  # weekly report; 2× cadence
    "fundamental_cot": timedelta(days=14),  # weekly report; 2× cadence
    "fundamental_fred": timedelta(days=3),  # daily series; 3-day gate
    "sentiment_news": timedelta(hours=2),   # near-real-time feed
    "knowledge": timedelta(minutes=30),     # 5-min digest; 6× cadence
    "shipping": timedelta(days=14),         # weekly AIS aggregate
}


def _clamp(value: float, lo: float = -100.0, hi: float = 100.0) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


def _combine(pairs: list[tuple[float | None, float]]) -> float | None:
    """Weighted average of (value, weight) pairs, ignoring None values.

    Returns None if all values are None.
    """
    total_w = 0.0
    weighted_sum = 0.0
    for val, w in pairs:
        if val is not None:
            weighted_sum += val * w
            total_w += w
    if total_w == 0:
        return None
    return _clamp(weighted_sum / total_w)


# ---------------------------------------------------------------------------
# Unified score computation
# ---------------------------------------------------------------------------


def compute_unified_score(
    technical: float | None,
    fundamental: float | None,
    sentiment: float | None,
    shipping: float | None,
) -> float | None:
    """Compute weighted unified score from module scores.

    Weights: technical=0.50, fundamental=0.15, sentiment=0.25, shipping=0.10.
    Missing (None) scores are excluded and weights are renormalised.
    Returns a score in [-100, 100] or None if all inputs are None.
    """
    inputs = {
        "technical": technical,
        "fundamental": fundamental,
        "sentiment": sentiment,
        "shipping": shipping,
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
# DB helpers — sentiment
# ---------------------------------------------------------------------------


def get_latest_sentiment_score() -> float | None:
    """Compute a combined sentiment score from SentimentNews + SentimentTwitter.

    Weights: news=0.60, twitter=0.40.
    Individual record scores are in [-1.0, +1.0]; we scale to [-100, 100].

    Freshness gate: news items must be newer than 2 hours; if all news is
    stale the news component is dropped and only twitter is used.  If both
    are stale, returns None.
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

    cutoff = datetime.now(timezone.utc) - FRESHNESS_THRESHOLDS["sentiment_news"]

    news_score: float | None = None
    if news_rows:
        # Check freshness of the most recent news item
        newest_news_ts = news_rows[0].timestamp.replace(tzinfo=timezone.utc)
        if newest_news_ts >= cutoff:
            total_relevance = sum(r.relevance for r in news_rows)
            if total_relevance > 0:
                news_score = sum(r.score * r.relevance for r in news_rows) / total_relevance
        else:
            logger.warning("News sentiment data is stale — ignoring in sentiment score")

    twitter_score: float | None = None
    if twitter_rows:
        twitter_score = sum(r.score for r in twitter_rows) / len(twitter_rows)

    # Combine news + twitter with renormalisation over present components
    combined = _combine([(news_score, 0.60), (twitter_score, 0.40)])
    if combined is None:
        return None

    # Scale from [-1.0, +1.0] to [-100, 100]
    return _clamp(combined * 100.0)


# ---------------------------------------------------------------------------
# DB helpers — knowledge (@marketfeed digest)
# ---------------------------------------------------------------------------


def get_knowledge_sentiment_score() -> float | None:
    """Return -100..+100 score from recent KnowledgeSummary rows.

    @marketfeed knowledge summaries are ingested every ~5 minutes.
    Freshness gate: summaries must be newer than 30 minutes.
    Scores are recency-weighted (1/rank).
    """
    from shared.models.knowledge import KnowledgeSummary
    from shared.models.base import SessionLocal
    from sqlalchemy import select, desc

    cutoff_freshness = datetime.now(timezone.utc) - FRESHNESS_THRESHOLDS["knowledge"]
    cutoff_query = datetime.now(timezone.utc) - timedelta(hours=2)

    with SessionLocal() as session:
        rows = session.scalars(
            select(KnowledgeSummary)
            .where(KnowledgeSummary.timestamp >= cutoff_query)
            .order_by(desc(KnowledgeSummary.timestamp))
            .limit(20)
        ).all()

    if not rows:
        return None

    # Freshness gate: newest summary must be within 30 minutes
    newest_ts = rows[0].timestamp.replace(tzinfo=timezone.utc)
    if newest_ts < cutoff_freshness:
        logger.warning(
            "Knowledge summaries are stale (newest=%s) — returning None", newest_ts
        )
        return None

    # Weight by recency (most recent first): weights 1, 0.5, 0.33, 0.25, …
    total_weight = 0.0
    weighted_sum = 0.0
    for i, r in enumerate(rows):
        if r.sentiment_score is None:
            continue
        weight = 1.0 / (i + 1)
        # sentiment_score is in [-1.0, +1.0]; scale to [-100, +100]
        weighted_sum += float(r.sentiment_score) * 100.0 * weight
        total_weight += weight

    if total_weight == 0:
        return None

    return _clamp(weighted_sum / total_weight)


# ---------------------------------------------------------------------------
# DB helpers — persistence
# ---------------------------------------------------------------------------


def store_scores(
    technical: float | None,
    fundamental: float | None,
    sentiment_shipping: float | None,
    unified: float | None,
    shipping: float | None = None,
) -> None:
    """Persist an AnalysisScore record to the database.

    sentiment_shipping is stored in the sentiment_score column.
    shipping is stored in the shipping_score column.
    """
    from shared.models import SessionLocal, AnalysisScore

    now = datetime.now(timezone.utc)
    record = AnalysisScore(
        timestamp=now,
        technical_score=technical,
        fundamental_score=fundamental,
        sentiment_score=sentiment_shipping,
        shipping_score=shipping,
        unified_score=unified,
    )
    session = SessionLocal()
    try:
        session.add(record)
        session.commit()
        logger.info(
            "Stored scores — technical=%s fundamental=%s sentiment=%s shipping=%s unified=%s",
            f"{technical:.1f}" if technical is not None else "N/A",
            f"{fundamental:.1f}" if fundamental is not None else "N/A",
            f"{sentiment_shipping:.1f}" if sentiment_shipping is not None else "N/A",
            f"{shipping:.1f}" if shipping is not None else "N/A",
            f"{unified:.1f}" if unified is not None else "N/A",
        )
    except Exception:
        session.rollback()
        logger.exception("Failed to store analysis scores")
        raise
    finally:
        session.close()

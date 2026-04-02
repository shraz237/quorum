"""Analyzer service — subscribes to prices.brent, computes scores, publishes to analysis.scores."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from shared.redis_streams import subscribe, publish
from shared.schemas.events import ScoresEvent

from indicators.technical import compute_technical_score
from indicators.fundamental import compute_fundamental_score
from indicators.scoring import compute_unified_score, get_latest_sentiment_score, store_scores

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

STREAM_IN = "prices.brent"
STREAM_OUT = "analysis.scores"
GROUP = "analyzer"
CONSUMER = "analyzer-1"


def run_analysis() -> None:
    """Compute all scores and publish the result."""
    logger.info("Running analysis cycle")

    technical = compute_technical_score()
    fundamental = compute_fundamental_score()
    sentiment_shipping = get_latest_sentiment_score()
    unified = compute_unified_score(technical, fundamental, sentiment_shipping)

    logger.info(
        "Scores — technical=%s fundamental=%s sentiment=%s unified=%s",
        f"{technical:.1f}" if technical is not None else "N/A",
        f"{fundamental:.1f}" if fundamental is not None else "N/A",
        f"{sentiment_shipping:.1f}" if sentiment_shipping is not None else "N/A",
        f"{unified:.1f}" if unified is not None else "N/A",
    )

    # Persist to DB
    try:
        store_scores(technical, fundamental, sentiment_shipping, unified)
    except Exception:
        logger.exception("Failed to persist scores to DB")

    # Publish to Redis stream
    event = ScoresEvent(
        timestamp=datetime.now(timezone.utc),
        technical_score=technical,
        fundamental_score=fundamental,
        sentiment_score=sentiment_shipping,
        shipping_score=None,
        unified_score=unified,
    )
    try:
        publish(STREAM_OUT, event.model_dump())
        logger.info("Published ScoresEvent to %s", STREAM_OUT)
    except Exception:
        logger.exception("Failed to publish scores to Redis")


def main() -> None:
    logger.info("Analyzer service starting — listening on stream '%s'", STREAM_IN)

    # Run an initial analysis cycle on startup
    try:
        run_analysis()
    except Exception:
        logger.exception("Initial analysis cycle failed")

    # Subscribe to price events and re-run analysis on each new bar
    for msg_id, data in subscribe(STREAM_IN, group=GROUP, consumer=CONSUMER, block=10_000):
        logger.info("Received price event %s — triggering analysis", msg_id)
        try:
            run_analysis()
        except Exception:
            logger.exception("Analysis cycle failed for message %s", msg_id)


if __name__ == "__main__":
    main()

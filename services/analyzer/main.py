"""Analyzer service — subscribes to prices.brent, computes scores, publishes to analysis.scores."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from shared.redis_streams import subscribe, publish
from shared.schemas.events import ScoresEvent

from indicators.technical import compute_technical_score
from indicators.fundamental import compute_fundamental_score
from indicators.scoring import (
    compute_unified_score,
    get_latest_sentiment_score,
    get_knowledge_sentiment_score,
    store_scores,
    _combine,
)
from indicators.shipping_score import compute_shipping_score

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

STREAM_IN = "prices.brent"
STREAM_OUT = "analysis.scores"
GROUP = "analyzer"
CONSUMER = "analyzer-1"

# Minimum seconds between analysis cycles (900s = 15 minutes per spec).
MIN_CYCLE_INTERVAL_SECONDS = 900

# Last time run_analysis actually published a scores event.
_last_cycle_ts: float = 0.0

# Set to True when a price event arrives but the throttle blocks the cycle.
# A background watchdog thread will re-trigger the cycle when the throttle expires.
_pending_cycle: bool = False
_pending_lock = threading.Lock()


def _watchdog() -> None:
    """Background thread: re-run analysis when a pending cycle is overdue."""
    while True:
        time.sleep(30)
        with _pending_lock:
            pending = _pending_cycle
        if pending:
            elapsed = time.time() - _last_cycle_ts
            if elapsed >= MIN_CYCLE_INTERVAL_SECONDS:
                logger.info("Watchdog: pending cycle overdue — running deferred analysis")
                try:
                    run_analysis(force=True)
                except Exception:
                    logger.exception("Watchdog deferred analysis cycle failed")


def run_analysis(force: bool = False) -> None:
    """Compute all scores and publish the result.

    Throttled to MIN_CYCLE_INTERVAL_SECONDS unless *force=True*. Skips
    publishing when every score is None (cold start — no data yet).
    When throttled, sets _pending_cycle=True so the watchdog can re-trigger.

    Score pipeline:
      1. technical    — multi-timeframe RSI/MACD/MA/BB with ADX regime filter
      2. fundamental  — EIA/COT/USD with rolling z-scores; freshness-gated
      3. sentiment    — combined news+twitter+@marketfeed knowledge (0.60/0.40 split
                        inside news+twitter; knowledge weighted 0.40 of combined)
      4. shipping     — AIS floating storage + Hormuz + PortWatch; freshness-gated
      5. unified      — weighted: technical=0.50, fundamental=0.15,
                        sentiment=0.25, shipping=0.10

    ScoresEvent fields (schema unchanged):
      sentiment_score = combined news+twitter+knowledge
      shipping_score  = AIS/PortWatch shipping score
    """
    global _last_cycle_ts, _pending_cycle
    now_ts = time.time()
    if not force and (now_ts - _last_cycle_ts) < MIN_CYCLE_INTERVAL_SECONDS:
        remaining = MIN_CYCLE_INTERVAL_SECONDS - (now_ts - _last_cycle_ts)
        logger.info("Throttled — %.0fs until next cycle (deferred cycle queued)", remaining)
        with _pending_lock:
            _pending_cycle = True
        return

    logger.info("Running analysis cycle")

    # --- Technical score (ADX regime-adjusted) ---
    technical = compute_technical_score()

    # --- Fundamental score (rolling z-score, freshness-gated) ---
    fundamental = compute_fundamental_score()

    # --- Sentiment: news+twitter (freshness-gated) + @marketfeed knowledge ---
    sent_news = get_latest_sentiment_score()       # news+twitter; already -100..+100
    sent_knowledge = get_knowledge_sentiment_score()  # @marketfeed digest; -100..+100
    # Combine: news/twitter 60%, @marketfeed knowledge 40%
    # Both are already -100..+100, so _combine works directly.
    sentiment = _combine([(sent_news, 0.60), (sent_knowledge, 0.40)])

    # --- Shipping score (AIS / PortWatch, freshness-gated) ---
    shipping = compute_shipping_score()

    # --- Unified score ---
    unified = compute_unified_score(technical, fundamental, sentiment, shipping)

    # Skip publishing when everything is None (cold start). The AI brain
    # would just burn tokens producing an "unable to analyse" message.
    if all(v is None for v in [technical, fundamental, sentiment, shipping, unified]):
        logger.info("All scores None — skipping publish (cold start)")
        _last_cycle_ts = now_ts
        return

    logger.info(
        "Scores — technical=%s fundamental=%s sentiment=%s "
        "(news=%s knowledge=%s) shipping=%s unified=%s",
        f"{technical:.1f}" if technical is not None else "N/A",
        f"{fundamental:.1f}" if fundamental is not None else "N/A",
        f"{sentiment:.1f}" if sentiment is not None else "N/A",
        f"{sent_news:.1f}" if sent_news is not None else "N/A",
        f"{sent_knowledge:.1f}" if sent_knowledge is not None else "N/A",
        f"{shipping:.1f}" if shipping is not None else "N/A",
        f"{unified:.1f}" if unified is not None else "N/A",
    )

    # Persist to DB
    try:
        store_scores(technical, fundamental, sentiment, unified, shipping=shipping)
    except Exception:
        logger.exception("Failed to persist scores to DB")

    # Publish to Redis stream
    # ScoresEvent schema (shared/schemas/events.py) is NOT changed:
    #   sentiment_score = combined news+twitter+knowledge
    #   shipping_score  = AIS/PortWatch shipping score
    event = ScoresEvent(
        timestamp=datetime.now(timezone.utc),
        technical_score=technical,
        fundamental_score=fundamental,
        sentiment_score=sentiment,
        shipping_score=shipping,
        unified_score=unified,
    )
    try:
        publish(STREAM_OUT, event.model_dump())
        logger.info("Published ScoresEvent to %s", STREAM_OUT)
        _last_cycle_ts = now_ts
        with _pending_lock:
            _pending_cycle = False
    except Exception:
        logger.exception("Failed to publish scores to Redis")


def main() -> None:
    logger.info("Analyzer service starting — listening on stream '%s'", STREAM_IN)

    # Start background watchdog to handle deferred cycles when throttle blocks
    watchdog_thread = threading.Thread(target=_watchdog, name="analyzer-watchdog", daemon=True)
    watchdog_thread.start()
    logger.info("Watchdog thread started (checks every 30s for deferred cycles)")

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

"""Sentiment service — schedules RSS and Twitter/X sentiment collection."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from sources.rss import collect_and_store as rss_collect
from sources.twitter import collect_and_store as twitter_collect
from sources.marketfeed import collect_and_store as marketfeed_collect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def safe_run(fn, name: str) -> None:
    """Execute *fn* and log any exception without crashing the scheduler."""
    try:
        fn()
    except Exception:
        logger.exception("Job '%s' failed", name)


def main() -> None:
    scheduler = BlockingScheduler(timezone="UTC")

    # RSS news: every 30 minutes
    scheduler.add_job(
        safe_run,
        "interval",
        minutes=30,
        args=[rss_collect, "rss_news"],
        id="rss_news",
        next_run_time=datetime.now(tz=timezone.utc),  # fire immediately on startup  # do not run immediately on startup
    )

    # Twitter/X via Grok: every 15 minutes
    scheduler.add_job(
        safe_run,
        "interval",
        minutes=15,
        args=[twitter_collect, "twitter_sentiment"],
        id="twitter_sentiment",
        next_run_time=datetime.now(tz=timezone.utc),  # fire immediately on startup
    )

    # Telegram @marketfeed channel: every 5 minutes.
    # Wave 2: marketfeed.py now produces BOTH per-message classification AND
    # a 5-minute digest in a single combined Haiku call, so the separate
    # marketfeed_summary job is no longer needed.
    scheduler.add_job(
        safe_run,
        "interval",
        minutes=120,  # every 2 hours (was 5 min — saves ~280 Haiku calls/day)
        args=[marketfeed_collect, "marketfeed"],
        id="marketfeed",
        next_run_time=datetime.now(tz=timezone.utc),
    )

    logger.info(
        "Sentiment scheduler starting — RSS 30min, Twitter 15min, "
        "@marketfeed 2h (classify+digest combined)"
    )
    scheduler.start()


if __name__ == "__main__":
    main()

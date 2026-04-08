"""Data-collector service entry point.

Schedules periodic jobs to fetch Brent crude OHLCV data from Yahoo Finance
and Alpha Vantage, persist it to TimescaleDB, and publish events to Redis.
"""

from __future__ import annotations

import logging
import traceback
from typing import Callable

from apscheduler.schedulers.blocking import BlockingScheduler

from shared.db_init import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def safe_run(fn: Callable, *args, **kwargs) -> None:
    """Call *fn* with *args*/*kwargs*, logging any exception without crashing."""
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.error("Job %s failed:\n%s", fn.__qualname__, traceback.format_exc())


def main() -> None:
    logger.info("Initialising database …")
    init_db()
    logger.info("Database initialised.")

    # Import collectors after DB is initialised (avoids import-time DB calls)
    from collectors.yahoo import collect_and_store as yf_collect
    from collectors.shipping import collect_and_store as shipping_collect
    from collectors.portwatch import collect_and_store as portwatch_collect
    from collectors.cot import collect_and_store as cot_collect
    from collectors.jodi import collect_and_store as jodi_collect

    scheduler = BlockingScheduler(timezone="UTC")

    # --- Yahoo Finance jobs ---
    # 1-minute bars: run every minute, fetch last day
    scheduler.add_job(
        safe_run,
        "interval",
        minutes=1,
        args=[yf_collect, "1m", "1d"],
        id="yahoo_1m",
        name="Yahoo 1-minute OHLCV",
        max_instances=1,
        coalesce=True,
    )

    # 5-minute bars: run every 5 minutes, fetch last 5 days
    scheduler.add_job(
        safe_run,
        "interval",
        minutes=5,
        args=[yf_collect, "5m", "5d"],
        id="yahoo_5m",
        name="Yahoo 5-minute OHLCV",
        max_instances=1,
        coalesce=True,
    )

    # 15-minute bars: run every 15 minutes, fetch last 5 days
    scheduler.add_job(
        safe_run,
        "interval",
        minutes=15,
        args=[yf_collect, "15m", "5d"],
        id="yahoo_15m",
        name="Yahoo 15-minute OHLCV",
        max_instances=1,
        coalesce=True,
    )

    # 1-hour bars: run every hour, fetch last 5 days
    scheduler.add_job(
        safe_run,
        "interval",
        hours=1,
        args=[yf_collect, "1h", "5d"],
        id="yahoo_1h",
        name="Yahoo 1-hour OHLCV",
        max_instances=1,
        coalesce=True,
    )

    # 1-day bars: run every 6 hours, fetch last 30 days
    scheduler.add_job(
        safe_run,
        "interval",
        hours=6,
        args=[yf_collect, "1d", "1mo"],
        id="yahoo_1d",
        name="Yahoo 1-day OHLCV",
        max_instances=1,
        coalesce=True,
    )

    # Stooq ICE Brent snapshot DISABLED — we switched to Yahoo CL=F (NYMEX WTI
    # front-month) as single price source. Stooq only served CB.F flat-tick bars
    # anyway; WTI CL=F has real OHLC historical depth from Yahoo. collectors/stooq.py
    # kept in repo for reference.

    # --- Alpha Vantage jobs ---
    # DISABLED: Alpha Vantage TIME_SERIES_INTRADAY with symbol "BZ" returns
    # Kanzhun Limited (stock), not Brent crude. AV's BRENT commodity function
    # only provides monthly/weekly spot — not intraday. Yahoo covers our needs.
    # scheduler.add_job(..., av_collect, "5min", id="av_5min", ...)

    # --- Macro / fundamental jobs (only the ones that actually work for free) ---
    from collectors.eia import collect_and_store as eia_collect
    from collectors.fred import collect_and_store as fred_collect

    scheduler.add_job(
        safe_run, "interval", hours=6, args=[eia_collect],
        id="eia", name="EIA crude inventories", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=12, args=[fred_collect],
        id="fred", name="FRED macro series", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=24, args=[cot_collect],
        id="cot", name="CFTC COT (cftc.gov)", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=24, args=[jodi_collect],
        id="jodi", name="JODI Oil World", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=24, args=[portwatch_collect],
        id="portwatch", name="IMF PortWatch (ArcGIS)", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=6, args=[shipping_collect],
        id="shipping", name="Datalastic AIS (skipped without key)", max_instances=1, coalesce=True,
    )

    # STILL DISABLED:
    #   - OPEC MOMR HTML (403 — Cloudflare/Akamai blocks all bot UAs)
    #     would require headless Playwright + JS execution to bypass.

    # Log all scheduled jobs
    logger.info("Scheduled jobs:")
    for job in scheduler.get_jobs():
        logger.info("  • %s (id=%s, trigger=%s)", job.name, job.id, job.trigger)

    # Warm up: immediately fetch all timeframes so the dashboard and analyzer
    # have data on first load.
    logger.info("Warming up — fetching 1m, 5m, 15m, 1h, 1d, 1wk Yahoo bars …")
    safe_run(yf_collect, "1m", "1d")
    safe_run(yf_collect, "5m", "5d")
    safe_run(yf_collect, "15m", "5d")
    safe_run(yf_collect, "1h", "5d")
    safe_run(yf_collect, "1d", "1mo")
    safe_run(yf_collect, "1wk", "2y")

    # Warm up macro / shipping collectors so the analyzer has fundamental
    # and shipping data on first cycle (instead of waiting hours).
    logger.info("Warming up macro and shipping collectors …")
    safe_run(eia_collect)
    safe_run(fred_collect)
    safe_run(cot_collect)
    safe_run(jodi_collect)
    safe_run(portwatch_collect)
    safe_run(shipping_collect)
    logger.info("Warm-up complete.")

    logger.info("Starting scheduler …")
    scheduler.start()


if __name__ == "__main__":
    main()

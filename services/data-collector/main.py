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
    from collectors.binance import collect_and_store as bn_collect
    from collectors.binance_ws import start_binance_ws
    from collectors.binance_metrics import (
        collect_all_metrics as bn_metrics_all,
        collect_funding_rate as bn_funding,
    )
    from collectors.binance_liquidations_ws import start_liquidations_ws
    from collectors.yahoo_wti import collect_and_store as yahoo_wti_collect
    from collectors.cross_assets import collect_and_store as cross_assets_collect
    from collectors.shipping import collect_and_store as shipping_collect
    from collectors.portwatch import collect_and_store as portwatch_collect
    from collectors.cot import collect_and_store as cot_collect
    from collectors.jodi import collect_and_store as jodi_collect

    scheduler = BlockingScheduler(timezone="UTC")

    # --- Binance USD-M Futures jobs (CLUSDT TRADIFI_PERPETUAL — tracks NYMEX WTI) ---
    # REST klines for historical backfill. The WebSocket worker (below) handles
    # the live current-bar updates in real time.
    scheduler.add_job(
        safe_run, "interval", minutes=1, args=[bn_collect, "1m", 500],
        id="binance_1m", name="Binance 1m klines", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", minutes=5, args=[bn_collect, "5m", 500],
        id="binance_5m", name="Binance 5m klines", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", minutes=15, args=[bn_collect, "15m", 500],
        id="binance_15m", name="Binance 15m klines", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=1, args=[bn_collect, "1h", 500],
        id="binance_1h", name="Binance 1h klines", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=4, args=[bn_collect, "4h", 500],
        id="binance_4h", name="Binance 4h klines", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=6, args=[bn_collect, "1d", 500],
        id="binance_1d", name="Binance 1d klines", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=24, args=[bn_collect, "1w", 200],
        id="binance_1w", name="Binance 1w klines", max_instances=1, coalesce=True,
    )

    # WebSocket live stream — subscribes to kline_1m for real-time tick updates.
    # Runs in a daemon background thread, not on the scheduler.
    start_binance_ws()
    start_liquidations_ws()

    # --- Yahoo CL=F (PRIMARY price feed — matches XTB OIL.WTI) ---
    # NYMEX WTI front-month future, pulled via yfinance. The Binance
    # CLUSDT perpetual (collected above) drifts 1-3% from real NYMEX
    # during low-liquidity hours, so we use Yahoo for all scoring /
    # chart / scalping decisions and keep Binance only for funding,
    # open interest, liquidations, and other derivatives metrics.
    scheduler.add_job(
        safe_run, "interval", minutes=1, args=[yahoo_wti_collect, "1m", "1d"],
        id="yahoo_wti_1m", name="Yahoo CL=F 1-min",
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", minutes=5, args=[yahoo_wti_collect, "5m", "5d"],
        id="yahoo_wti_5m", name="Yahoo CL=F 5-min",
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", minutes=15, args=[yahoo_wti_collect, "15m", "5d"],
        id="yahoo_wti_15m", name="Yahoo CL=F 15-min",
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=1, args=[yahoo_wti_collect, "1h", "5d"],
        id="yahoo_wti_1h", name="Yahoo CL=F 1-hour",
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        safe_run, "interval", hours=6, args=[yahoo_wti_collect, "1d", "1mo"],
        id="yahoo_wti_1d", name="Yahoo CL=F 1-day",
        max_instances=1, coalesce=True,
    )

    # --- Binance derived metrics (OI, funding, long/short, taker flow) ---
    # Fast-cadence metrics: every 5 min.
    scheduler.add_job(
        safe_run, "interval", minutes=5, args=[bn_metrics_all],
        id="binance_metrics", name="Binance derived metrics (OI/LSR/taker)",
        max_instances=1, coalesce=True,
    )
    # Funding rate: rarely changes (8h exchange cadence), poll every 30 min.
    scheduler.add_job(
        safe_run, "interval", minutes=30, args=[bn_funding, 500],
        id="binance_funding", name="Binance funding rate history",
        max_instances=1, coalesce=True,
    )

    # Cross-asset context (DXY / SPX / Gold / BTC / VIX) — 15 min cadence
    scheduler.add_job(
        safe_run, "interval", minutes=15, args=[cross_assets_collect, "1h", "5d"],
        id="cross_assets", name="Cross-asset correlations",
        max_instances=1, coalesce=True,
    )

    # --- Yahoo Finance DISABLED — replaced by Binance CLUSDT (better data) ---
    # collectors/yahoo.py kept in repo as reference only. See commit that
    # migrated to Binance.

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
    # have data on first load. Binance klines come back quickly (<1s each).
    logger.info("Warming up — fetching Binance CLUSDT klines 1m/5m/15m/1h/4h/1d/1w …")
    safe_run(bn_collect, "1m", 1000)
    safe_run(bn_collect, "5m", 1000)
    safe_run(bn_collect, "15m", 1000)
    safe_run(bn_collect, "1h", 1000)
    safe_run(bn_collect, "4h", 500)
    safe_run(bn_collect, "1d", 500)
    safe_run(bn_collect, "1w", 200)

    # Warm up Binance derived metrics
    logger.info("Warming up Binance metrics (OI, LSR, taker, funding) …")
    safe_run(bn_funding, 500)
    safe_run(bn_metrics_all)

    # Warm up Yahoo CL=F (primary WTI price feed)
    logger.info("Warming up Yahoo CL=F feed …")
    safe_run(yahoo_wti_collect, "1m", "1d")
    safe_run(yahoo_wti_collect, "5m", "5d")
    safe_run(yahoo_wti_collect, "15m", "5d")
    safe_run(yahoo_wti_collect, "1h", "5d")
    safe_run(yahoo_wti_collect, "1d", "1mo")

    # Warm up cross-asset collectors (DXY / SPX / Gold / BTC / VIX)
    logger.info("Warming up cross-asset collectors …")
    safe_run(cross_assets_collect, "1h", "5d")

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

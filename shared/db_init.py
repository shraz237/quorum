"""Database initialisation: create tables, TimescaleDB extension, hypertables.

Run with:
    python -m shared.db_init
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from shared.models.base import Base, engine

logger = logging.getLogger(__name__)

# Tables that should become hypertables (they all have a 'timestamp' column).
_HYPERTABLES: list[str] = [
    "ohlcv",
    "macro_eia",
    "macro_cot",
    "macro_fred",
    "macro_jodi",
    "macro_opec",
    "sentiment_news",
    "sentiment_twitter",
    "analysis_scores",
    "ai_recommendations",
    "shipping_positions",
    "shipping_metrics",
]

# Compression policy: compress chunks older than this many days.
_COMPRESS_AFTER_DAYS = 30


def init_db() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with engine.connect() as conn:
        # Enable the TimescaleDB extension (no-op if already enabled).
        logger.info("Enabling TimescaleDB extension …")
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;"))
        conn.commit()

    # Create all ORM-mapped tables.
    logger.info("Creating tables …")
    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        for table in _HYPERTABLES:
            # create_hypertable raises an error if the table is already a
            # hypertable, so we use if_not_exists => TRUE.
            logger.info("Creating hypertable: %s", table)
            conn.execute(
                text(
                    f"SELECT create_hypertable('{table}', 'timestamp', "
                    f"if_not_exists => TRUE);"
                )
            )
            conn.commit()

            # Add a compression policy.
            logger.info("Adding compression policy for: %s (%d days)", table, _COMPRESS_AFTER_DAYS)
            try:
                conn.execute(
                    text(
                        f"ALTER TABLE {table} SET ("
                        f"timescaledb.compress, "
                        f"timescaledb.compress_orderby = 'timestamp DESC'"
                        f");"
                    )
                )
                conn.execute(
                    text(
                        f"SELECT add_compression_policy('{table}', "
                        f"INTERVAL '{_COMPRESS_AFTER_DAYS} days', "
                        f"if_not_exists => TRUE);"
                    )
                )
                conn.commit()
            except Exception as exc:
                # Non-fatal — compression may already be configured.
                logger.warning("Compression policy for %s skipped: %s", table, exc)
                conn.rollback()

    with engine.connect() as conn:
        # ------------------------------------------------------------------
        # Continuous aggregates
        # ------------------------------------------------------------------

        # Daily OHLCV rollup from 1-minute bars.
        logger.info("Creating continuous aggregate: ohlcv_daily …")
        try:
            conn.execute(
                text(
                    """
                    CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_daily
                    WITH (timescaledb.continuous) AS
                    SELECT
                        time_bucket('1 day', timestamp) AS bucket,
                        source,
                        first(open, timestamp)           AS open,
                        max(high)                        AS high,
                        min(low)                         AS low,
                        last(close, timestamp)           AS close,
                        sum(volume)                      AS volume
                    FROM ohlcv
                    WHERE timeframe = '1min'
                    GROUP BY bucket, source
                    WITH NO DATA;
                    """
                )
            )
            conn.commit()
        except Exception as exc:
            logger.warning("ohlcv_daily aggregate skipped: %s", exc)
            conn.rollback()

        # Hourly analysis scores rollup.
        logger.info("Creating continuous aggregate: scores_hourly …")
        try:
            conn.execute(
                text(
                    """
                    CREATE MATERIALIZED VIEW IF NOT EXISTS scores_hourly
                    WITH (timescaledb.continuous) AS
                    SELECT
                        time_bucket('1 hour', timestamp) AS bucket,
                        avg(technical_score)             AS technical_score,
                        avg(fundamental_score)           AS fundamental_score,
                        avg(sentiment_score)             AS sentiment_score,
                        avg(shipping_score)              AS shipping_score,
                        avg(unified_score)               AS unified_score
                    FROM analysis_scores
                    GROUP BY bucket
                    WITH NO DATA;
                    """
                )
            )
            conn.commit()
        except Exception as exc:
            logger.warning("scores_hourly aggregate skipped: %s", exc)
            conn.rollback()

        # ------------------------------------------------------------------
        # Refresh policies for continuous aggregates
        # ------------------------------------------------------------------

        logger.info("Adding refresh policy for ohlcv_daily …")
        try:
            conn.execute(
                text(
                    """
                    SELECT add_continuous_aggregate_policy(
                        'ohlcv_daily',
                        start_offset  => INTERVAL '3 days',
                        end_offset    => INTERVAL '1 hour',
                        schedule_interval => INTERVAL '1 hour',
                        if_not_exists => TRUE
                    );
                    """
                )
            )
            conn.commit()
        except Exception as exc:
            logger.warning("ohlcv_daily refresh policy skipped: %s", exc)
            conn.rollback()

        logger.info("Adding refresh policy for scores_hourly …")
        try:
            conn.execute(
                text(
                    """
                    SELECT add_continuous_aggregate_policy(
                        'scores_hourly',
                        start_offset  => INTERVAL '3 days',
                        end_offset    => INTERVAL '1 hour',
                        schedule_interval => INTERVAL '1 hour',
                        if_not_exists => TRUE
                    );
                    """
                )
            )
            conn.commit()
        except Exception as exc:
            logger.warning("scores_hourly refresh policy skipped: %s", exc)
            conn.rollback()

    logger.info("Database initialisation complete.")


if __name__ == "__main__":
    init_db()

"""Database initialisation: create tables, TimescaleDB extension, hypertables.

Run with:
    python -m shared.db_init
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from shared.models.base import Base, engine, SessionLocal
# Import all models so they register with Base.metadata before create_all
import shared.models  # noqa: F401 — side-effect import to register all ORM classes

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
    "binance_liquidations",
    "binance_open_interest",
    "binance_long_short_ratios",
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
            # TimescaleDB requires the partitioning column (timestamp) to be
            # part of any unique index/primary key. Our SQLAlchemy models use
            # `id` as the PK, so we need to convert it to a composite PK
            # (id, timestamp) before calling create_hypertable.
            logger.info("Adjusting primary key on: %s", table)
            try:
                # Check if PK already includes timestamp — skip if so.
                result = conn.execute(text(f"""
                    SELECT a.attname FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                    WHERE i.indrelid = '{table}'::regclass AND i.indisprimary
                """)).fetchall()
                pk_columns = {r[0] for r in result}
                if "timestamp" in pk_columns:
                    logger.debug("PK on %s already includes timestamp, skipping", table)
                else:
                    conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pkey;"))
                    conn.execute(text(f"ALTER TABLE {table} ADD PRIMARY KEY (id, timestamp);"))
                    conn.commit()
            except Exception as exc:
                logger.warning("PK adjust for %s skipped: %s", table, exc)
                conn.rollback()

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

    # ------------------------------------------------------------------
    # Migrate legacy positions & initialise account row
    # ------------------------------------------------------------------
    _migrate_legacy_positions()
    _ensure_account_row()

    logger.info("Database initialisation complete.")


def _migrate_legacy_positions() -> None:
    """Assign legacy open positions (campaign_id=NULL) to new Campaign rows.

    Also backfills lots/margin_used/nominal_value with conservative defaults:
    lots=1, margin_used = entry_price * 100 / 10, layer_index=0.
    These are tagged with notes="legacy migration".
    """
    from shared.models.positions import Position
    from shared.models.campaigns import Campaign

    with SessionLocal() as session:
        legacy = (
            session.query(Position)
            .filter(Position.status == "open", Position.campaign_id.is_(None))
            .all()
        )
        if not legacy:
            logger.info("No legacy positions to migrate.")
            return

        logger.info("Migrating %d legacy position(s) to campaigns…", len(legacy))
        for pos in legacy:
            # Create a campaign for this legacy position
            campaign = Campaign(
                opened_at=pos.opened_at or datetime.now(tz=timezone.utc),
                side=pos.side,
                status="open",
                max_loss_pct=50.0,
                notes="legacy migration",
            )
            session.add(campaign)
            session.flush()

            # Backfill sizing: lots=1, margin = entry*100/10, nominal = entry*100
            lots = 1.0
            margin = (pos.entry_price * 100) / 10
            nominal = pos.entry_price * 100

            pos.campaign_id = campaign.id
            pos.lots = lots
            pos.margin_used = margin
            pos.nominal_value = nominal
            pos.layer_index = 0
            pos.notes = ((pos.notes + "\n") if pos.notes else "") + "legacy migration"

            logger.info(
                "Migrated position #%s → campaign #%s (%s @ %.2f, lots=1)",
                pos.id, campaign.id, pos.side, pos.entry_price,
            )

        session.commit()


def _ensure_account_row() -> None:
    """Create the singleton account row if it doesn't exist yet."""
    from shared.account_manager import get_or_create_account
    try:
        get_or_create_account()
        logger.info("Account row ensured.")
    except Exception as exc:
        logger.warning("Could not ensure account row: %s", exc)


if __name__ == "__main__":
    init_db()

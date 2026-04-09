"""Yahoo Finance WTI collector — primary price source (CL=F).

CL=F is the real NYMEX WTI front-month future. It matches XTB's
OIL.WTI symbol within a broker spread (~$0.02-0.05). We use this as
the PRIMARY price feed for the chart, scoring engine, scalping
analyzer, and all trade decisions.

Binance CLUSDT is kept as a SECONDARY feed only for its unique
derivatives metrics (funding rate, open interest, long/short ratios,
liquidations, orderbook) and the live header ticker — NOT for pricing
decisions, because the TRADIFI perpetual drifts 1-3% from real NYMEX
during low-liquidity hours (we observed $2.66 = 2.75% at one point).
"""

from __future__ import annotations

import logging
from datetime import timezone

import yfinance as yf
from sqlalchemy.dialects.postgresql import insert as pg_insert

from shared.models.base import SessionLocal
from shared.models.ohlcv import OHLCV
from shared.redis_streams import publish
from shared.schemas.events import PriceEvent

logger = logging.getLogger(__name__)

# Maps yfinance interval strings to internal timeframe labels
INTERVAL_MAP: dict[str, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "1h": "1H",
    "1d": "1D",
    "1wk": "1W",
}

_TICKER = "CL=F"
_STREAM = "prices.brent"  # legacy stream name — carries WTI now
_SOURCE = "yahoo"


def fetch_wti_ohlcv(interval: str = "1h", period: str = "1d") -> list[dict]:
    """Download CL=F OHLCV bars from Yahoo Finance."""
    timeframe = INTERVAL_MAP.get(interval, interval)
    try:
        df = yf.download(
            _TICKER,
            interval=interval,
            period=period,
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.warning("yfinance download failed for %s/%s: %s", interval, period, exc)
        return []

    if df.empty:
        logger.warning("yfinance empty for %s/%s", interval, period)
        return []

    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)

    records: list[dict] = []
    for ts, row in df.iterrows():
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            aware_ts = ts.to_pydatetime()
        else:
            aware_ts = ts.to_pydatetime().replace(tzinfo=timezone.utc)
        try:
            records.append({
                "timestamp": aware_ts,
                "source": _SOURCE,
                "timeframe": timeframe,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]) if "Volume" in row and row["Volume"] is not None else None,
            })
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping malformed row: %s (%s)", row, exc)

    logger.info(
        "Fetched %d Yahoo CL=F bars (interval=%s period=%s)",
        len(records), interval, period,
    )
    return records


def collect_and_store(interval: str = "1h", period: str = "5d") -> None:
    """Fetch and upsert WTI OHLCV bars."""
    records = fetch_wti_ohlcv(interval=interval, period=period)
    if not records:
        return

    with SessionLocal() as session:
        stmt = pg_insert(OHLCV).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "timeframe", "timestamp"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        session.execute(stmt)
        session.commit()

    logger.info("Upserted %d Yahoo CL=F rows (interval=%s)", len(records), interval)

    # Publish latest bar so the analyzer runs on Yahoo price changes too
    latest = records[-1]
    try:
        event = PriceEvent(**latest)
        publish(_STREAM, event.model_dump())
    except Exception:
        logger.exception("Failed to publish PriceEvent")

"""Yahoo Finance collector for WTI crude oil (CL=F) OHLCV data.

CL=F is the real NYMEX WTI front-month future — matches XTB OIL.WTI CFD
virtually 1:1 (no drift). We switched from BZ=F (Brent NYMEX BLDF) because
that was a derivative contract that drifted $0.30-$1.00 from ICE Brent.
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
_STREAM = "prices.brent"  # legacy stream name, kept for backward-compat (carries WTI now)


def fetch_brent_ohlcv(interval: str = "1h", period: str = "1d") -> list[dict]:
    """Download CL=F (WTI front-month) OHLCV bars from Yahoo Finance.

    Args:
        interval: yfinance interval string (e.g. "1m", "1h", "1d").
        period: yfinance period string (e.g. "1d", "5d", "1mo").

    Returns:
        List of dicts with keys: timestamp, source, timeframe, open, high,
        low, close, volume.
    """
    timeframe = INTERVAL_MAP.get(interval, interval)
    df = yf.download(
        _TICKER,
        interval=interval,
        period=period,
        progress=False,
        auto_adjust=True,
    )

    if df.empty:
        logger.warning("yfinance returned empty DataFrame for interval=%s period=%s", interval, period)
        return []

    # yfinance may return a MultiIndex when downloading a single ticker with
    # auto_adjust=True in newer versions — flatten if needed.
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)

    records: list[dict] = []
    for ts, row in df.iterrows():
        # Ensure timezone-aware UTC timestamp
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            aware_ts = ts.to_pydatetime()
        else:
            aware_ts = ts.to_pydatetime().replace(tzinfo=timezone.utc)

        records.append(
            {
                "timestamp": aware_ts,
                "source": "yahoo",
                "timeframe": timeframe,
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]) if "Volume" in row and row["Volume"] is not None else None,
            }
        )

    logger.info("Fetched %d bars from Yahoo Finance (interval=%s)", len(records), interval)
    return records


def collect_and_store(interval: str = "1h", period: str = "1d") -> None:
    """Fetch OHLCV data, persist to DB, and publish to Redis stream."""
    records = fetch_brent_ohlcv(interval=interval, period=period)
    if not records:
        return

    # Upsert by (source, timeframe, timestamp). For existing bars we refresh
    # open/high/low/close/volume because the current (unfinished) bar will
    # change until it closes.
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

    logger.info("Upserted %d OHLCV rows (interval=%s)", len(records), interval)

    # Publish the most recent bar as a PriceEvent
    latest = records[-1]
    event = PriceEvent(**latest)
    publish(_STREAM, event.model_dump())
    logger.info("Published PriceEvent to stream '%s' (interval=%s)", _STREAM, interval)

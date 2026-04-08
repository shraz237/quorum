"""Binance futures derived-metrics collectors.

Pulls four Binance-only signals that give an edge over any free
commodity data source:

  - funding rate history (8h cadence, published by the exchange)
  - open interest history (5-min cadence)
  - top trader long/short position ratio  (smart money positioning)
  - global account long/short ratio        (retail crowd positioning)
  - taker long/short volume ratio          (aggressive flow direction)

All endpoints are public — no API key required. Data is upserted into
shared.models.binance_metrics tables. Scheduled cadence is 5 minutes for
live metrics; funding rate runs every 30 minutes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests
from sqlalchemy.dialects.postgresql import insert as pg_insert

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.binance_metrics import (
    BinanceFundingRate,
    BinanceLongShortRatio,
    BinanceOpenInterest,
)

logger = logging.getLogger(__name__)

_BASE = "https://fapi.binance.com"
_REQUEST_TIMEOUT = 15


def _symbol() -> str:
    return (settings.binance_symbol or "CLUSDT").upper()


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _get_json(path: str, params: dict) -> list | dict | None:
    try:
        r = requests.get(f"{_BASE}{path}", params=params, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.error("Binance GET %s failed: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Funding rate (8h cadence, we pull last 1000 which ≈ 333 days)
# ---------------------------------------------------------------------------

def collect_funding_rate(limit: int = 500) -> None:
    data = _get_json("/fapi/v1/fundingRate", {"symbol": _symbol(), "limit": limit})
    if not isinstance(data, list) or not data:
        return

    records: list[dict] = []
    for row in data:
        try:
            records.append({
                "symbol": row["symbol"],
                "funding_time": _ms_to_dt(row["fundingTime"]),
                "funding_rate": float(row["fundingRate"]),
                "mark_price": float(row["markPrice"]) if row.get("markPrice") else None,
            })
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping malformed funding rate row: %s (%s)", row, exc)

    if not records:
        return

    with SessionLocal() as session:
        stmt = pg_insert(BinanceFundingRate).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "funding_time"],
            set_={
                "funding_rate": stmt.excluded.funding_rate,
                "mark_price": stmt.excluded.mark_price,
            },
        )
        session.execute(stmt)
        session.commit()

    logger.info("Upserted %d funding rate rows (%s)", len(records), _symbol())


# ---------------------------------------------------------------------------
# Open interest history (5-min cadence)
# ---------------------------------------------------------------------------

def collect_open_interest(period: str = "5m", limit: int = 500) -> None:
    """Pull OI history. Period ∈ {5m,15m,30m,1h,2h,4h,6h,12h,1d}. Max limit 500."""
    data = _get_json(
        "/futures/data/openInterestHist",
        {"symbol": _symbol(), "period": period, "limit": limit},
    )
    if not isinstance(data, list) or not data:
        return

    records: list[dict] = []
    for row in data:
        try:
            records.append({
                "symbol": row["symbol"],
                "timestamp": _ms_to_dt(row["timestamp"]),
                "open_interest": float(row["sumOpenInterest"]),
                "open_interest_value_usd": (
                    float(row["sumOpenInterestValue"])
                    if row.get("sumOpenInterestValue") else None
                ),
            })
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping malformed OI row: %s (%s)", row, exc)

    if not records:
        return

    with SessionLocal() as session:
        stmt = pg_insert(BinanceOpenInterest).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "timestamp"],
            set_={
                "open_interest": stmt.excluded.open_interest,
                "open_interest_value_usd": stmt.excluded.open_interest_value_usd,
            },
        )
        session.execute(stmt)
        session.commit()

    logger.info("Upserted %d OI rows (%s, %s)", len(records), _symbol(), period)


# ---------------------------------------------------------------------------
# Long/short ratios — three sources, unified table
# ---------------------------------------------------------------------------

def _collect_lsr(path: str, ratio_type: str, period: str, limit: int) -> None:
    data = _get_json(
        path, {"symbol": _symbol(), "period": period, "limit": limit},
    )
    if not isinstance(data, list) or not data:
        return

    records: list[dict] = []
    for row in data:
        try:
            rec: dict = {
                "symbol": row.get("symbol") or _symbol(),
                "ratio_type": ratio_type,
                "timestamp": _ms_to_dt(row["timestamp"]),
                "long_pct": None,
                "short_pct": None,
                "long_short_ratio": 0.0,
                "buy_volume": None,
                "sell_volume": None,
            }

            if ratio_type in ("top_position", "global_account"):
                rec["long_pct"] = float(row["longAccount"])
                rec["short_pct"] = float(row["shortAccount"])
                rec["long_short_ratio"] = float(row["longShortRatio"])
            elif ratio_type == "taker":
                rec["buy_volume"] = float(row["buyVol"])
                rec["sell_volume"] = float(row["sellVol"])
                rec["long_short_ratio"] = float(row["buySellRatio"])

            records.append(rec)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping malformed LSR row (%s): %s (%s)", ratio_type, row, exc)

    if not records:
        return

    with SessionLocal() as session:
        stmt = pg_insert(BinanceLongShortRatio).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "ratio_type", "timestamp"],
            set_={
                "long_pct": stmt.excluded.long_pct,
                "short_pct": stmt.excluded.short_pct,
                "long_short_ratio": stmt.excluded.long_short_ratio,
                "buy_volume": stmt.excluded.buy_volume,
                "sell_volume": stmt.excluded.sell_volume,
            },
        )
        session.execute(stmt)
        session.commit()

    logger.info(
        "Upserted %d LSR rows (%s, %s, %s)",
        len(records), _symbol(), ratio_type, period,
    )


def collect_top_long_short(period: str = "5m", limit: int = 500) -> None:
    _collect_lsr(
        "/futures/data/topLongShortPositionRatio",
        ratio_type="top_position",
        period=period,
        limit=limit,
    )


def collect_global_long_short(period: str = "5m", limit: int = 500) -> None:
    _collect_lsr(
        "/futures/data/globalLongShortAccountRatio",
        ratio_type="global_account",
        period=period,
        limit=limit,
    )


def collect_taker_ratio(period: str = "5m", limit: int = 500) -> None:
    _collect_lsr(
        "/futures/data/takerlongshortRatio",
        ratio_type="taker",
        period=period,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Unified entry point for the scheduler
# ---------------------------------------------------------------------------

def collect_all_metrics() -> None:
    """Run one pass of all fast-cadence metric collectors."""
    collect_open_interest(period="5m", limit=500)
    collect_top_long_short(period="5m", limit=500)
    collect_global_long_short(period="5m", limit=500)
    collect_taker_ratio(period="5m", limit=500)

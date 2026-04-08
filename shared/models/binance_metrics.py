"""Binance futures derived-metrics models.

Four tables capturing Binance-only signals that we cannot get from any
other free source:

  funding_rates     — 8h funding rate history for the perpetual
  open_interest     — total open contracts over time (5-min cadence)
  long_short_ratios — positioning stats (top traders, global, taker flow)
  liquidations      — live liquidation events from the @forceOrder WS stream

All times are UTC. Symbol is always the configured binance_symbol (CLUSDT
by default) — we store it explicitly so we can later track multiple pairs.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class BinanceFundingRate(Base):
    __tablename__ = "binance_funding_rates"
    __table_args__ = (
        UniqueConstraint("symbol", "funding_time", name="uq_funding_symbol_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    funding_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    funding_rate: Mapped[float] = mapped_column(Float, nullable=False)
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)


class BinanceOpenInterest(Base):
    __tablename__ = "binance_open_interest"
    __table_args__ = (
        UniqueConstraint("symbol", "timestamp", name="uq_oi_symbol_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    open_interest: Mapped[float] = mapped_column(Float, nullable=False)
    open_interest_value_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


class BinanceLongShortRatio(Base):
    """Unified table for all long/short positioning ratios.

    ratio_type values:
      - "top_position"   — top trader long/short by POSITION weight (smart money)
      - "global_account" — all accounts by count (retail)
      - "taker"          — aggressive taker buy/sell volume ratio
    """

    __tablename__ = "binance_long_short_ratios"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "ratio_type", "timestamp",
            name="uq_lsr_symbol_type_time",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    ratio_type: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    long_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    short_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    long_short_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    buy_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_volume: Mapped[float | None] = mapped_column(Float, nullable=True)


class BinanceLiquidation(Base):
    """Individual liquidation events from the @forceOrder WebSocket stream.

    No unique constraint — Binance may replay events and we want ALL of them
    so we can draw cluster markers. Dedup on read by (symbol, side, price,
    timestamp) if needed.
    """

    __tablename__ = "binance_liquidations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY or SELL
    price: Mapped[float] = mapped_column(Float, nullable=False)
    orig_qty: Mapped[float] = mapped_column(Float, nullable=False)
    executed_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    quote_qty_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_status: Mapped[str | None] = mapped_column(String(16), nullable=True)


Index(
    "ix_liquidations_symbol_ts",
    BinanceLiquidation.symbol,
    BinanceLiquidation.timestamp.desc(),
)

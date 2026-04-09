"""Signal snapshots — periodic capture of every feature we track.

Used for:
  - historical pattern matching (find similar past moments by feature vector)
  - signal performance tracking (forward returns after each snapshot)

Schema is flat (one column per feature) for fast similarity queries.
Stored once every SNAPSHOT_INTERVAL_MINUTES by a background worker.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Index
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class SignalSnapshot(Base):
    __tablename__ = "signal_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True, unique=True,
    )

    # Spot state
    price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Analysis scores (-100..+100)
    technical: Mapped[float | None] = mapped_column(Float, nullable=True)
    fundamental: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping: Mapped[float | None] = mapped_column(Float, nullable=True)
    unified: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Binance derived metrics
    funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_trader_long_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    global_retail_long_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    taker_buysell_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Order book
    orderbook_imbalance_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Forward returns — populated by a background job after the horizon elapses
    forward_return_1h_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    forward_return_4h_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    forward_return_24h_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


Index("ix_signal_snapshots_ts_desc", SignalSnapshot.timestamp.desc())

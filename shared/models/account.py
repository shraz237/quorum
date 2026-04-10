"""Account model — one row per persona (main + scalper).

Two independent trading personas share the same table but have separate
balances, P/L tracking, and margin accounting:

  main    — conservative, DCA campaigns, managed by Opus Heartbeat + committee
  scalper — aggressive, fast in-and-out, managed by Scalp Brain auto-executor
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class Account(Base):
    """One row per persona — each has its own balance, margin, P/L."""
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 'main' or 'scalper' — each persona is an independent trader
    persona: Mapped[str] = mapped_column(String(16), nullable=False, default="main", unique=True, index=True)
    starting_balance: Mapped[float] = mapped_column(Float, nullable=False, default=50000.0)
    cash: Mapped[float] = mapped_column(Float, nullable=False, default=50000.0)
    realized_pnl_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

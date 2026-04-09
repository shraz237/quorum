"""Campaign model — a directional DCA trading campaign."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class Campaign(Base):
    """A directional trading campaign — one or more DCA layers on the same side.

    Statuses:
        open              — campaign is active
        closed_tp         — closed by take-profit
        closed_sl         — closed by stop-loss
        closed_manual     — closed via dashboard
        closed_strategy   — closed by AI strategy decision
        closed_hard_stop  — closed by the -50% drawdown hard-stop guard
    """
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # LONG | SHORT
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    # open | closed_tp | closed_sl | closed_manual | closed_strategy | closed_hard_stop
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)

    max_loss_pct: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    # Campaign-level take-profit price. When set and price crosses it, the entire campaign
    # is auto-closed with status=closed_tp. ALTER TABLE campaigns ADD COLUMN take_profit FLOAT;
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # set on close
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Trade journal snapshots — captured at open and close for feedback-loop
    # analytics. Free-form JSONB so we can evolve the shape without migrations.
    # Typical entry_snapshot keys: scores, conviction, funding, oi, orderbook,
    # whale_delta, volume_profile, reason, current_price.
    # Typical exit_snapshot keys: same plus max_favorable_excursion_usd,
    # max_adverse_excursion_usd, duration_minutes, ai_review.
    entry_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    exit_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

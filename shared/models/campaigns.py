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

    # Which persona owns this campaign — 'main' or 'scalper'
    persona: Mapped[str] = mapped_column(String(16), nullable=False, default="main", index=True)

    # LONG | SHORT
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    # open | closed_tp | closed_sl | closed_manual | closed_strategy | closed_hard_stop
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", index=True)

    max_loss_pct: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    # Campaign-level take-profit price. When set and price crosses it, the entire campaign
    # is auto-closed with status=closed_tp. ALTER TABLE campaigns ADD COLUMN take_profit FLOAT;
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Campaign-level stop-loss price. When set and price crosses it, the entire campaign
    # is auto-closed with status=closed_sl. Separate from max_loss_pct which is a
    # drawdown-% based emergency brake.
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # set on close
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Dynamic size multiplier applied to every DCA layer of this campaign.
    # Computed once at open from current market state; 0.5 .. 3.0.
    # Rows created before this column was added default to 1.0.
    size_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    # JSONB with the full sizing reasoning (base, reasons, state inputs)
    # so the dashboard can show "why the bot sized this way".
    sizing_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Trade journal snapshots — captured at open and close for feedback-loop
    # analytics. Free-form JSONB so we can evolve the shape without migrations.
    # Typical entry_snapshot keys: scores, conviction, funding, oi, orderbook,
    # whale_delta, volume_profile, reason, current_price.
    # Typical exit_snapshot keys: same plus max_favorable_excursion_usd,
    # max_adverse_excursion_usd, duration_minutes, ai_review.
    entry_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    exit_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

"""Forward-looking thesis — a conditional trading plan that triggers
on a future market condition and then tracks its outcome.

Two independent domains share this table, separated by the `domain` column:

  campaign — theses for the main campaign system. Created by the user
             (via chat or dashboard form), by the Opus heartbeat manager,
             or by the ai-brain score event flow. When triggered they
             notify the user who can decide whether to open a campaign.

  scalp    — theses created by the scalp brain itself when it's in a
             LEAN_LONG or LEAN_SHORT state. These are the scalper's own
             "if this signal completes, I'd go" notes — they let the
             scalp bot accumulate a personal learning corpus over time
             without ever touching the main campaign system.

Lifecycle:
  pending    — waiting for trigger condition
  triggered  — trigger fired, waiting for resolution window to close
  expired    — created with expires_at that passed without a trigger
  cancelled  — user cancelled explicitly
  resolved   — outcome recorded (correct / wrong / partial / unresolved)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class Thesis(Base):
    __tablename__ = "theses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # When + who
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # "user" | "user_chat" | "user_form" | "heartbeat" | "scalp_brain" | "ai_brain"

    # Which domain — separates scalp from campaign so stats roll up independently
    domain: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="campaign")
    # "campaign" | "scalp"

    # Description
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    thesis_text: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Rich context snapshot — everything the creator thought was relevant
    # at creation time, so resolved theses can be compared against the
    # state that motivated them. Deep/verbose on purpose (user requested
    # "all thoughts should be deeply written for later review").
    context_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Trigger
    # price_cross_above | price_cross_below | score_above | score_below |
    # time_elapsed | news_keyword | scalp_brain_state | manual
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trigger_params: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Optional explicit expiry (auto-mark expired if trigger never fires)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Planned action if/when the trigger fires
    # LONG | SHORT | CLOSE_EXISTING | WATCH | NONE
    planned_action: Mapped[str] = mapped_column(String(16), nullable=False, default="WATCH")
    planned_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_size_margin: Mapped[float | None] = mapped_column(Float, nullable=True)

    # How outcome gets evaluated after trigger
    # "fixed_window" — resolve_at = triggered_at + resolution_window_minutes
    # "tp_or_sl_first" — resolve when price crosses planned TP or SL first
    outcome_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="tp_or_sl_first")
    resolution_window_minutes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=240)  # 4h

    # Status + trigger snapshot
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # pending | triggered | expired | cancelled | resolved
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    triggered_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    triggered_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Outcome
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # correct | wrong | partial | unresolved
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    outcome_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # price at resolve time
    outcome_hypothetical_pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # What the P/L would have been if the plan had been executed
    outcome_max_favorable_excursion: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_max_adverse_excursion: Mapped[float | None] = mapped_column(Float, nullable=True)

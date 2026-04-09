"""Heartbeat run audit log — one row per Opus heartbeat decision.

Every 15 minutes (configurable) the ai-brain heartbeat worker asks
Claude Opus 4.6 to review every open campaign. Opus returns one of
`hold` / `close` / `update_levels` per campaign, and each decision is
persisted here for audit, backtest, and the dashboard panel.

Rows with `campaign_id IS NULL` are "tick summary" rows — used when the
worker ran but had no open campaigns, or when a tick errored out before
any per-campaign decision could be made.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class HeartbeatRun(Base):
    __tablename__ = "heartbeat_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # Nullable: null for tick-summary rows (no open campaigns, errors, skipped)
    campaign_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    # hold | close | update_levels | skipped | error
    decision: Mapped[str] = mapped_column(String(20), nullable=False)

    # Opus's rationale (short human-readable reason)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Full Opus tool-call JSON for audit
    opus_raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Did the action actually run? False when guardrails blocked it
    # (cooldown, indecision, validation fail) — row is still logged.
    executed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # End-to-end tick duration (full heartbeat, not per-campaign)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

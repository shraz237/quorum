"""WatchSession model — tracks active live-monitoring sessions.

DB migration SQL (orchestrator will run):

    CREATE TABLE watch_sessions (
        id BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        ended_at TIMESTAMPTZ,
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        focus VARCHAR(8) NOT NULL DEFAULT 'EITHER',
        cycle_seconds INTEGER NOT NULL DEFAULT 30,
        question TEXT,
        telegram_chat_id BIGINT,
        telegram_message_id BIGINT,
        last_tick_at TIMESTAMPTZ,
        tick_count INTEGER NOT NULL DEFAULT 0,
        last_price DOUBLE PRECISION,
        last_unified_score DOUBLE PRECISION
    );
    CREATE INDEX ix_watch_sessions_status ON watch_sessions(status);
    CREATE INDEX ix_watch_sessions_expires_at ON watch_sessions(expires_at);
"""

from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from shared.models.base import Base


class WatchSession(Base):
    """An active live-monitoring session. Background worker updates it every cycle."""

    __tablename__ = "watch_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # active | expired | stopped
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)

    # LONG | SHORT | EITHER
    focus: Mapped[str] = mapped_column(String(8), nullable=False, default="EITHER")

    # How often to tick, in seconds (default 30, min 15, max 300)
    cycle_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # User's question / context (what are they watching for)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Telegram chat_id and message_id of the editable card (set by notifier on first update)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Running state
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tick_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_unified_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)

"""LLM usage audit log — one row per Anthropic/OpenAI/xAI call.

Every call site across the bot logs token usage and estimated cost here.
Dashboard rolls this up into per-day / per-site / per-model breakdowns
so the user can see exactly where tokens (and dollars) are going.

Schema is deliberately flat — no joins needed for the common rollup
queries. Cost is computed at insert time from the current pricing
table in shared/llm_usage.py, so historical rows stay valid even if
we change pricing later.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    # Where the call came from
    service: Mapped[str] = mapped_column(String(32), nullable=False, index=True)     # ai-brain | dashboard | sentiment
    call_site: Mapped[str] = mapped_column(String(64), nullable=False, index=True)   # e.g. heartbeat.opus, now_brief.haiku
    model: Mapped[str] = mapped_column(String(64), nullable=False, index=True)       # exact model id
    provider: Mapped[str] = mapped_column(String(16), nullable=False)                # anthropic | openai | xai

    # Token counts — raw from the provider's usage object
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Cost computed at insert time from shared/llm_usage.py pricing table
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Round-trip latency — handy for debugging + spotting slow calls
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Did the call succeed? Failed calls still get logged (with 0 tokens)
    # so the rate of failures is visible in the dashboard.
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

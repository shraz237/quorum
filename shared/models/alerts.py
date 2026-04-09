"""Alert model — user-defined price / keyword / score alerts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class Alert(Base):
    """User-defined alert that fires when a condition is met."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # price | keyword | score | smart
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # active | triggered | cancelled
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", index=True
    )

    # For kind=price: price target and direction ("above"|"below")
    price_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_direction: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # For kind=keyword: substring to match against knowledge digests / sentiment news titles
    keyword: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # For kind=score: which component, threshold, direction
    score_component: Mapped[str | None] = mapped_column(String(32), nullable=True)
    score_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_direction: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )  # "above"|"below"|"crosses"

    # For kind=smart: a tree of conditions combined with AND/OR.
    # Shape:
    #   {"op": "AND", "clauses": [
    #       {"metric": "funding_rate_pct", "cmp": "<=", "value": -0.03},
    #       {"metric": "orderbook_imbalance_pct", "cmp": ">=", "value": 30},
    #       {"op": "OR", "clauses": [
    #           {"metric": "unified_score", "cmp": "<=", "value": -20},
    #           {"metric": "retail_delta_pct", "cmp": ">=", "value": 15}
    #       ]}
    #   ]}
    expression: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Message the user wants attached to the alert
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Fire-once or repeating
    one_shot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    triggered_value: Mapped[float | None] = mapped_column(Float, nullable=True)

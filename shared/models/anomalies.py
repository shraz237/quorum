"""Anomaly log table — persistent history of extreme market events detected.

Every time the detector fires for a new category, a row is appended here so
the user can review "what weird stuff happened in the last 24h / week".
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class Anomaly(Base):
    __tablename__ = "anomalies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..10
    direction: Mapped[str] = mapped_column(String(16), nullable=False)  # BULL / BEAR / NEUTRAL
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    metric_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)


Index(
    "ix_anomalies_category_detected",
    Anomaly.category,
    Anomaly.detected_at.desc(),
)

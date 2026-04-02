"""Health monitoring utilities for the Brent crude trading bot.

Checks data freshness for each key data source and generates alert messages
when sources fall behind their expected update schedules.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from shared.models.base import engine

logger = logging.getLogger(__name__)

# Maximum acceptable age for each data source before it is considered stale.
FRESHNESS_THRESHOLDS: dict[str, timedelta] = {
    "ohlcv": timedelta(minutes=5),
    "eia": timedelta(days=8),
    "sentiment": timedelta(hours=1),
    "scores": timedelta(minutes=30),
}

# Maps logical source names to the SQL query that returns the latest timestamp.
_FRESHNESS_QUERIES: dict[str, str] = {
    "ohlcv": "SELECT MAX(timestamp) FROM ohlcv",
    "eia": "SELECT MAX(timestamp) FROM macro_eia",
    "sentiment": "SELECT MAX(timestamp) FROM sentiment_news",
    "scores": "SELECT MAX(timestamp) FROM analysis_scores",
}


def check_data_freshness() -> dict[str, dict]:
    """Query each source for its most recent record and compare to thresholds.

    Returns
    -------
    dict
        Keyed by source name.  Each value is a dict with:
          - ``latest``: datetime | None — timestamp of most recent record
          - ``age``: timedelta | None — how old that record is
          - ``threshold``: timedelta — acceptable maximum age
          - ``healthy``: bool — True when age <= threshold (or data is very new)
    """
    now = datetime.now(tz=timezone.utc)
    status: dict[str, dict] = {}

    with engine.connect() as conn:
        for source, query in _FRESHNESS_QUERIES.items():
            threshold = FRESHNESS_THRESHOLDS[source]
            try:
                row = conn.execute(text(query)).fetchone()
                latest: datetime | None = row[0] if row else None
            except Exception as exc:
                logger.warning("Freshness query failed for %s: %s", source, exc)
                latest = None

            if latest is None:
                age = None
                healthy = False
            else:
                # Ensure timezone-aware comparison.
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=timezone.utc)
                age = now - latest
                healthy = age <= threshold

            status[source] = {
                "latest": latest,
                "age": age,
                "threshold": threshold,
                "healthy": healthy,
            }

    return status


def generate_health_alerts(status: dict[str, dict]) -> list[str]:
    """Produce human-readable alert messages for any unhealthy sources.

    Parameters
    ----------
    status:
        Output of :func:`check_data_freshness`.

    Returns
    -------
    list[str]
        One alert string per unhealthy source (empty list when all healthy).
    """
    alerts: list[str] = []

    for source, info in status.items():
        if info["healthy"]:
            continue

        threshold = info["threshold"]
        latest = info["latest"]
        age = info["age"]

        if latest is None:
            alerts.append(
                f"[ALERT] {source.upper()}: no data found in the database."
            )
        else:
            # Format age as a readable string.
            total_seconds = int(age.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                age_str = f"{hours}h {minutes}m"
            elif minutes:
                age_str = f"{minutes}m {seconds}s"
            else:
                age_str = f"{seconds}s"

            threshold_seconds = int(threshold.total_seconds())
            t_hours, t_rem = divmod(threshold_seconds, 3600)
            t_minutes, _ = divmod(t_rem, 60)
            if t_hours:
                threshold_str = f"{t_hours}h {t_minutes}m"
            else:
                threshold_str = f"{t_minutes}m"

            alerts.append(
                f"[ALERT] {source.upper()}: last update {age_str} ago "
                f"(threshold {threshold_str}). Latest record: {latest.isoformat()}."
            )

    return alerts

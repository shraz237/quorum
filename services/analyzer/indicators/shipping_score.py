"""Shipping/AIS sub-score computed from ShippingMetric rows.

Bullish drivers (price goes up):
  - Decreasing floating storage (less idle laden tonnage)
  - Increased traffic through chokepoints heading TO importer hubs
  - Port congestion at major export terminals (supply disruption)

Bearish drivers (price goes down):
  - Rising floating storage (oversupply)
  - Reduced traffic through chokepoints
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, desc

from shared.models.base import SessionLocal
from shared.models.shipping import ShippingMetric

logger = logging.getLogger(__name__)

# Freshness gate: shipping data must be newer than 14 days (2× expected weekly cadence)
SHIPPING_FRESHNESS_THRESHOLD = timedelta(days=14)


def _clamp(v: float, lo: float = -100, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def compute_shipping_score() -> float | None:
    """Aggregate shipping metrics into a -100..+100 score.

    Reads ShippingMetric rows from the last 7 days, groups by metric_name,
    takes the latest value per metric, and applies domain-specific scoring rules.

    Returns None if no recent shipping data is available or all data is stale
    beyond SHIPPING_FRESHNESS_THRESHOLD.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)

    with SessionLocal() as session:
        rows = session.scalars(
            select(ShippingMetric)
            .where(ShippingMetric.timestamp >= cutoff)
            .order_by(desc(ShippingMetric.timestamp))
            .limit(200)
        ).all()

    if not rows:
        logger.info("No shipping metrics in the last 7 days — skipping shipping score")
        return None

    # Additional freshness gate: reject if the newest row is too old
    newest_ts = rows[0].timestamp.replace(tzinfo=timezone.utc)
    age = datetime.now(tz=timezone.utc) - newest_ts
    if age > SHIPPING_FRESHNESS_THRESHOLD:
        logger.warning("Shipping data is stale (age=%s) — returning None", age)
        return None

    # Group by metric_name, take latest value (rows already ordered desc by timestamp)
    latest_by_metric: dict[str, float] = {}
    for r in rows:
        if r.metric_name not in latest_by_metric and r.value is not None:
            latest_by_metric[r.metric_name] = r.value

    score = 0.0
    n_components = 0

    # ------------------------------------------------------------------
    # Floating storage: high = bearish (oversupply parked at sea)
    # ------------------------------------------------------------------
    fs = latest_by_metric.get("floating_storage")
    if fs is not None:
        # Each VLCC of floating storage = -2 points (max 30 vessels = -60)
        score += _clamp(-fs * 2, -60, 60)
        n_components += 1
        logger.debug("Floating storage=%.1f → component score=%.1f", fs, _clamp(-fs * 2, -60, 60))

    # ------------------------------------------------------------------
    # Strait of Hormuz traffic: low = supply disruption = bullish
    # ~10-20 vessels is normal; below 5 is significant disruption
    # ------------------------------------------------------------------
    hormuz = latest_by_metric.get("hormuz_traffic")
    if hormuz is not None:
        if hormuz < 5:
            score += 40
        elif hormuz < 10:
            score += 20
        n_components += 1
        logger.debug("Hormuz traffic=%.1f → n_components incremented", hormuz)

    # ------------------------------------------------------------------
    # PortWatch tanker counts at major export terminals
    # Higher than baseline (~50) = oversupply concentration → mildly bearish
    # Lower than baseline = disruption → bullish
    # ------------------------------------------------------------------
    portwatch_keys = [k for k in latest_by_metric if k.startswith("portwatch_tanker_")]
    if portwatch_keys:
        avg_traffic = sum(latest_by_metric[k] for k in portwatch_keys) / len(portwatch_keys)
        if avg_traffic < 30:
            score += 25  # disruption at export terminals — bullish
        elif avg_traffic > 70:
            score -= 15  # oversupply loading at terminals — mildly bearish
        n_components += 1
        logger.debug(
            "PortWatch avg traffic=%.1f over %d ports → n_components incremented",
            avg_traffic,
            len(portwatch_keys),
        )

    if n_components == 0:
        logger.info("No recognised shipping metrics found in DB — returning None")
        return None

    final = _clamp(score)
    logger.info(
        "Shipping score: %.1f (from %d component(s), raw accumulation=%.1f)",
        final,
        n_components,
        score,
    )
    return final

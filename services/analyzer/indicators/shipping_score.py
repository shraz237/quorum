"""Shipping/AIS sub-score computed from ShippingMetric rows — z-score version.

Replaces the old hardcoded-threshold version (2026-04) that was producing a
stuck -15 shipping score because portwatch returns cumulative visit counts
in the thousands rather than point-in-time tanker counts (avg of
US-HOU + SG + NL-RTM = ~13,000 >>> old threshold of 70 → -15 every cycle).

New model: for each metric, compute a rolling z-score over the last 30
days of history, then map deviations to bullish / bearish contributions
using a domain-specific sign:

  floating_storage       — HIGH is oversupply     → +z is BEARISH (sign -1)
  hormuz_traffic         — LOW is disruption      → -z is BULLISH (sign -1)
  portwatch_tanker_*     — HIGH is loading glut   → +z is BEARISH (sign -1)
  chokepoint_throughput  — HIGH is fluid supply   → +z is BEARISH (sign -1)

Deviations < 0.5σ contribute nothing (noise band). Beyond that we scale
linearly up to ±40 points per metric, and cap the total at ±100.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev

from sqlalchemy import select, desc

from shared.models.base import SessionLocal
from shared.models.shipping import ShippingMetric

logger = logging.getLogger(__name__)

# Freshness gate: shipping data must be newer than 14 days
SHIPPING_FRESHNESS_THRESHOLD = timedelta(days=14)

# Lookback window for computing the rolling mean/stdev baseline
LOOKBACK_DAYS = 30

# Minimum number of prior samples we need to trust a z-score
MIN_SAMPLES = 10

# Z-score thresholds
NEUTRAL_BAND = 0.5       # |z| < 0.5 → no contribution (noise)
MAX_Z = 3.0              # |z| >= 3.0 clamps the per-metric contribution
MAX_COMPONENT = 40.0     # max points per metric at |z|=MAX_Z

# Sign convention: negative = bearish for oil price
# For a metric whose HIGH reading is bearish (oversupply), sign = -1.
# For a metric whose HIGH reading is bullish (tightness), sign = +1.
_METRIC_SIGNS: dict[str, int] = {
    "floating_storage": -1,
    "hormuz_traffic": +1,            # HIGH = normal flow; low = disruption = bullish
    "chokepoint_throughput": -1,
    # Port tanker counts — HIGH = oversupply loading at export terminals
    # Prefix match below for portwatch_tanker_* and similar
}


def _clamp(v: float, lo: float = -100, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def _metric_sign(name: str) -> int:
    if name in _METRIC_SIGNS:
        return _METRIC_SIGNS[name]
    if name.startswith("portwatch_tanker_") or name.startswith("port_tanker_"):
        return -1
    if name.startswith("chokepoint_"):
        return -1
    return -1  # conservative default: treat unknown metrics as bearish on HIGH


def _contribution_from_z(z: float, sign: int) -> float:
    """Map a z-score + metric sign to a per-component contribution in
    [-MAX_COMPONENT, +MAX_COMPONENT]."""
    abs_z = abs(z)
    if abs_z < NEUTRAL_BAND:
        return 0.0
    # Linear scale from NEUTRAL_BAND to MAX_Z into [0, MAX_COMPONENT]
    scaled = min(abs_z, MAX_Z) / MAX_Z
    magnitude = scaled * MAX_COMPONENT
    direction = -1 if z > 0 else 1  # positive z = above average
    # sign convention: sign=-1 means "high is bearish", so when z>0 we want -magnitude
    return direction * magnitude * (-sign)


def compute_shipping_score() -> float | None:
    """Aggregate shipping metrics into a -100..+100 score via z-score scaling.

    Returns None if no recent shipping data exists or all data is stale.
    """
    now = datetime.now(tz=timezone.utc)
    history_cutoff = now - timedelta(days=LOOKBACK_DAYS)
    recent_cutoff = now - timedelta(days=7)

    with SessionLocal() as session:
        # Pull 30-day history once, then we'll partition in Python
        history = session.scalars(
            select(ShippingMetric)
            .where(ShippingMetric.timestamp >= history_cutoff)
            .order_by(desc(ShippingMetric.timestamp))
        ).all()

    if not history:
        logger.info("No shipping metrics in the last %d days — skipping score", LOOKBACK_DAYS)
        return None

    # Newest row must be within freshness window
    newest_ts = history[0].timestamp
    if newest_ts.tzinfo is None:
        newest_ts = newest_ts.replace(tzinfo=timezone.utc)
    if (now - newest_ts) > SHIPPING_FRESHNESS_THRESHOLD:
        logger.warning("Shipping data stale (newest=%s) — returning None", newest_ts)
        return None

    # Partition by metric_name
    by_metric: dict[str, list[ShippingMetric]] = {}
    for r in history:
        if r.value is None:
            continue
        by_metric.setdefault(r.metric_name, []).append(r)

    total_score = 0.0
    components: list[str] = []

    for metric_name, rows in by_metric.items():
        # Values are newest-first; compute baseline from the EARLIER samples
        # so the current reading is compared to its own history.
        if len(rows) < MIN_SAMPLES + 1:
            continue

        latest_row = rows[0]
        if (now - latest_row.timestamp.replace(tzinfo=timezone.utc)) > timedelta(days=7):
            continue  # metric hasn't refreshed recently

        baseline_values = [r.value for r in rows[1 : MIN_SAMPLES * 3 + 1]]
        if len(baseline_values) < MIN_SAMPLES:
            continue
        mu = mean(baseline_values)
        sigma = pstdev(baseline_values)
        if sigma <= 0:
            continue  # flat signal, no information

        z = (latest_row.value - mu) / sigma
        sign = _metric_sign(metric_name)
        contrib = _contribution_from_z(z, sign)

        logger.debug(
            "shipping[%s]: latest=%.1f μ=%.1f σ=%.1f z=%.2f sign=%+d → %+.1f",
            metric_name, latest_row.value, mu, sigma, z, sign, contrib,
        )

        if contrib != 0:
            total_score += contrib
            components.append(f"{metric_name}(z={z:+.2f})")

    if not components:
        logger.info(
            "No shipping metrics with meaningful deviation — returning 0 (neutral)",
        )
        return 0.0

    final = _clamp(total_score)
    logger.info(
        "Shipping score: %.1f (from %d component(s): %s)",
        final, len(components), ", ".join(components),
    )
    return final

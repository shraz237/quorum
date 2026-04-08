"""Conviction meter plugin — composite decision-support score.

Combines multiple signals into a single 0..100 "conviction" number plus
a direction (BULL/BEAR/MIXED) and the top drivers behind the reading.

Inputs (all read from DB / fresh tool calls):
  - Unified analysis score (primary weight 40%)
  - Technical score (secondary weight 20%)
  - Recent score momentum (60 min delta, weight 10%)
  - Funding rate extreme flag (weight 10%)
  - Long/short retail-vs-smart-money delta (weight 10%)
  - Active alerts count (weight 5%)
  - Breaking news flag (weight 5%)

Output shape:
{
  "score": 62.3,                 # 0..100 composite
  "direction": "BEAR",            # BULL / BEAR / MIXED
  "label": "Medium Signal",
  "color": "yellow",
  "drivers": [
    {"name": "Unified score", "value": -18.5, "contribution": -7.4},
    ...
  ],
  "as_of": "2026-04-08T23:45:12Z"
}
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func

from shared.models.base import SessionLocal
from shared.models.binance_metrics import (
    BinanceFundingRate,
    BinanceLongShortRatio,
)
from shared.models.signals import AnalysisScore
from shared.models.alerts import Alert
from shared.models.knowledge import KnowledgeSummary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Band helpers
# ---------------------------------------------------------------------------

def _signal_strength_label(score: float) -> tuple[str, str]:
    """Return (label, tailwind-color-name) for a 0..100 signal strength."""
    if score >= 80:
        return ("EXTREME", "red")
    if score >= 60:
        return ("Strong", "orange")
    if score >= 40:
        return ("Medium", "yellow")
    if score >= 20:
        return ("Mild", "emerald")
    return ("Quiet", "gray")


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_conviction() -> dict:
    """Assemble the composite conviction reading.

    Returns the JSON-ready dict described in the module docstring.
    """
    now = datetime.now(tz=timezone.utc)
    drivers: list[dict] = []

    # Direction accumulator: sum of signed contributions
    signed_sum = 0.0
    # Strength accumulator: sum of |contributions|
    abs_sum = 0.0

    with SessionLocal() as session:
        # 1. Latest unified score (primary, weight 40)
        latest = (
            session.query(AnalysisScore)
            .order_by(desc(AnalysisScore.timestamp))
            .first()
        )
        unified = latest.unified_score if latest else None
        technical = latest.technical_score if latest else None

        if unified is not None:
            # Normalise -100..+100 to a signed contribution of weight 40
            contrib = (unified / 100.0) * 40.0
            signed_sum += contrib
            abs_sum += abs(contrib)
            drivers.append({
                "name": "Unified score",
                "value": round(unified, 1),
                "contribution": round(contrib, 1),
            })

        # 2. Technical score (secondary, weight 20)
        if technical is not None:
            contrib = (technical / 100.0) * 20.0
            signed_sum += contrib
            abs_sum += abs(contrib)
            drivers.append({
                "name": "Technical",
                "value": round(technical, 1),
                "contribution": round(contrib, 1),
            })

        # 3. Score momentum — delta from ~60 min ago (weight 10)
        an_hour_ago = now - timedelta(minutes=60)
        old_score_row = (
            session.query(AnalysisScore)
            .filter(AnalysisScore.timestamp <= an_hour_ago)
            .order_by(desc(AnalysisScore.timestamp))
            .first()
        )
        if old_score_row is not None and unified is not None and old_score_row.unified_score is not None:
            delta = unified - old_score_row.unified_score
            # A 20-point swing in an hour is strong — scale accordingly.
            contrib = max(-10.0, min(10.0, delta / 2.0))
            signed_sum += contrib
            abs_sum += abs(contrib)
            drivers.append({
                "name": "60-min momentum",
                "value": round(delta, 1),
                "contribution": round(contrib, 1),
            })

        # 4. Funding rate extreme (weight 10)
        fr = (
            session.query(BinanceFundingRate)
            .order_by(desc(BinanceFundingRate.funding_time))
            .first()
        )
        if fr is not None:
            # Funding beyond +/- 0.02% (per 8h period) is meaningful.
            # Sign is contrarian: positive funding (longs paying) is a mild
            # bearish signal and vice versa — so we NEGATE the raw rate.
            rate_pct = fr.funding_rate * 100
            contrib = max(-10.0, min(10.0, -rate_pct * 30.0))
            if abs(contrib) >= 1.0:
                signed_sum += contrib
                abs_sum += abs(contrib)
                drivers.append({
                    "name": "Funding extreme",
                    "value": round(rate_pct, 4),
                    "contribution": round(contrib, 1),
                })

        # 5. Retail vs smart money delta (weight 10)
        top = (
            session.query(BinanceLongShortRatio)
            .filter(BinanceLongShortRatio.ratio_type == "top_position")
            .order_by(desc(BinanceLongShortRatio.timestamp))
            .first()
        )
        glob = (
            session.query(BinanceLongShortRatio)
            .filter(BinanceLongShortRatio.ratio_type == "global_account")
            .order_by(desc(BinanceLongShortRatio.timestamp))
            .first()
        )
        if top and glob and top.long_pct is not None and glob.long_pct is not None:
            delta_pct = (glob.long_pct - top.long_pct) * 100
            # Positive delta = retail more long than smart money = contrarian bear.
            contrib = max(-10.0, min(10.0, -delta_pct / 2.0))
            if abs(contrib) >= 1.0:
                signed_sum += contrib
                abs_sum += abs(contrib)
                drivers.append({
                    "name": "Retail vs smart money",
                    "value": round(delta_pct, 1),
                    "contribution": round(contrib, 1),
                })

        # 6. Active alerts (weight 5) — any alert recently triggered adds
        # strength (direction unknown, so this adds to |contribution| only).
        recent_alerts = (
            session.query(func.count(Alert.id))
            .filter(
                Alert.triggered_at >= now - timedelta(hours=2),
            )
            .scalar() or 0
        )
        if recent_alerts > 0:
            contrib_abs = min(5.0, recent_alerts * 2.0)
            abs_sum += contrib_abs
            drivers.append({
                "name": "Recent alerts",
                "value": recent_alerts,
                "contribution": round(contrib_abs, 1),
            })

        # 7. Breaking news — any urgent KnowledgeSummary in last 30 min (weight 5)
        recent_news = (
            session.query(KnowledgeSummary)
            .filter(
                KnowledgeSummary.timestamp >= now - timedelta(minutes=30),
            )
            .order_by(desc(KnowledgeSummary.timestamp))
            .first()
        )
        if recent_news is not None:
            abs_sum += 5.0
            drivers.append({
                "name": "Breaking news",
                "value": (recent_news.summary or "")[:60],
                "contribution": 5.0,
            })

    # Normalise strength to 0..100. Max theoretical |sum| is ~90 (all weights),
    # but in practice 60 is very high. Clip at 100.
    strength = min(100.0, abs_sum)
    direction = "MIXED"
    if strength >= 15:
        direction = "BULL" if signed_sum > 0 else "BEAR" if signed_sum < 0 else "MIXED"

    label, color = _signal_strength_label(strength)

    # Sort drivers by absolute contribution so the top factors surface first
    drivers.sort(key=lambda d: abs(float(d.get("contribution", 0))), reverse=True)

    return {
        "score": round(strength, 1),
        "signed_score": round(signed_sum, 1),
        "direction": direction,
        "label": label,
        "color": color,
        "drivers": drivers[:5],
        "as_of": now.isoformat(),
    }

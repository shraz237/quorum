"""Fundamental indicator scoring for Brent crude oil analysis."""

from __future__ import annotations

import logging
import math
import statistics
from datetime import timedelta

logger = logging.getLogger(__name__)


def _clamp(value: float, lo: float = -100.0, hi: float = 100.0) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


def _zscore_to_score(z: float, scale: float = 30.0) -> float:
    """Convert a z-score to a -100..+100 score. z=±2 → ~±60, z=±3 → ~±90, z=±4 → ±100."""
    return _clamp(z * scale)


def _rolling_zscore(values: list[float], current: float) -> float | None:
    """Compute z-score of *current* relative to *values* (the lookback window).

    Returns None if the window has fewer than 5 values or std is zero.
    """
    if len(values) < 5:
        return None
    try:
        mean = statistics.mean(values)
        std = statistics.stdev(values)
    except statistics.StatisticsError:
        return None
    if std == 0.0 or math.isnan(std):
        return None
    return (current - mean) / std


# ---------------------------------------------------------------------------
# Individual indicator scorers
# ---------------------------------------------------------------------------


def score_eia_inventory(change: float, history: list[float] | None = None) -> float:
    """Score EIA crude inventory change using a rolling z-score.

    Draw (negative change) = bullish (positive score).
    Build (positive change) = bearish (negative score).

    If *history* has >= 5 values, computes z = (change - mean) / std over the
    lookback, then maps z → score via _zscore_to_score (negated so draw = bullish).
    Falls back to the original /50 divisor formula when history is insufficient.

    # TODO: use TimescaleDB continuous aggregates for performance once data grows.
    """
    if history is not None and len(history) >= 5:
        z = _rolling_zscore(history, change)
        if z is not None:
            # Negate: a draw (negative change) that is below the mean is bullish (+)
            return _zscore_to_score(-z)

    # Cold-start fallback: original magic-divisor formula
    return _clamp(-change / 50.0)


def score_cot_positioning(net: float, history: list[float] | None = None) -> float:
    """Score CFTC COT net speculator positioning using a rolling z-score.

    Net long = bullish (positive score).

    If *history* has >= 5 values, computes z = (net - mean) / std over the
    lookback, then maps z → score.  Falls back to the original /5000 divisor.

    # TODO: use TimescaleDB continuous aggregates for performance once data grows.
    """
    if history is not None and len(history) >= 5:
        z = _rolling_zscore(history, net)
        if z is not None:
            return _zscore_to_score(z)

    # Cold-start fallback: original magic-divisor formula
    return _clamp(net / 5000.0)


def score_usd(current: float, previous: float, change_history: list[float] | None = None) -> float:
    """Score USD strength relative to a previous value using a rolling z-score.

    Stronger USD = bearish for oil (negative score).

    If *change_history* has >= 5 values (past daily changes), computes z of the
    current change relative to historical changes, then negates (stronger USD →
    bearish).  Falls back to the original pct_change * 30 formula.

    # TODO: use TimescaleDB continuous aggregates for performance once data grows.
    """
    if previous == 0:
        return 0.0

    current_change = current - previous

    if change_history is not None and len(change_history) >= 5:
        z = _rolling_zscore(change_history, current_change)
        if z is not None:
            # Negate: a stronger USD (positive change) is bearish
            return _zscore_to_score(-z)

    # Cold-start fallback: original pct-change formula
    pct_change = current_change / previous * 100.0
    return _clamp(-pct_change * 30.0)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_latest_eia() -> tuple[float, list[float]] | None:
    """Return (latest_change, history) for EIA crude inventory change.

    history is the last 26 values (excluding the latest), used for z-score
    normalisation.  Returns None if no data available.

    Freshness gate: returns None if the latest row is older than 14 days.
    """
    from datetime import datetime, timezone
    from shared.models import SessionLocal, MacroEIA
    from sqlalchemy import select

    session = SessionLocal()
    try:
        stmt = (
            select(MacroEIA)
            .where(MacroEIA.crude_inventory_change.isnot(None))
            .order_by(MacroEIA.timestamp.desc())
            .limit(27)  # 1 current + 26 history
        )
        rows = session.execute(stmt).scalars().all()
    finally:
        session.close()

    if not rows:
        return None

    # Freshness check: 14-day threshold (2× weekly cadence)
    age = datetime.now(timezone.utc) - rows[0].timestamp.replace(tzinfo=timezone.utc)
    if age > timedelta(days=14):
        logger.warning("EIA data is stale (age=%s) — returning None", age)
        return None

    latest = float(rows[0].crude_inventory_change)
    history = [float(r.crude_inventory_change) for r in rows[1:]]
    return latest, history


def get_latest_cot() -> tuple[float, list[float]] | None:
    """Return (latest_net, history) for COT non-commercial net positioning.

    history is the last 52 values (excluding the latest), used for z-score
    normalisation.  Returns None if no data available.

    Freshness gate: returns None if the latest row is older than 14 days.
    """
    from datetime import datetime, timezone
    from shared.models import SessionLocal, MacroCOT
    from sqlalchemy import select

    session = SessionLocal()
    try:
        stmt = (
            select(MacroCOT)
            .where(MacroCOT.non_commercial_long.isnot(None))
            .where(MacroCOT.non_commercial_short.isnot(None))
            .order_by(MacroCOT.timestamp.desc())
            .limit(53)  # 1 current + 52 history
        )
        rows = session.execute(stmt).scalars().all()
    finally:
        session.close()

    if not rows:
        return None

    # Freshness check: 14-day threshold (2× weekly cadence)
    age = datetime.now(timezone.utc) - rows[0].timestamp.replace(tzinfo=timezone.utc)
    if age > timedelta(days=14):
        logger.warning("COT data is stale (age=%s) — returning None", age)
        return None

    def _net(r: MacroCOT) -> float:
        return (r.non_commercial_long or 0.0) - (r.non_commercial_short or 0.0)

    latest_net = _net(rows[0])
    history = [_net(r) for r in rows[1:]]
    return latest_net, history


def get_latest_usd() -> tuple[float, float, list[float]] | None:
    """Return (current, previous, change_history) for USD index (DTWEXBGS).

    change_history is the day-over-day changes for the last 30 values,
    used for z-score normalisation.  Returns None if insufficient data.

    Freshness gate: returns None if the latest row is older than 3 days.
    """
    from datetime import datetime, timezone
    from shared.models import SessionLocal, MacroFRED
    from sqlalchemy import select

    DXY_SERIES = ["DTWEXBGS", "DTWEXM", "DXY"]

    session = SessionLocal()
    try:
        rows_all = None
        for series_id in DXY_SERIES:
            stmt = (
                select(MacroFRED)
                .where(MacroFRED.series_id == series_id)
                .where(MacroFRED.value.isnot(None))
                .order_by(MacroFRED.timestamp.desc())
                .limit(32)  # enough for 30 historical day-changes
            )
            rows = session.execute(stmt).scalars().all()
            if len(rows) >= 2:
                rows_all = rows
                break
    finally:
        session.close()

    if rows_all is None or len(rows_all) < 2:
        return None

    # Freshness check: 3-day threshold
    age = datetime.now(timezone.utc) - rows_all[0].timestamp.replace(tzinfo=timezone.utc)
    if age > timedelta(days=3):
        logger.warning("FRED USD data is stale (age=%s) — returning None", age)
        return None

    current = float(rows_all[0].value)
    previous = float(rows_all[1].value)

    # Build list of day-over-day changes from the historical window (excluding current)
    values = [float(r.value) for r in rows_all]
    change_history = [values[i] - values[i + 1] for i in range(1, len(values) - 1)]

    return current, previous, change_history


# ---------------------------------------------------------------------------
# Composite fundamental score
# ---------------------------------------------------------------------------


def compute_fundamental_score() -> float | None:
    """Compute weighted fundamental score.

    Weights: eia=0.40, cot=0.30, usd=0.30.
    Returns a score in [-100, 100] or None if no data is available.
    Each sub-score uses a rolling z-score (26-week EIA, 52-week COT, 30-day FRED)
    with a cold-start fallback to the original divisor formula if fewer than 5
    historical rows are available.
    """
    weights = {"eia": 0.40, "cot": 0.30, "usd": 0.30}
    scores: dict[str, float | None] = {}

    # EIA
    try:
        eia_result = get_latest_eia()
        if eia_result is not None:
            change, history = eia_result
            scores["eia"] = score_eia_inventory(change, history)
    except Exception:
        logger.exception("Error fetching/scoring EIA data")

    # COT
    try:
        cot_result = get_latest_cot()
        if cot_result is not None:
            net, history = cot_result
            scores["cot"] = score_cot_positioning(net, history)
    except Exception:
        logger.exception("Error fetching/scoring COT data")

    # USD
    try:
        usd_result = get_latest_usd()
        if usd_result is not None:
            current, previous, change_history = usd_result
            scores["usd"] = score_usd(current, previous, change_history)
    except Exception:
        logger.exception("Error fetching/scoring USD data")

    total_weight = 0.0
    weighted_sum = 0.0
    for key, weight in weights.items():
        val = scores.get(key)
        if val is not None:
            weighted_sum += val * weight
            total_weight += weight

    if total_weight == 0:
        return None

    return _clamp(weighted_sum / total_weight)

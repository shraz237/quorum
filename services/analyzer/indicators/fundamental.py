"""Fundamental indicator scoring for Brent crude oil analysis."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _clamp(value: float, lo: float = -100.0, hi: float = 100.0) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Individual indicator scorers
# ---------------------------------------------------------------------------


def score_eia_inventory(change: float) -> float:
    """Score EIA crude inventory change.

    Draw (negative change) = bullish (positive score).
    Build (positive change) = bearish (negative score).
    Scale: -change * 10 (e.g. -5M barrel draw → +50).
    """
    return _clamp(-change * 10.0)


def score_cot_positioning(net: float) -> float:
    """Score CFTC COT net speculator positioning.

    Net long = bullish (positive score).
    Scale: net / 3000 (e.g. +60k net long → +20).
    """
    return _clamp(net / 3000.0)


def score_usd(current: float, previous: float) -> float:
    """Score USD strength relative to a previous value.

    Stronger USD = bearish for oil (negative score).
    Scale: -pct_change * 30.
    """
    if previous == 0:
        return 0.0
    pct_change = (current - previous) / previous * 100.0
    return _clamp(-pct_change * 30.0)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_latest_eia() -> float | None:
    """Return the most recent EIA crude inventory change (thousand barrels)."""
    from shared.models import SessionLocal, MacroEIA
    from sqlalchemy import select

    session = SessionLocal()
    try:
        stmt = (
            select(MacroEIA)
            .where(MacroEIA.crude_inventory_change.isnot(None))
            .order_by(MacroEIA.timestamp.desc())
            .limit(1)
        )
        row = session.execute(stmt).scalars().first()
        if row is not None:
            return row.crude_inventory_change
        return None
    finally:
        session.close()


def get_latest_cot() -> float | None:
    """Return the most recent COT net speculator position (non_commercial_long - non_commercial_short)."""
    from shared.models import SessionLocal, MacroCOT
    from sqlalchemy import select

    session = SessionLocal()
    try:
        stmt = (
            select(MacroCOT)
            .where(MacroCOT.non_commercial_long.isnot(None))
            .where(MacroCOT.non_commercial_short.isnot(None))
            .order_by(MacroCOT.timestamp.desc())
            .limit(1)
        )
        row = session.execute(stmt).scalars().first()
        if row is None:
            return None
        return (row.non_commercial_long or 0.0) - (row.non_commercial_short or 0.0)
    finally:
        session.close()


def get_latest_usd() -> tuple[float, float] | None:
    """Return (current, previous) DXY values from FRED (series_id='DTWEXBGS' or 'DTWEXM').

    Returns None if insufficient data.
    """
    from shared.models import SessionLocal, MacroFRED
    from sqlalchemy import select

    DXY_SERIES = ["DTWEXBGS", "DTWEXM", "DXY"]

    session = SessionLocal()
    try:
        for series_id in DXY_SERIES:
            stmt = (
                select(MacroFRED)
                .where(MacroFRED.series_id == series_id)
                .where(MacroFRED.value.isnot(None))
                .order_by(MacroFRED.timestamp.desc())
                .limit(2)
            )
            rows = session.execute(stmt).scalars().all()
            if len(rows) >= 2:
                return float(rows[0].value), float(rows[1].value)
        return None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Composite fundamental score
# ---------------------------------------------------------------------------


def compute_fundamental_score() -> float | None:
    """Compute weighted fundamental score.

    Weights: eia=0.40, cot=0.30, usd=0.30.
    Returns a score in [-100, 100] or None if no data is available.
    """
    weights = {"eia": 0.40, "cot": 0.30, "usd": 0.30}
    scores: dict[str, float | None] = {}

    # EIA
    try:
        eia_change = get_latest_eia()
        if eia_change is not None:
            scores["eia"] = score_eia_inventory(eia_change)
    except Exception:
        logger.exception("Error fetching/scoring EIA data")

    # COT
    try:
        cot_net = get_latest_cot()
        if cot_net is not None:
            scores["cot"] = score_cot_positioning(cot_net)
    except Exception:
        logger.exception("Error fetching/scoring COT data")

    # USD
    try:
        usd_vals = get_latest_usd()
        if usd_vals is not None:
            current, previous = usd_vals
            scores["usd"] = score_usd(current, previous)
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

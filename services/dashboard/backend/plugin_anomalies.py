"""Anomaly detector — scan every data surface for extreme/rare conditions.

Runs a set of threshold-based checks and returns a list of CURRENTLY ACTIVE
anomalies with severity (1-10), direction, and an explanation. Also
persists newly-detected anomalies to the `anomalies` table so the user
can review "what rare events happened in the last day/week".

Categories:
  - funding_extreme            — |funding| >= 0.03% per 8h
  - funding_blowout            — |funding| >= 0.05% per 8h (very rare)
  - oi_spike                   — |OI 24h change| >= 15%
  - oi_blowout                 — |OI 24h change| >= 25%
  - score_momentum_spike       — |unified score 60m delta| >= 20
  - price_range_break          — close beyond 7-day high/low
  - orderbook_extreme          — |book imbalance| >= 50%
  - retail_crowded             — |retail vs smart delta| >= 20%
  - whale_liquidation_cluster  — >= $200k liquidations in last 10 min
  - taker_flow_extreme         — taker ratio <= 0.5 or >= 2.0
  - atr_spike                  — 1h ATR > 2x 7d average
  - breaking_news              — @marketfeed digest in last 15 min

Each detection produces:
{
  "category": "funding_extreme",
  "severity": 8,
  "direction": "BULL" | "BEAR" | "NEUTRAL",
  "title": "Funding rate extreme negative",
  "description": "Funding -0.3042% / 8h — shorts crowded, squeeze risk",
  "metric_value": -0.3042,
  "metric_threshold": -0.03,
  "icon": "🔥",
}
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func

from shared.models.base import SessionLocal
from shared.models.anomalies import Anomaly
from shared.models.binance_metrics import (
    BinanceFundingRate,
    BinanceOpenInterest,
    BinanceLongShortRatio,
    BinanceLiquidation,
)
from shared.models.signals import AnalysisScore
from shared.models.ohlcv import OHLCV
from shared.models.knowledge import KnowledgeSummary

logger = logging.getLogger(__name__)

# Minimum interval between storing the same category to the history log.
# Prevents spamming the DB with the same event every 30s while it persists.
_MIN_STORE_INTERVAL_MINUTES = 20


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------

def _check_funding(session) -> list[dict]:
    """Flag extreme funding rates."""
    fr = (
        session.query(BinanceFundingRate)
        .order_by(desc(BinanceFundingRate.funding_time))
        .first()
    )
    if fr is None:
        return []

    rate_pct = fr.funding_rate * 100
    hits: list[dict] = []

    if rate_pct <= -0.05:
        hits.append({
            "category": "funding_blowout",
            "severity": 9,
            "direction": "BULL",  # contrarian
            "title": "Funding BLOWOUT negative",
            "description": f"Funding {rate_pct:.4f}% / 8h — shorts paying blood. Imminent squeeze risk.",
            "metric_value": rate_pct,
            "metric_threshold": -0.05,
            "icon": "🔥",
        })
    elif rate_pct <= -0.03:
        hits.append({
            "category": "funding_extreme",
            "severity": 7,
            "direction": "BULL",
            "title": "Funding extreme negative",
            "description": f"Funding {rate_pct:.4f}% / 8h — shorts crowded, contrarian bull signal.",
            "metric_value": rate_pct,
            "metric_threshold": -0.03,
            "icon": "⚠️",
        })
    elif rate_pct >= 0.05:
        hits.append({
            "category": "funding_blowout",
            "severity": 9,
            "direction": "BEAR",
            "title": "Funding BLOWOUT positive",
            "description": f"Funding {rate_pct:.4f}% / 8h — longs paying blood. Imminent top risk.",
            "metric_value": rate_pct,
            "metric_threshold": 0.05,
            "icon": "🔥",
        })
    elif rate_pct >= 0.03:
        hits.append({
            "category": "funding_extreme",
            "severity": 7,
            "direction": "BEAR",
            "title": "Funding extreme positive",
            "description": f"Funding {rate_pct:.4f}% / 8h — longs crowded, contrarian bear signal.",
            "metric_value": rate_pct,
            "metric_threshold": 0.03,
            "icon": "⚠️",
        })

    return hits


def _check_open_interest(session) -> list[dict]:
    """Flag large 24h OI changes."""
    latest = (
        session.query(BinanceOpenInterest)
        .order_by(desc(BinanceOpenInterest.timestamp))
        .first()
    )
    if latest is None:
        return []
    since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    old = (
        session.query(BinanceOpenInterest)
        .filter(BinanceOpenInterest.timestamp <= since)
        .order_by(desc(BinanceOpenInterest.timestamp))
        .first()
    )
    if old is None or not old.open_interest:
        return []

    change_pct = (latest.open_interest - old.open_interest) / old.open_interest * 100
    hits: list[dict] = []

    if abs(change_pct) >= 25:
        hits.append({
            "category": "oi_blowout",
            "severity": 9,
            "direction": "NEUTRAL",
            "title": "Open Interest BLOWOUT",
            "description": f"OI {change_pct:+.2f}% in 24h — massive position flow, regime shift.",
            "metric_value": round(change_pct, 2),
            "metric_threshold": 25.0,
            "icon": "💥",
        })
    elif abs(change_pct) >= 15:
        hits.append({
            "category": "oi_spike",
            "severity": 6,
            "direction": "NEUTRAL",
            "title": "Open Interest spike",
            "description": f"OI {change_pct:+.2f}% in 24h — strong position flow.",
            "metric_value": round(change_pct, 2),
            "metric_threshold": 15.0,
            "icon": "📈" if change_pct > 0 else "📉",
        })

    return hits


def _check_score_momentum(session) -> list[dict]:
    """Flag big unified score swings in the last 60 min."""
    latest = (
        session.query(AnalysisScore)
        .order_by(desc(AnalysisScore.timestamp))
        .first()
    )
    if latest is None or latest.unified_score is None:
        return []
    an_hour_ago = datetime.now(tz=timezone.utc) - timedelta(minutes=60)
    old = (
        session.query(AnalysisScore)
        .filter(AnalysisScore.timestamp <= an_hour_ago)
        .order_by(desc(AnalysisScore.timestamp))
        .first()
    )
    if old is None or old.unified_score is None:
        return []

    delta = latest.unified_score - old.unified_score
    if abs(delta) < 20:
        return []

    return [{
        "category": "score_momentum_spike",
        "severity": 7 if abs(delta) >= 30 else 5,
        "direction": "BULL" if delta > 0 else "BEAR",
        "title": "Unified score swing",
        "description": f"Unified score moved {delta:+.1f} in 60 min ({old.unified_score:.1f} → {latest.unified_score:.1f})",
        "metric_value": round(delta, 1),
        "metric_threshold": 20.0,
        "icon": "🚀" if delta > 0 else "🪂",
    }]


def _check_price_range_break(session) -> list[dict]:
    """Flag when current close breaks the 7-day range."""
    latest = (
        session.query(OHLCV)
        .filter(OHLCV.source == "yahoo", OHLCV.timeframe == "1H")
        .order_by(desc(OHLCV.timestamp))
        .first()
    )
    if latest is None:
        return []
    since = datetime.now(tz=timezone.utc) - timedelta(days=7)
    stats = (
        session.query(
            func.max(OHLCV.high).label("hi"),
            func.min(OHLCV.low).label("lo"),
        )
        .filter(
            OHLCV.source == "yahoo",
            OHLCV.timeframe == "1H",
            OHLCV.timestamp >= since,
            OHLCV.timestamp < latest.timestamp,
        )
        .one()
    )
    if stats.hi is None or stats.lo is None:
        return []

    close = latest.close
    hits: list[dict] = []
    if close > stats.hi:
        hits.append({
            "category": "price_range_break",
            "severity": 8,
            "direction": "BULL",
            "title": "7-day high break",
            "description": f"Close ${close:.2f} broke 7d high ${stats.hi:.2f}",
            "metric_value": round(close, 3),
            "metric_threshold": round(stats.hi, 3),
            "icon": "🚀",
        })
    elif close < stats.lo:
        hits.append({
            "category": "price_range_break",
            "severity": 8,
            "direction": "BEAR",
            "title": "7-day low break",
            "description": f"Close ${close:.2f} broke 7d low ${stats.lo:.2f}",
            "metric_value": round(close, 3),
            "metric_threshold": round(stats.lo, 3),
            "icon": "🪂",
        })
    return hits


def _check_retail_crowded(session) -> list[dict]:
    """Flag when retail is much more one-sided than smart money."""
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
    if not (top and glob and top.long_pct is not None and glob.long_pct is not None):
        return []
    delta = (glob.long_pct - top.long_pct) * 100
    if abs(delta) < 20:
        return []
    return [{
        "category": "retail_crowded",
        "severity": 6,
        "direction": "BEAR" if delta > 0 else "BULL",
        "title": "Retail vs smart money divergence",
        "description": (
            f"Retail {delta:+.1f}% {'more long' if delta > 0 else 'more short'} "
            f"than smart money — strong contrarian signal."
        ),
        "metric_value": round(delta, 1),
        "metric_threshold": 20.0,
        "icon": "⚖️",
    }]


def _check_liquidation_cluster(session) -> list[dict]:
    """Flag heavy liquidations in the last 10 minutes."""
    since = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
    longs_liq = (
        session.query(func.sum(BinanceLiquidation.quote_qty_usd))
        .filter(
            BinanceLiquidation.timestamp >= since,
            BinanceLiquidation.side == "SELL",
        ).scalar() or 0.0
    )
    shorts_liq = (
        session.query(func.sum(BinanceLiquidation.quote_qty_usd))
        .filter(
            BinanceLiquidation.timestamp >= since,
            BinanceLiquidation.side == "BUY",
        ).scalar() or 0.0
    )
    longs_liq = float(longs_liq)
    shorts_liq = float(shorts_liq)
    total = longs_liq + shorts_liq
    if total < 200_000:
        return []
    dominant = "longs" if longs_liq > shorts_liq else "shorts"
    direction = "BULL" if dominant == "longs" else "BEAR"  # capitulation = contrarian
    return [{
        "category": "whale_liquidation_cluster",
        "severity": 8,
        "direction": direction,
        "title": f"Liquidation cluster — {dominant} wiped",
        "description": (
            f"${total/1000:.0f}K liquidated in 10 min "
            f"(longs ${longs_liq/1000:.0f}K / shorts ${shorts_liq/1000:.0f}K) — "
            f"{dominant} capitulation often marks reversal."
        ),
        "metric_value": round(total, 0),
        "metric_threshold": 200_000.0,
        "icon": "💀",
    }]


def _check_taker_flow(session) -> list[dict]:
    latest = (
        session.query(BinanceLongShortRatio)
        .filter(BinanceLongShortRatio.ratio_type == "taker")
        .order_by(desc(BinanceLongShortRatio.timestamp))
        .first()
    )
    if latest is None:
        return []
    ratio = latest.long_short_ratio
    if ratio >= 2.0:
        return [{
            "category": "taker_flow_extreme",
            "severity": 7,
            "direction": "BULL",
            "title": "Taker flow extreme buying",
            "description": f"Taker buy/sell ratio {ratio:.2f} — aggressive buyers overwhelming sellers.",
            "metric_value": round(ratio, 2),
            "metric_threshold": 2.0,
            "icon": "🟢",
        }]
    if ratio <= 0.5:
        return [{
            "category": "taker_flow_extreme",
            "severity": 7,
            "direction": "BEAR",
            "title": "Taker flow extreme selling",
            "description": f"Taker buy/sell ratio {ratio:.2f} — aggressive sellers overwhelming buyers.",
            "metric_value": round(ratio, 2),
            "metric_threshold": 0.5,
            "icon": "🔴",
        }]
    return []


def _check_breaking_news(session) -> list[dict]:
    since = datetime.now(tz=timezone.utc) - timedelta(minutes=15)
    latest = (
        session.query(KnowledgeSummary)
        .filter(KnowledgeSummary.timestamp >= since)
        .order_by(desc(KnowledgeSummary.timestamp))
        .first()
    )
    if latest is None:
        return []
    return [{
        "category": "breaking_news",
        "severity": 5,
        "direction": "NEUTRAL",
        "title": "Breaking news in last 15 min",
        "description": (latest.summary or "")[:200],
        "metric_value": None,
        "metric_threshold": None,
        "icon": "📰",
    }]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_ALL_CHECKS = [
    _check_funding,
    _check_open_interest,
    _check_score_momentum,
    _check_price_range_break,
    _check_retail_crowded,
    _check_liquidation_cluster,
    _check_taker_flow,
    _check_breaking_news,
]


def _persist_new(session, anomaly: dict) -> None:
    """Store an anomaly to history if we haven't logged the same category recently."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=_MIN_STORE_INTERVAL_MINUTES)
    recent = (
        session.query(Anomaly)
        .filter(
            Anomaly.category == anomaly["category"],
            Anomaly.detected_at >= cutoff,
        )
        .first()
    )
    if recent is not None:
        return
    row = Anomaly(
        detected_at=datetime.now(tz=timezone.utc),
        category=anomaly["category"],
        severity=anomaly["severity"],
        direction=anomaly["direction"],
        title=anomaly["title"],
        description=anomaly["description"],
        metric_value=anomaly.get("metric_value"),
        metric_threshold=anomaly.get("metric_threshold"),
    )
    session.add(row)
    session.commit()


def detect_anomalies() -> list[dict]:
    """Run all checks and return a flat list of active anomalies.

    Side effect: persists each unique category (per 20-min window) to the
    `anomalies` history table.
    """
    results: list[dict] = []
    with SessionLocal() as session:
        for fn in _ALL_CHECKS:
            try:
                hits = fn(session)
            except Exception:
                logger.exception("Anomaly check %s crashed", fn.__name__)
                session.rollback()
                continue
            for hit in hits:
                results.append(hit)
        # Persist all new anomalies in a separate clean session so a
        # persistence failure (e.g. missing table) cannot corrupt the
        # read-side session that the checks above used.
    try:
        with SessionLocal() as write_session:
            for hit in results:
                try:
                    _persist_new(write_session, hit)
                except Exception:
                    logger.exception("Failed to persist anomaly %s", hit.get("category"))
                    write_session.rollback()
    except Exception:
        logger.exception("Anomaly persistence session failed")

    # Sort by severity descending so the scariest thing is first.
    results.sort(key=lambda a: a.get("severity", 0), reverse=True)
    return results


def get_anomaly_history(hours: int = 24, limit: int = 100) -> list[dict]:
    """Return stored anomalies from the last N hours, newest first."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        rows = (
            session.query(Anomaly)
            .filter(Anomaly.detected_at >= since)
            .order_by(desc(Anomaly.detected_at))
            .limit(limit)
            .all()
        )
    return [
        {
            "id": r.id,
            "time": int(r.detected_at.timestamp()),
            "category": r.category,
            "severity": r.severity,
            "direction": r.direction,
            "title": r.title,
            "description": r.description,
            "metric_value": r.metric_value,
            "metric_threshold": r.metric_threshold,
        }
        for r in rows
    ]

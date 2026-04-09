"""Learning system — signal snapshots, pattern matching, signal performance.

Three related features that build the feedback loop:

1. capture_signal_snapshot()
   Periodically (every 5 min) captures the CURRENT feature vector into
   signal_snapshots table. Run by a background thread inside the dashboard.

2. backfill_forward_returns()
   Also periodic. For snapshots older than 1h/4h/24h whose forward returns
   are still null, computes them from the OHLCV history and writes them
   back. This is what makes signal performance tracking possible.

3. find_similar_moments(current_snapshot, top_n=10)
   Pattern matching: takes the current feature vector, scales it, and
   computes Euclidean distance to every historical snapshot that has a
   forward return. Returns the top N nearest neighbours with their
   actual forward returns so the user can see "last time it looked like
   this, price moved X%".

4. compute_signal_performance()
   Aggregate: for each feature (funding, technical, retail_crowded, etc.)
   bucket snapshots by value range and compute the average forward return
   per bucket. Answers "does high unified score actually predict upward
   returns?".
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, desc, func

from shared.models.base import SessionLocal
from shared.models.ohlcv import OHLCV
from shared.models.signal_snapshots import SignalSnapshot
from shared.models.signals import AnalysisScore
from shared.models.binance_metrics import (
    BinanceFundingRate,
    BinanceOpenInterest,
    BinanceLongShortRatio,
)

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECONDS = 300  # 5 min
_worker_thread: threading.Thread | None = None


# ---------------------------------------------------------------------------
# Snapshot capture
# ---------------------------------------------------------------------------

def _current_feature_vector() -> dict:
    """Gather the full feature vector from the latest DB state."""
    vec: dict = {}

    with SessionLocal() as session:
        # Latest price
        ohlc = (
            session.query(OHLCV)
            .filter(OHLCV.source == "binance", OHLCV.timeframe == "1min")
            .order_by(desc(OHLCV.timestamp))
            .first()
        )
        vec["price"] = float(ohlc.close) if ohlc and ohlc.close else None

        scores = (
            session.query(AnalysisScore)
            .order_by(desc(AnalysisScore.timestamp))
            .first()
        )
        if scores:
            vec["technical"] = scores.technical_score
            vec["fundamental"] = scores.fundamental_score
            vec["sentiment"] = scores.sentiment_score
            vec["shipping"] = scores.shipping_score
            vec["unified"] = scores.unified_score

        fr = (
            session.query(BinanceFundingRate)
            .order_by(desc(BinanceFundingRate.funding_time))
            .first()
        )
        if fr:
            vec["funding_rate"] = float(fr.funding_rate)

        oi = (
            session.query(BinanceOpenInterest)
            .order_by(desc(BinanceOpenInterest.timestamp))
            .first()
        )
        if oi:
            vec["open_interest"] = float(oi.open_interest)

        top = (
            session.query(BinanceLongShortRatio)
            .filter(BinanceLongShortRatio.ratio_type == "top_position")
            .order_by(desc(BinanceLongShortRatio.timestamp))
            .first()
        )
        if top and top.long_pct is not None:
            vec["top_trader_long_pct"] = float(top.long_pct)

        glob = (
            session.query(BinanceLongShortRatio)
            .filter(BinanceLongShortRatio.ratio_type == "global_account")
            .order_by(desc(BinanceLongShortRatio.timestamp))
            .first()
        )
        if glob and glob.long_pct is not None:
            vec["global_retail_long_pct"] = float(glob.long_pct)

        taker = (
            session.query(BinanceLongShortRatio)
            .filter(BinanceLongShortRatio.ratio_type == "taker")
            .order_by(desc(BinanceLongShortRatio.timestamp))
            .first()
        )
        if taker:
            vec["taker_buysell_ratio"] = float(taker.long_short_ratio)

    return vec


def capture_signal_snapshot() -> None:
    """Persist the current feature vector into signal_snapshots table."""
    vec = _current_feature_vector()
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)

    with SessionLocal() as session:
        # Idempotent: skip if a snapshot already exists at this exact minute
        exists = (
            session.query(SignalSnapshot)
            .filter(SignalSnapshot.timestamp == now)
            .first()
        )
        if exists:
            return
        row = SignalSnapshot(timestamp=now, **{k: v for k, v in vec.items() if v is not None})
        session.add(row)
        try:
            session.commit()
        except Exception:
            logger.exception("Failed to persist signal snapshot")
            session.rollback()


# ---------------------------------------------------------------------------
# Forward-return backfill
# ---------------------------------------------------------------------------

def _price_at_or_after(session, target_ts: datetime) -> float | None:
    row = (
        session.query(OHLCV)
        .filter(
            OHLCV.source == "binance",
            OHLCV.timeframe == "1min",
            OHLCV.timestamp >= target_ts,
        )
        .order_by(OHLCV.timestamp.asc())
        .first()
    )
    return float(row.close) if row else None


def backfill_forward_returns() -> int:
    """For each snapshot whose forward_return_<H>h is null and whose age
    exceeds H hours, compute and persist the forward return.

    Returns the number of rows updated.
    """
    now = datetime.now(tz=timezone.utc)
    updates = 0

    horizons = [(1, "forward_return_1h_pct"), (4, "forward_return_4h_pct"), (24, "forward_return_24h_pct")]

    with SessionLocal() as session:
        for hours, field in horizons:
            cutoff = now - timedelta(hours=hours)
            rows = (
                session.query(SignalSnapshot)
                .filter(
                    SignalSnapshot.timestamp <= cutoff,
                    getattr(SignalSnapshot, field).is_(None),
                    SignalSnapshot.price.isnot(None),
                )
                .order_by(SignalSnapshot.timestamp.desc())
                .limit(200)
                .all()
            )
            for snap in rows:
                target_ts = snap.timestamp + timedelta(hours=hours)
                if target_ts > now:
                    continue
                future_price = _price_at_or_after(session, target_ts)
                if future_price is None or not snap.price:
                    continue
                ret_pct = (future_price - snap.price) / snap.price * 100
                setattr(snap, field, round(ret_pct, 4))
                updates += 1

            if updates > 0:
                try:
                    session.commit()
                except Exception:
                    logger.exception("forward-return backfill commit failed")
                    session.rollback()

    if updates > 0:
        logger.info("Backfilled %d forward returns", updates)
    return updates


# ---------------------------------------------------------------------------
# Pattern matching (similar-moment lookup)
# ---------------------------------------------------------------------------

# Normalisation parameters for the distance function.
# Each feature has a typical range — we divide by this to scale to ~[-1, 1].
_FEATURE_SCALES: dict[str, float] = {
    "technical": 100.0,
    "fundamental": 100.0,
    "sentiment": 100.0,
    "shipping": 100.0,
    "unified": 100.0,
    "funding_rate": 0.001,       # 0.1% = 1 unit
    "top_trader_long_pct": 0.1,  # 10% = 1 unit
    "global_retail_long_pct": 0.1,
    "taker_buysell_ratio": 0.3,  # 0.3 ratio = 1 unit
}

_FEATURE_WEIGHTS: dict[str, float] = {
    "funding_rate": 2.0,  # weight funding heavily
    "top_trader_long_pct": 1.5,
    "global_retail_long_pct": 1.5,
    "taker_buysell_ratio": 1.5,
    "unified": 1.5,
    "technical": 1.0,
    "fundamental": 1.0,
    "sentiment": 1.0,
    "shipping": 0.5,
}


def find_similar_moments(top_n: int = 10, min_age_hours: int = 24) -> dict:
    """Find historical snapshots most similar to the current feature vector.

    Only considers snapshots with forward_return_24h_pct populated (i.e.
    old enough to have a 24h outcome).
    """
    current = _current_feature_vector()
    current_time = datetime.now(tz=timezone.utc)
    cutoff = current_time - timedelta(hours=min_age_hours)

    with SessionLocal() as session:
        rows = (
            session.query(SignalSnapshot)
            .filter(
                SignalSnapshot.timestamp <= cutoff,
                SignalSnapshot.forward_return_24h_pct.isnot(None),
            )
            .all()
        )

    if not rows:
        return {
            "current_features": current,
            "matches": [],
            "note": "No historical snapshots with forward returns yet — collect data for 24h+ first.",
        }

    scored: list[tuple[float, SignalSnapshot]] = []
    for row in rows:
        dist_sq = 0.0
        terms = 0
        for key, scale in _FEATURE_SCALES.items():
            cur_v = current.get(key)
            hist_v = getattr(row, key, None)
            if cur_v is None or hist_v is None:
                continue
            diff = (float(cur_v) - float(hist_v)) / scale
            weight = _FEATURE_WEIGHTS.get(key, 1.0)
            dist_sq += weight * diff * diff
            terms += 1
        if terms == 0:
            continue
        distance = math.sqrt(dist_sq / terms)
        scored.append((distance, row))

    scored.sort(key=lambda t: t[0])
    top = scored[:top_n]

    matches = []
    for dist, row in top:
        matches.append({
            "distance": round(dist, 4),
            "timestamp": row.timestamp.isoformat(),
            "price": row.price,
            "forward_return_1h_pct": row.forward_return_1h_pct,
            "forward_return_4h_pct": row.forward_return_4h_pct,
            "forward_return_24h_pct": row.forward_return_24h_pct,
            "features": {
                "unified": row.unified,
                "technical": row.technical,
                "funding_rate_pct": round(row.funding_rate * 100, 4) if row.funding_rate is not None else None,
                "top_trader_long_pct": row.top_trader_long_pct,
                "global_retail_long_pct": row.global_retail_long_pct,
            },
        })

    # Forward-return distribution from matches
    returns_24h = [m["forward_return_24h_pct"] for m in matches if m["forward_return_24h_pct"] is not None]
    returns_4h = [m["forward_return_4h_pct"] for m in matches if m["forward_return_4h_pct"] is not None]
    returns_1h = [m["forward_return_1h_pct"] for m in matches if m["forward_return_1h_pct"] is not None]

    def _stats(arr: list[float]) -> dict:
        if not arr:
            return {"mean": None, "median": None, "win_rate_pct": None, "n": 0}
        sorted_arr = sorted(arr)
        mid = len(sorted_arr) // 2
        median = sorted_arr[mid] if len(sorted_arr) % 2 else (sorted_arr[mid - 1] + sorted_arr[mid]) / 2
        wins = sum(1 for x in arr if x > 0)
        return {
            "mean": round(sum(arr) / len(arr), 3),
            "median": round(median, 3),
            "win_rate_pct": round(wins / len(arr) * 100, 1),
            "n": len(arr),
        }

    return {
        "current_features": current,
        "matches": matches,
        "distribution": {
            "1h": _stats(returns_1h),
            "4h": _stats(returns_4h),
            "24h": _stats(returns_24h),
        },
        "total_history": len(rows),
    }


# ---------------------------------------------------------------------------
# Signal performance (per-feature bucket stats)
# ---------------------------------------------------------------------------

def _bucket(value: float | None, buckets: list[tuple[float, float, str]]) -> str | None:
    if value is None:
        return None
    for lo, hi, label in buckets:
        if lo <= value < hi:
            return label
    return buckets[-1][2]


def compute_signal_performance() -> dict:
    """For each feature, bucket snapshots and compute per-bucket stats."""
    with SessionLocal() as session:
        rows = (
            session.query(SignalSnapshot)
            .filter(SignalSnapshot.forward_return_24h_pct.isnot(None))
            .all()
        )

    if not rows:
        return {"error": "no snapshots with forward returns yet", "total": 0}

    # Define buckets per feature
    feature_buckets: dict[str, tuple[str, list[tuple[float, float, str]]]] = {
        "unified": ("unified", [
            (-100, -20, "strong_bear"),
            (-20, -5, "bear"),
            (-5, 5, "neutral"),
            (5, 20, "bull"),
            (20, 101, "strong_bull"),
        ]),
        "technical": ("technical", [
            (-100, -20, "strong_bear"),
            (-20, -5, "bear"),
            (-5, 5, "neutral"),
            (5, 20, "bull"),
            (20, 101, "strong_bull"),
        ]),
        "funding_rate": ("funding_rate", [
            (-1.0, -0.0005, "negative_extreme"),
            (-0.0005, -0.0001, "negative_mild"),
            (-0.0001, 0.0001, "neutral"),
            (0.0001, 0.0005, "positive_mild"),
            (0.0005, 1.0, "positive_extreme"),
        ]),
        "taker_buysell_ratio": ("taker_buysell_ratio", [
            (0.0, 0.8, "sell_dominant"),
            (0.8, 0.95, "mild_sell"),
            (0.95, 1.05, "balanced"),
            (1.05, 1.2, "mild_buy"),
            (1.2, 100.0, "buy_dominant"),
        ]),
    }

    performance: dict[str, dict] = {}
    for feat, (attr, buckets) in feature_buckets.items():
        bucket_stats: dict[str, dict] = {
            label: {"count": 0, "sum_1h": 0.0, "sum_4h": 0.0, "sum_24h": 0.0, "wins_24h": 0}
            for _, _, label in buckets
        }
        for row in rows:
            value = getattr(row, attr, None)
            label = _bucket(value, buckets)
            if label is None:
                continue
            stats = bucket_stats[label]
            stats["count"] += 1
            if row.forward_return_1h_pct is not None:
                stats["sum_1h"] += row.forward_return_1h_pct
            if row.forward_return_4h_pct is not None:
                stats["sum_4h"] += row.forward_return_4h_pct
            if row.forward_return_24h_pct is not None:
                stats["sum_24h"] += row.forward_return_24h_pct
                if row.forward_return_24h_pct > 0:
                    stats["wins_24h"] += 1

        # Convert sums to averages
        result_buckets = []
        for label, stats in bucket_stats.items():
            n = stats["count"]
            result_buckets.append({
                "bucket": label,
                "count": n,
                "avg_return_1h_pct": round(stats["sum_1h"] / n, 3) if n else None,
                "avg_return_4h_pct": round(stats["sum_4h"] / n, 3) if n else None,
                "avg_return_24h_pct": round(stats["sum_24h"] / n, 3) if n else None,
                "win_rate_24h_pct": round(stats["wins_24h"] / n * 100, 1) if n else None,
            })
        performance[feat] = result_buckets

    return {
        "total_snapshots_with_returns": len(rows),
        "performance": performance,
    }


# ---------------------------------------------------------------------------
# Background worker — snapshot + backfill every SNAPSHOT_INTERVAL_SECONDS
# ---------------------------------------------------------------------------

def _worker_loop() -> None:
    logger.info("Learning worker thread started (interval=%ds)", SNAPSHOT_INTERVAL_SECONDS)
    tick = 0
    while True:
        try:
            capture_signal_snapshot()
            # Every 3rd tick (15 min) backfill forward returns — the main
            # cost is lots of small queries, no need to do it every 5 min.
            if tick % 3 == 0:
                backfill_forward_returns()
            # Smart alert evaluation runs on a faster 1-minute cycle.
            # Done inline here since we're already in a worker thread.
            try:
                from plugin_smart_alerts import evaluate_smart_alerts
                evaluate_smart_alerts()
            except Exception:
                logger.exception("smart alert eval failed")
        except Exception:
            logger.exception("Learning worker iteration failed")
        tick += 1
        time.sleep(SNAPSHOT_INTERVAL_SECONDS)


def start_learning_worker() -> None:
    """Launch the background snapshot + backfill worker (idempotent)."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(
        target=_worker_loop,
        daemon=True,
        name="learning-worker",
    )
    _worker_thread.start()

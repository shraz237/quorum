"""Background alert evaluator — polls DB every 30s, fires alerts to Telegram."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc

from shared.models.base import SessionLocal
from shared.models.alerts import Alert
from shared.models.ohlcv import OHLCV
from shared.models.signals import AnalysisScore
from shared.models.knowledge import KnowledgeSummary
from shared.models.sentiment import SentimentNews
from shared.redis_streams import publish

logger = logging.getLogger(__name__)

ALERT_STREAM = "alert.triggered"
POLL_INTERVAL_SECONDS = 30

# In-memory cache of last seen score per component (for "crosses" detection)
_last_seen_scores: dict[str, float] = {}


def _latest_price() -> float | None:
    with SessionLocal() as session:
        row = (
            session.query(OHLCV)
            .filter(OHLCV.timeframe == "1min", OHLCV.source == "yahoo")
            .order_by(desc(OHLCV.timestamp))
            .first()
        )
        if row is None:
            row = (
                session.query(OHLCV)
                .filter(OHLCV.timeframe == "1min")
                .order_by(desc(OHLCV.timestamp))
                .first()
            )
        return float(row.close) if row else None


def _latest_scores() -> dict | None:
    with SessionLocal() as session:
        row = (
            session.query(AnalysisScore)
            .order_by(desc(AnalysisScore.timestamp))
            .first()
        )
        if row is None:
            return None
        return {
            "technical": row.technical_score,
            "fundamental": row.fundamental_score,
            "sentiment": row.sentiment_score,
            "shipping": row.shipping_score,
            "unified": row.unified_score,
        }


def _recent_text_matches(keyword: str) -> list[str]:
    """Return text snippets from recent knowledge digests / news containing the keyword."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=15)
    hits: list[str] = []
    needle = keyword.lower()

    with SessionLocal() as session:
        digests = (
            session.query(KnowledgeSummary)
            .filter(KnowledgeSummary.timestamp >= cutoff)
            .all()
        )
        for d in digests:
            blob = (d.summary or "") + " " + (d.key_events or "")
            if needle in blob.lower():
                hits.append(d.summary[:200] if d.summary else "")

        news = (
            session.query(SentimentNews)
            .filter(SentimentNews.timestamp >= cutoff)
            .limit(50)
            .all()
        )
        for n in news:
            if needle in (n.title or "").lower():
                hits.append(n.title[:200])

    return hits


def _fire_alert(
    alert_id: int,
    triggered_value: float | None,
    match_info: str | None = None,
) -> None:
    """Mark alert as triggered and publish to Redis."""
    with SessionLocal() as session:
        alert = session.get(Alert, alert_id)
        if alert is None or alert.status != "active":
            return
        alert.status = "triggered"
        alert.triggered_at = datetime.now(tz=timezone.utc)
        alert.triggered_value = triggered_value
        session.commit()

        payload = {
            "type": "alert_triggered",
            "alert_id": alert.id,
            "kind": alert.kind,
            "message": alert.message or "",
            "triggered_value": triggered_value,
            "match_info": match_info,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            publish(ALERT_STREAM, payload)
            logger.info("Alert #%s fired (kind=%s)", alert.id, alert.kind)
        except Exception:
            logger.exception("Failed to publish alert %s", alert.id)


def _evaluate_once() -> None:
    """One evaluation pass over all active alerts."""
    with SessionLocal() as session:
        active = session.query(Alert).filter(Alert.status == "active").all()

    if not active:
        return

    price = _latest_price()
    scores = _latest_scores()

    for alert in active:
        try:
            if (
                alert.kind == "price"
                and price is not None
                and alert.price_target is not None
            ):
                if alert.price_direction == "above" and price >= alert.price_target:
                    _fire_alert(alert.id, triggered_value=price)
                elif alert.price_direction == "below" and price <= alert.price_target:
                    _fire_alert(alert.id, triggered_value=price)

            elif alert.kind == "keyword" and alert.keyword:
                hits = _recent_text_matches(alert.keyword)
                if hits:
                    _fire_alert(alert.id, triggered_value=None, match_info=hits[0])

            elif (
                alert.kind == "score"
                and scores
                and alert.score_component
                and alert.score_threshold is not None
            ):
                current = scores.get(alert.score_component)
                if current is None:
                    continue
                prev = _last_seen_scores.get(alert.score_component)
                _last_seen_scores[alert.score_component] = current

                if alert.score_direction == "above" and current >= alert.score_threshold:
                    _fire_alert(alert.id, triggered_value=current)
                elif alert.score_direction == "below" and current <= alert.score_threshold:
                    _fire_alert(alert.id, triggered_value=current)
                elif alert.score_direction == "crosses" and prev is not None:
                    crossed_up = prev < alert.score_threshold <= current
                    crossed_down = prev > alert.score_threshold >= current
                    if crossed_up or crossed_down:
                        _fire_alert(alert.id, triggered_value=current)

        except Exception:
            logger.exception("Error evaluating alert %s", alert.id)


def _evaluate_smart_once() -> None:
    """Evaluate smart confluence alerts (dashboard-side plugin).

    Imported lazily so the analyzer service doesn't hard-depend on dashboard
    code. If the dashboard plugin isn't importable (e.g. circular), we skip
    silently.
    """
    try:
        # Dashboard plugins live under /app in the dashboard container only.
        # In the analyzer container this import will typically fail — that's
        # OK, smart alerts run inside the dashboard process instead.
        import sys
        if "/app" not in sys.path:
            sys.path.insert(0, "/app")
        from plugin_smart_alerts import evaluate_smart_alerts  # type: ignore
        evaluate_smart_alerts()
    except ImportError:
        pass
    except Exception:
        logger.exception("smart alerts evaluation failed")


def run_evaluator_loop() -> None:
    """Main loop — runs forever in a daemon thread."""
    logger.info(
        "Alerts evaluator started (polling every %ds)", POLL_INTERVAL_SECONDS
    )
    while True:
        try:
            _evaluate_once()
            _evaluate_smart_once()
        except Exception:
            logger.exception("Alert evaluator iteration failed")
        time.sleep(POLL_INTERVAL_SECONDS)

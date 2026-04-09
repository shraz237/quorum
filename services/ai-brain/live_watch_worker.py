"""Live Watch worker — runs active watch sessions, emits updates to Redis."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import desc
from shared.models.base import SessionLocal
from shared.models.watch_sessions import WatchSession
from shared.models.ohlcv import OHLCV
from shared.models.signals import AnalysisScore
from shared.models.knowledge import KnowledgeSummary
from shared.redis_streams import publish

logger = logging.getLogger(__name__)
STREAM = "live_watch.update"
POLL_INTERVAL_SECONDS = 5  # check for due ticks every 5s

# Materiality thresholds — escalate to full Opus call
PRICE_DELTA_PCT = 0.1
SCORE_DELTA = 5.0


def _latest_price() -> tuple[float | None, datetime | None]:
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
        if row is None:
            return None, None
        return float(row.close), row.timestamp


def _latest_scores() -> dict | None:
    with SessionLocal() as session:
        row = session.query(AnalysisScore).order_by(desc(AnalysisScore.timestamp)).first()
        if row is None:
            return None
        return {
            "technical": row.technical_score,
            "fundamental": row.fundamental_score,
            "sentiment": row.sentiment_score,
            "shipping": row.shipping_score,
            "unified": row.unified_score,
            "timestamp": row.timestamp.isoformat(),
        }


def _recent_knowledge(n: int = 3) -> list[dict]:
    with SessionLocal() as session:
        rows = (
            session.query(KnowledgeSummary)
            .order_by(desc(KnowledgeSummary.timestamp))
            .limit(n)
            .all()
        )
        return [
            {
                "timestamp": r.timestamp.isoformat(),
                "summary": (r.summary or "")[:200],
                "sentiment_score": r.sentiment_score,
                "sentiment_label": r.sentiment_label,
            }
            for r in rows
        ]


def _compute_verdict_quick(session: WatchSession, price: float, scores: dict | None) -> dict:
    """Quick heuristic verdict — no LLM call. Returns dict with action, confidence, summary."""
    if scores is None:
        return {"action": "WAIT", "confidence": 0.3, "summary": "Waiting for fresh scores"}

    unified = scores.get("unified") or 0
    focus = session.focus

    if focus == "LONG":
        if unified > 30:
            return {
                "action": "FAVOR_LONG",
                "confidence": 0.6 + min((unified - 30) / 100, 0.35),
                "summary": f"Unified +{unified:.0f} supports LONG bias",
            }
        elif unified < -20:
            return {
                "action": "AVOID_LONG",
                "confidence": 0.6,
                "summary": f"Unified {unified:.0f} argues against LONG",
            }
        else:
            return {
                "action": "WAIT",
                "confidence": 0.4,
                "summary": f"Unified {unified:.0f} — indecisive for LONG",
            }
    elif focus == "SHORT":
        if unified < -30:
            return {
                "action": "FAVOR_SHORT",
                "confidence": 0.6 + min((-unified - 30) / 100, 0.35),
                "summary": f"Unified {unified:.0f} supports SHORT",
            }
        elif unified > 20:
            return {
                "action": "AVOID_SHORT",
                "confidence": 0.6,
                "summary": f"Unified +{unified:.0f} argues against SHORT",
            }
        else:
            return {
                "action": "WAIT",
                "confidence": 0.4,
                "summary": f"Unified {unified:.0f} — indecisive for SHORT",
            }
    else:  # EITHER
        if abs(unified) > 30:
            direction = "LONG" if unified > 0 else "SHORT"
            return {
                "action": f"CONSIDER_{direction}",
                "confidence": 0.6,
                "summary": f"Unified {unified:+.0f} — direction {direction}",
            }
        return {"action": "WAIT", "confidence": 0.3, "summary": "No clear direction"}


def _tick_session(session: WatchSession) -> None:
    """Run one tick on a session, publish an update."""
    price, price_ts = _latest_price()
    scores = _latest_scores()
    knowledge = _recent_knowledge(3)

    # Compute deltas
    price_delta = None
    price_delta_pct = None
    if price is not None and session.last_price is not None:
        price_delta = price - session.last_price
        price_delta_pct = (price_delta / session.last_price) * 100 if session.last_price else 0

    score_delta = None
    if scores and session.last_unified_score is not None:
        score_delta = (scores.get("unified") or 0) - session.last_unified_score

    verdict = _compute_verdict_quick(session, price or 0, scores)

    now = datetime.now(tz=timezone.utc)
    remaining_seconds = max(0, int((session.expires_at - now).total_seconds()))

    payload = {
        "type": "live_watch_update",
        "session_id": session.id,
        "timestamp": now.isoformat(),
        "tick_number": session.tick_count + 1,
        "focus": session.focus,
        "question": session.question,
        "remaining_seconds": remaining_seconds,
        "current_price": price,
        "price_source": "stooq",
        "price_delta": price_delta,
        "price_delta_pct": price_delta_pct,
        "scores": scores,
        "score_delta": score_delta,
        "recent_knowledge": knowledge,
        "verdict": verdict,
    }

    # Persist tick state
    with SessionLocal() as s:
        row = s.get(WatchSession, session.id)
        if row is None:
            return
        row.last_tick_at = now
        row.tick_count += 1
        row.last_price = price
        row.last_unified_score = scores.get("unified") if scores else None
        row.last_verdict = verdict.get("summary", "")
        s.commit()

    try:
        publish(STREAM, payload)
    except Exception:
        logger.exception("Failed to publish live_watch update")


def _expire_due_sessions() -> None:
    """Mark expired sessions as 'expired'."""
    now = datetime.now(tz=timezone.utc)
    with SessionLocal() as session:
        due = session.query(WatchSession).filter(
            WatchSession.status == "active",
            WatchSession.expires_at <= now,
        ).all()
        for row in due:
            row.status = "expired"
            row.ended_at = now
            # Publish final update
            publish(STREAM, {
                "type": "live_watch_update",
                "session_id": row.id,
                "timestamp": now.isoformat(),
                "final": True,
                "tick_number": row.tick_count + 1,
                "focus": row.focus,
                "verdict": {
                    "action": "SESSION_EXPIRED",
                    "confidence": 0.0,
                    "summary": "Watch session ended",
                },
            })
        session.commit()


def run_worker_loop() -> None:
    logger.info("Live Watch worker started (polling every %ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            now = datetime.now(tz=timezone.utc)
            _expire_due_sessions()

            with SessionLocal() as session:
                active = session.query(WatchSession).filter(WatchSession.status == "active").all()

            for sess in active:
                # Is this session due for a tick?
                if sess.last_tick_at is None:
                    due = True
                else:
                    due = (now - sess.last_tick_at).total_seconds() >= sess.cycle_seconds

                if due:
                    try:
                        _tick_session(sess)
                    except Exception:
                        logger.exception("Tick failed for session %s", sess.id)
        except Exception:
            logger.exception("Live Watch worker iteration failed")

        time.sleep(POLL_INTERVAL_SECONDS)

"""AI Brain service — orchestrates Haiku, Grok, and Opus to produce trading recommendations."""

from __future__ import annotations

import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from shared.redis_streams import subscribe, publish
from shared.schemas.events import RecommendationEvent
from shared.position_manager import (
    check_tp_sl_hits,
    list_open_positions,
    open_position,
    close_position,
)

from agents.haiku import summarize_scores
from agents.grok import get_twitter_narrative
from agents.opus import synthesize_recommendation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

STREAM_IN = "analysis.scores"
STREAM_OUT = "signal.recommendation"
STREAM_POSITION = "position.event"
GROUP = "ai-brain"
CONSUMER = "ai-brain-1"

# Minimum confidence required to open a new position.
# Prevents stacking positions when Opus is uncertain or stuck in a loop.
MIN_OPEN_CONFIDENCE = 0.65

# --- Task 3: Score cache — skip LLM cycle when scores are flat ---
_last_processed_scores: dict | None = None
_last_processed_ts: float = 0
_SCORE_CACHE_TTL_SECONDS = 1800  # 30 min
_SCORE_DELTA_THRESHOLD = 5.0  # on -100..+100 scale


def _should_publish_recommendation(rec: dict) -> bool:
    """Return True if the recommendation is materially different from the previous one.

    Suppresses publish when action, unified_score (±10), and confidence (±0.10)
    are all unchanged within the last 30 minutes.
    """
    from shared.models.base import SessionLocal
    from shared.models.signals import AIRecommendation
    from sqlalchemy import desc

    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
    try:
        with SessionLocal() as session:
            prev = (
                session.query(AIRecommendation)
                .filter(AIRecommendation.timestamp >= cutoff)
                .order_by(desc(AIRecommendation.timestamp))
                .first()
            )
            if prev is None:
                return True
            if prev.action != rec.get("action"):
                return True
            prev_score = prev.unified_score or 0
            new_score = rec.get("unified_score") or 0
            if abs(new_score - prev_score) >= 10:
                return True
            prev_conf = prev.confidence or 0
            new_conf = rec.get("confidence") or 0
            if abs(new_conf - prev_conf) >= 0.10:
                return True
            return False
    except Exception:
        logger.exception("signal_change_gate failed, defaulting to publish")
        return True


def _publish_position_event(kind: str, snap: dict) -> None:
    """Publish a position lifecycle event to the notifier."""
    payload = {
        "type": kind,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **snap,
    }
    try:
        publish(STREAM_POSITION, payload)
        logger.info("Published position.%s for #%s", kind, snap.get("id"))
    except Exception:
        logger.exception("Failed to publish position event")


def process_scores(scores: dict) -> None:
    """Run the full AI pipeline for a given scores event and publish the result."""
    global _last_processed_scores, _last_processed_ts

    unified = scores.get("unified_score")
    tech = scores.get("technical_score")
    fund = scores.get("fundamental_score")
    sent = scores.get("sentiment_score")

    # Skip if we don't have any real data yet — avoid burning tokens
    # on empty/None scores during cold start.
    if unified is None and tech is None and fund is None and sent is None:
        logger.info("Skipping scores event — all scores are None (cold start)")
        return

    logger.info("Processing scores: unified=%s", unified)

    # --- Step 0: Check existing open positions for TP/SL hits ---
    try:
        closed_hits = check_tp_sl_hits()
        for snap in closed_hits:
            kind = "tp_hit" if snap["status"] == "closed_tp" else "sl_hit"
            _publish_position_event(kind, snap)
    except Exception:
        logger.exception("TP/SL check failed")

    open_positions = list_open_positions()
    if open_positions:
        logger.info("Tracking %d open positions", len(open_positions))

    # --- Task 3: Score cache — skip LLM cycle when unified_score barely moved ---
    now = _time.time()
    if (
        _last_processed_scores is not None
        and (now - _last_processed_ts) < _SCORE_CACHE_TTL_SECONDS
    ):
        prev_unified = _last_processed_scores.get("unified_score") or 0
        new_unified = scores.get("unified_score") or 0
        if abs(new_unified - prev_unified) < _SCORE_DELTA_THRESHOLD:
            logger.info(
                "Scores unchanged within ±%.1f (prev=%.1f new=%.1f) — skipping LLM cycle",
                _SCORE_DELTA_THRESHOLD,
                prev_unified,
                new_unified,
            )
            return

    _last_processed_scores = dict(scores)
    _last_processed_ts = now

    # --- Step 1: Haiku + Grok in parallel ---
    haiku_summary: str = ""
    grok_narrative: str = ""

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_haiku = executor.submit(summarize_scores, scores)
        future_grok = executor.submit(get_twitter_narrative)

        for future in as_completed([future_haiku, future_grok]):
            if future is future_haiku:
                try:
                    haiku_summary = future.result()
                    logger.info("Haiku summary ready (%d chars)", len(haiku_summary))
                except Exception:
                    logger.exception("Haiku agent raised an unexpected error")
                    haiku_summary = "Haiku summary unavailable."
            else:
                try:
                    grok_narrative = future.result()
                    logger.info("Grok narrative ready (%d chars)", len(grok_narrative))
                except Exception:
                    logger.exception("Grok agent raised an unexpected error")
                    grok_narrative = "Grok narrative unavailable."

    # --- Step 2: Opus sequentially (with open positions context) ---
    rec = synthesize_recommendation(
        scores, haiku_summary, grok_narrative, open_positions=open_positions,
    )
    logger.info(
        "Opus recommendation: action=%s confidence=%s",
        rec.get("action"),
        rec.get("confidence"),
    )

    # --- Step 2b: Apply Opus position management actions ---
    manage = rec.get("manage_positions") or []
    for action in manage:
        try:
            pos_id = int(action.get("id"))
            verb = str(action.get("action", "")).lower()
            reason = action.get("reason") or "Opus management decision"

            if verb == "close":
                from shared.position_manager import get_current_price
                price = get_current_price()
                if price is None:
                    continue
                snap = close_position(pos_id, price, "closed_strategy", notes=reason)
                if snap:
                    _publish_position_event("strategy_close", snap)
        except Exception:
            logger.exception("Failed to apply manage action: %s", action)

    # --- Step 2c: Open a new position if Opus recommends BUY/SELL with prices ---
    action = (rec.get("action") or "").upper()
    side_map = {"BUY": "LONG", "LONG": "LONG", "SELL": "SHORT", "SHORT": "SHORT"}
    new_side = side_map.get(action)
    entry = rec.get("entry_price")
    conf = rec.get("confidence") or 0
    if new_side and entry is not None:
        # Confidence floor — skip low-conviction signals to avoid stacking positions
        if conf < MIN_OPEN_CONFIDENCE:
            logger.info(
                "Skipping position open — confidence %.2f below %.2f threshold",
                conf,
                MIN_OPEN_CONFIDENCE,
            )
        else:
            # Duplicate-side guard — don't stack positions on the same side
            existing = list_open_positions()
            same_side = [p for p in existing if str(p.get("side", "")).upper() == new_side]
            if same_side:
                logger.info(
                    "Skipping position open — already have %d open %s position(s)",
                    len(same_side),
                    new_side,
                )
            else:
                try:
                    new_id = open_position(
                        side=new_side,
                        entry_price=float(entry),
                        stop_loss=rec.get("stop_loss"),
                        take_profit=rec.get("take_profit"),
                        notes=(rec.get("analysis_text") or "")[:500],
                    )
                    if new_id is not None:
                        _publish_position_event(
                            "opened",
                            {
                                "id": new_id,
                                "side": new_side,
                                "entry_price": float(entry),
                                "stop_loss": rec.get("stop_loss"),
                                "take_profit": rec.get("take_profit"),
                            },
                        )
                except Exception:
                    logger.exception("Failed to open position from recommendation")

    # --- Step 3: Publish to Redis stream (with signal change gate) ---
    event = RecommendationEvent(
        timestamp=datetime.now(timezone.utc),
        action=rec.get("action", "WAIT"),
        unified_score=rec.get("unified_score"),
        opus_override_score=rec.get("opus_override_score"),
        confidence=rec.get("confidence"),
        entry_price=rec.get("entry_price"),
        stop_loss=rec.get("stop_loss"),
        take_profit=rec.get("take_profit"),
        haiku_summary=haiku_summary,
        grok_narrative=grok_narrative,
    )
    # Task 4: Only publish if recommendation changed materially from the previous one
    if _should_publish_recommendation(rec):
        try:
            publish(STREAM_OUT, event.model_dump())
            logger.info("Published RecommendationEvent to %s", STREAM_OUT)
        except Exception:
            logger.exception("Failed to publish recommendation to Redis")
    else:
        logger.info(
            "RecommendationEvent suppressed (no material change from previous) — stored in DB only"
        )


def main() -> None:
    logger.info("AI Brain service starting — listening on stream '%s'", STREAM_IN)

    for msg_id, data in subscribe(STREAM_IN, group=GROUP, consumer=CONSUMER, block=10_000):
        logger.info("Received scores event %s", msg_id)
        try:
            process_scores(data)
        except Exception:
            logger.exception("Failed to process scores event %s", msg_id)


if __name__ == "__main__":
    main()

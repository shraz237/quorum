"""AI Brain service — orchestrates Haiku, Grok, and Opus to produce trading recommendations."""

from __future__ import annotations

import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from shared.redis_streams import subscribe, publish
from shared.schemas.events import RecommendationEvent
from shared.position_manager import (
    check_tp_sl_hits,
    list_open_positions,
    list_open_campaigns,
    open_new_campaign,
    add_dca_layer,
    get_current_price,
)
from shared.account_manager import recompute_account_state
from shared.sizing import DCA_LAYERS_MARGIN, DCA_DRAWDOWN_TRIGGER_PCT

from agents.haiku import summarize_scores
from agents.grok import get_twitter_narrative
from agents.opus import synthesize_recommendation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

STREAM_IN = "analysis.scores"
STREAM_KNOWLEDGE = "knowledge.summary"
STREAM_OUT = "signal.recommendation"
STREAM_POSITION = "position.event"
GROUP = "ai-brain"
CONSUMER = "ai-brain-1"
CONSUMER_KNOWLEDGE = "ai-brain-knowledge"

# Breaking news threshold: digests with |sentiment_score| >= this trigger
# an immediate Opus reassessment when they contradict an open campaign.
BREAKING_NEWS_THRESHOLD = 0.5

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


def _handle_campaign_signal(new_side: str, conf: float, rec: dict) -> None:
    """Apply BUY/SELL signal logic against the current campaign state."""
    account = recompute_account_state()
    free_margin = account.get("free_margin") or 0.0

    current_price = get_current_price()
    if current_price is None:
        logger.warning("_handle_campaign_signal: no current price — skipping")
        return

    open_camps = list_open_campaigns()

    if not open_camps:
        # No open campaign — open a new one if we have margin for layer 0
        layer0_margin = DCA_LAYERS_MARGIN[0]
        if free_margin < layer0_margin:
            logger.info(
                "Skipping campaign open — free_margin %.2f < layer0 %.2f",
                free_margin, layer0_margin,
            )
            return
        campaign_id = open_new_campaign(new_side, current_price)
        _publish_position_event(
            "campaign_opened",
            {
                "id": campaign_id,
                "side": new_side,
                "entry_price": current_price,
                "layer": 0,
            },
        )
        return

    camp = open_camps[0]
    camp_side = camp.get("side", "").upper()

    if camp_side != new_side:
        # Conflict — opposite campaign open, do nothing
        logger.info(
            "Signal conflict: open campaign is %s, signal is %s — doing nothing",
            camp_side, new_side,
        )
        return

    # Same side — check if we should add a DCA layer
    avg_entry = camp.get("avg_entry_price")
    layers_used = camp.get("layers_used", 0)
    next_margin = camp.get("next_layer_margin")

    if next_margin is None:
        logger.info("Campaign #%s: all DCA layers exhausted", camp["id"])
        return

    if free_margin < next_margin:
        logger.info(
            "Campaign #%s: free_margin %.2f < next layer %.2f — skipping DCA",
            camp["id"], free_margin, next_margin,
        )
        return

    should_dca = False
    reason = ""

    # Check drawdown trigger
    if avg_entry and avg_entry > 0:
        if camp_side == "LONG":
            drawdown_pct = ((avg_entry - current_price) / avg_entry) * 100
        else:
            drawdown_pct = ((current_price - avg_entry) / avg_entry) * 100

        if drawdown_pct >= DCA_DRAWDOWN_TRIGGER_PCT:
            should_dca = True
            reason = f"drawdown {drawdown_pct:.2f}% >= {DCA_DRAWDOWN_TRIGGER_PCT}%"

    # Or strong fresh signal
    if not should_dca and conf >= 0.75:
        # Check last DCA layer timing — find latest position in campaign
        from shared.models.base import SessionLocal
        from shared.models.positions import Position as _Position
        with SessionLocal() as session:
            latest_pos = (
                session.query(_Position)
                .filter(
                    _Position.campaign_id == camp["id"],
                    _Position.status == "open",
                )
                .order_by(_Position.opened_at.desc())
                .first()
            )
            if latest_pos and latest_pos.opened_at:
                age = datetime.now(tz=timezone.utc) - latest_pos.opened_at
                if age >= timedelta(minutes=30):
                    should_dca = True
                    reason = f"strong signal conf={conf:.2f}, last layer {age.seconds//60}m ago"

    if should_dca:
        new_pos_id = add_dca_layer(camp["id"], current_price)
        if new_pos_id is not None:
            _publish_position_event(
                "dca_layer_added",
                {
                    "id": new_pos_id,
                    "campaign_id": camp["id"],
                    "side": camp_side,
                    "entry_price": current_price,
                    "layer": layers_used,
                    "reason": reason,
                },
            )
            logger.info(
                "Campaign #%s: DCA layer %d added @ %.2f (%s)",
                camp["id"], layers_used, current_price, reason,
            )
    else:
        logger.info(
            "Campaign #%s: holding, no DCA trigger (conf=%.2f, avg_entry=%s, price=%.2f)",
            camp["id"], conf, avg_entry, current_price,
        )


def process_scores(scores: dict) -> None:
    """Run the full AI pipeline for a given scores event and publish the result."""
    global _last_processed_scores, _last_processed_ts

    breaking_news = scores.pop("__breaking_news__", None)
    is_urgent = breaking_news is not None

    unified = scores.get("unified_score")
    tech = scores.get("technical_score")
    fund = scores.get("fundamental_score")
    sent = scores.get("sentiment_score")

    # Skip if we don't have any real data yet — avoid burning tokens
    # on empty/None scores during cold start. (Urgent breaking news always runs.)
    if not is_urgent and unified is None and tech is None and fund is None and sent is None:
        logger.info("Skipping scores event — all scores are None (cold start)")
        return

    if is_urgent:
        logger.warning(
            "🚨 URGENT cycle triggered by breaking news: %s",
            (breaking_news.get("summary") or "")[:120],
        )
    else:
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
    # (Urgent breaking-news cycles ALWAYS bypass the cache.)
    now = _time.time()
    if (
        not is_urgent
        and _last_processed_scores is not None
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

    # --- Step 2: Opus sequentially (with open positions + optional breaking news) ---
    rec = synthesize_recommendation(
        scores,
        haiku_summary,
        grok_narrative,
        open_positions=open_positions,
        breaking_news=breaking_news,
    )
    logger.info(
        "Opus recommendation: action=%s confidence=%s",
        rec.get("action"),
        rec.get("confidence"),
    )

    # --- Step 2b: Campaign-based position management ---
    action = (rec.get("action") or "").upper()
    side_map = {"BUY": "LONG", "LONG": "LONG", "SELL": "SHORT", "SHORT": "SHORT"}
    new_side = side_map.get(action)
    conf = rec.get("confidence") or 0

    if new_side and conf >= MIN_OPEN_CONFIDENCE:
        try:
            _handle_campaign_signal(new_side, conf, rec)
        except Exception:
            logger.exception("Campaign signal handling failed")

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


# ---------------------------------------------------------------------------
# Breaking-news watcher
# ---------------------------------------------------------------------------

def _digest_side_implication(sentiment_score: float) -> str | None:
    """A digest with positive score is bullish for oil → favors LONG.
    Negative is bearish → favors SHORT. Returns 'LONG' / 'SHORT' / None."""
    if sentiment_score >= BREAKING_NEWS_THRESHOLD:
        return "LONG"
    if sentiment_score <= -BREAKING_NEWS_THRESHOLD:
        return "SHORT"
    return None


def process_breaking_news(digest: dict) -> None:
    """Triggered when a knowledge.summary digest arrives.

    If the digest is high-impact AND contradicts an open campaign side, run an
    immediate Opus reassessment (bypassing the score cache and the analyzer
    throttle) so the bot can decide whether to close the conflicting campaign
    before TP/SL hits.
    """
    sentiment_score = digest.get("sentiment_score")
    if sentiment_score is None:
        return
    try:
        sentiment_score = float(sentiment_score)
    except (TypeError, ValueError):
        return

    digest_side = _digest_side_implication(sentiment_score)
    if digest_side is None:
        # Below threshold — let the regular cycle handle it
        return

    # Find any open campaign whose side is the OPPOSITE of what the news favors
    try:
        open_campaigns = list_open_campaigns()
    except Exception:
        logger.exception("Failed to list open campaigns for breaking-news check")
        return

    conflicts = [
        c for c in open_campaigns
        if str(c.get("side", "")).upper() != digest_side
    ]
    if not conflicts:
        logger.info(
            "Breaking news (sentiment=%+.2f, favors %s) — no conflicting open campaigns, no action",
            sentiment_score, digest_side,
        )
        return

    logger.warning(
        "🚨 BREAKING NEWS — sentiment=%+.2f favors %s, %d conflicting open campaign(s) — triggering urgent Opus reassessment",
        sentiment_score, digest_side, len(conflicts),
    )

    # Build a synthetic "scores" event mirroring whatever the analyzer last
    # published, but force the LLM cycle to run regardless of the cache.
    from shared.models.base import SessionLocal
    from shared.models.signals import AnalysisScore
    from sqlalchemy import desc

    try:
        with SessionLocal() as session:
            row = session.query(AnalysisScore).order_by(desc(AnalysisScore.timestamp)).first()
            if row:
                base_scores = {
                    "timestamp": row.timestamp.isoformat(),
                    "technical_score": row.technical_score,
                    "fundamental_score": row.fundamental_score,
                    "sentiment_score": row.sentiment_score,
                    "shipping_score": row.shipping_score,
                    "unified_score": row.unified_score,
                }
            else:
                base_scores = {}
    except Exception:
        logger.exception("Failed to read latest scores for breaking-news cycle")
        base_scores = {}

    # Inject the breaking-news digest as a marker so process_scores knows
    # to bypass the cache and pass the digest to Opus as URGENT context.
    base_scores["__breaking_news__"] = {
        "summary": digest.get("summary"),
        "sentiment_score": sentiment_score,
        "sentiment_label": digest.get("sentiment_label"),
        "key_events": digest.get("key_events", []),
        "favors_side": digest_side,
        "conflicting_campaign_ids": [c["id"] for c in conflicts],
    }

    # Reset cache so process_scores can't short-circuit
    global _last_processed_scores, _last_processed_ts
    _last_processed_scores = None
    _last_processed_ts = 0

    try:
        process_scores(base_scores)
    except Exception:
        logger.exception("Breaking-news urgent cycle failed")


def _knowledge_consumer() -> None:
    """Background thread: subscribe to knowledge.summary and react to breaking news."""
    logger.info("Knowledge consumer starting on stream '%s'", STREAM_KNOWLEDGE)
    while True:
        try:
            for msg_id, data in subscribe(
                STREAM_KNOWLEDGE,
                group=GROUP,
                consumer=CONSUMER_KNOWLEDGE,
                block=10_000,
            ):
                logger.info("Received knowledge digest %s", msg_id)
                try:
                    process_breaking_news(data)
                except Exception:
                    logger.exception("process_breaking_news failed for %s", msg_id)
        except Exception:
            logger.exception("knowledge consumer crashed, restarting in 5s")
            _time.sleep(5)


def main() -> None:
    logger.info("AI Brain service starting — listening on streams '%s' and '%s'",
                STREAM_IN, STREAM_KNOWLEDGE)

    # Start the breaking-news watcher in a background thread
    threading.Thread(target=_knowledge_consumer, daemon=True, name="knowledge-watcher").start()

    # Start the live watch worker in a background thread
    from live_watch_worker import run_worker_loop as _live_watch_loop
    threading.Thread(target=_live_watch_loop, daemon=True, name="live-watch").start()
    logger.info("Live Watch worker thread started")

    for msg_id, data in subscribe(STREAM_IN, group=GROUP, consumer=CONSUMER, block=10_000):
        logger.info("Received scores event %s", msg_id)
        try:
            process_scores(data)
        except Exception:
            logger.exception("Failed to process scores event %s", msg_id)


if __name__ == "__main__":
    main()

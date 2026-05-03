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
    compute_campaign_state,
    list_open_positions,
    list_open_campaigns,
    open_new_campaign,
    add_dca_layer,
    get_current_price,
)
from shared.account_manager import DEFAULT_LEVERAGE, recompute_account_state
from shared.sizing import DCA_LAYERS_MARGIN, DCA_DRAWDOWN_TRIGGER_PCT


def _campaign_sizing_payload(campaign_id: int) -> dict:
    """Return the sizing fields for Telegram notifications.

    margin × leverage = notional — this is what the user wants to see
    at a glance in every notification. Silently returns an empty dict if
    the campaign state can't be computed so the publish never fails.
    """
    try:
        state = compute_campaign_state(campaign_id) or {}
        return {
            "total_margin": state.get("total_margin"),
            "total_lots": state.get("total_lots"),
            "total_nominal": state.get("total_nominal"),
            "leverage": DEFAULT_LEVERAGE,
            "avg_entry_price": state.get("avg_entry_price"),
            "layers_used": state.get("layers_used"),
            "max_layers": state.get("max_layers"),
        }
    except Exception:
        logger.exception("Failed to fetch sizing payload for campaign #%s", campaign_id)
        return {}

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
# Lowered from 0.65 -> 0.55 after review: 0.65 was blocking ~95% of non-extreme
# setups that would still have been valid discretionary entries. With 0.55 we
# catch setups like "BUY 0.58 score 18 with bullish breaking news" that were
# being silently skipped during quiet regimes.
MIN_OPEN_CONFIDENCE = 0.55

# --- Task 3: Score cache — skip LLM cycle when scores are flat ---
_last_processed_scores: dict | None = None
_last_processed_ts: float = 0
_SCORE_CACHE_TTL_SECONDS = 3600  # 30 min
_SCORE_DELTA_THRESHOLD = 5.0  # on -100..+100 scale


def _should_publish_recommendation(rec: dict) -> bool:
    """Return True if the recommendation should be pushed to the Redis
    stream (and thus to Telegram).

    The old behaviour gated on same-action + score delta < 10 + confidence
    delta < 0.10 over a 30-min window — which effectively suppressed
    almost every same-direction signal during quiet regimes (e.g. 50
    BUY signals in a row would only publish once). Users reported only
    seeing marketfeed digests on Telegram, never the signals themselves.

    New behaviour — publish if ANY of:
      - Action changed vs the last published signal
      - Actionable signal (BUY/SELL/LONG/SHORT) with confidence rising
        by >= 0.05 (so gradual conviction upgrades DO show up)
      - 20+ minutes elapsed since the last publish of this action
        (heartbeat for the current regime, at most one per 20 min)
    Only pure WAIT↔HOLD churn at similar confidence is suppressed.
    """
    from shared.models.base import SessionLocal
    from shared.models.signals import AIRecommendation
    from sqlalchemy import desc

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=2)
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

            new_action = (rec.get("action") or "").upper()
            prev_action = (prev.action or "").upper()

            # 1. Action changed — always publish
            if prev_action != new_action:
                return True

            # 2. Actionable signal with confidence upgrade
            is_actionable = new_action in ("BUY", "SELL", "LONG", "SHORT")
            prev_conf = prev.confidence or 0
            new_conf = rec.get("confidence") or 0
            if is_actionable and (new_conf - prev_conf) >= 0.05:
                return True

            # 3. Heartbeat — at most one publish per 20 min even during
            #    long same-direction runs
            age = datetime.now(tz=timezone.utc) - prev.timestamp
            if age >= timedelta(minutes=20):
                return True

            return False
    except Exception:
        logger.exception("signal_change_gate failed, defaulting to publish")
        return True


_CAMPAIGN_HOT_EVENTS = {
    "campaign_opened",
    "campaign_manual_close",
    "campaign_tp",
    "campaign_hard_stop",
    "tp_hit",
    "sl_hit",
    "strategy_close",
}


def _publish_position_event(kind: str, snap: dict) -> None:
    """Publish a position lifecycle event to the notifier.

    Also arms the heartbeat hot window (30s ticks for 5 min) on any
    campaign open/close transition so Opus aggressively monitors the
    new state and can react fast if the setup was wrong.
    """
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

    if kind in _CAMPAIGN_HOT_EVENTS:
        try:
            from shared.heartbeat_hot import arm_hot_window
            arm_hot_window(reason=f"event={kind}")
        except Exception:
            logger.exception("Failed to arm heartbeat hot window on %s", kind)


def _handle_campaign_signal(new_side: str, conf: float, rec: dict) -> None:
    """Apply BUY/SELL signal logic against the current campaign state."""

    # --- GATE 0: MARKET HOURS ---
    # WTI futures trade Sun 5pm–Fri 5pm CT (nearly 24/5 but closed weekends).
    # Refuse to open new campaigns on Saturday or Sunday UTC when there's
    # no real price discovery happening.
    now_utc = datetime.now(tz=timezone.utc)
    weekday = now_utc.weekday()  # 0=Mon ... 6=Sun
    if weekday in (5, 6):  # Saturday or Sunday
        logger.info("Market hours BLOCKED %s entry: weekend (day=%d)", new_side, weekday)
        return

    # --- GATE 1: RANGE BIAS ---
    try:
        from shared.range_bias import should_allow_entry
        allowed, reason = should_allow_entry(new_side)
        if not allowed:
            logger.warning("Range bias BLOCKED %s entry: %s", new_side, reason)
            try:
                publish(STREAM_POSITION, {
                    "type": "heartbeat_action",
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    "campaign_id": 0,
                    "action": "blocked",
                    "side": new_side,
                    "reason": f"Entry BLOCKED by range bias: {reason}",
                })
            except Exception:
                pass
            return
        logger.info("Range bias OK for %s: %s", new_side, reason)
    except Exception:
        logger.exception("Range bias check failed — allowing entry")

    # --- GATE 2: TECHNICAL SCORE ALIGNMENT ---
    # Never go LONG when technicals are bearish or SHORT when bullish.
    # Root cause of losing campaigns #7 (tech=-4.5), #10 (tech=-1.7).
    #
    # OPUS OVERRIDE: when Opus confidence ≥ 70% AND opus_override_score ≥ 40,
    # bypass the tech gate entirely. This lets Opus punch through on
    # exceptional conviction (e.g. 80+ facilities destroyed, Hormuz
    # compromised) where the chart hasn't caught up to the news yet.
    # Only fires on extreme events, not marginal calls.
    MIN_TECH_SCORE_FOR_ENTRY = 5.0
    OPUS_OVERRIDE_CONFIDENCE = 0.70
    OPUS_OVERRIDE_SCORE = 40
    try:
        opus_conf = rec.get("confidence") or 0
        opus_override = rec.get("opus_override_score") or 0
        opus_overriding = (opus_conf >= OPUS_OVERRIDE_CONFIDENCE and abs(opus_override) >= OPUS_OVERRIDE_SCORE)

        from shared.models.base import SessionLocal as _SL
        from shared.models.signals import AnalysisScore as _AS
        with _SL() as _sess:
            _latest = _sess.query(_AS).order_by(_AS.timestamp.desc()).first()
            if _latest and _latest.technical_score is not None:
                tech = float(_latest.technical_score)
                blocked = False
                if new_side == "BUY" and tech < MIN_TECH_SCORE_FOR_ENTRY:
                    blocked = True
                    block_reason = f"Tech score BLOCKED BUY: technical={tech:.1f} < {MIN_TECH_SCORE_FOR_ENTRY} minimum"
                elif new_side == "SELL" and tech > -MIN_TECH_SCORE_FOR_ENTRY:
                    blocked = True
                    block_reason = f"Tech score BLOCKED SELL: technical={tech:.1f} > {-MIN_TECH_SCORE_FOR_ENTRY} minimum"
                if blocked and opus_overriding:
                    logger.warning(
                        "Tech gate would block but OPUS OVERRIDE active (conf=%.2f, override_score=%.0f) — ALLOWING %s",
                        opus_conf, opus_override, new_side,
                    )
                    try:
                        publish(STREAM_POSITION, {
                            "type": "heartbeat_action",
                            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                            "campaign_id": 0,
                            "action": "opus_override",
                            "side": new_side,
                            "reason": (
                                f"OPUS OVERRIDE: tech={tech:.1f} would block {new_side} but "
                                f"Opus confidence {opus_conf:.0%} + override score {opus_override:.0f} "
                                f"punched through the gate. Exceptional conviction event."
                            ),
                        })
                    except Exception:
                        pass
                    blocked = False  # allow through
                if blocked:
                    logger.warning(block_reason)
                    try:
                        publish(STREAM_POSITION, {
                            "type": "heartbeat_action",
                            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                            "campaign_id": 0,
                            "action": "blocked",
                            "side": new_side,
                            "reason": f"Entry BLOCKED: {block_reason}",
                        })
                    except Exception:
                        pass
                    return
    except Exception:
        logger.exception("Tech score gate failed — allowing entry")

    # --- GATE 3: COOLDOWN AFTER LOSING TRADE ---
    # Don't immediately re-enter after getting stopped out. Wait 30 min.
    LOSS_COOLDOWN_MINUTES = 30
    try:
        from shared.models.campaigns import Campaign as _Camp
        with _SL() as _sess:
            last_closed = (
                _sess.query(_Camp)
                .filter(
                    _Camp.persona == "main",
                    _Camp.status != "open",
                    _Camp.realized_pnl < 0,
                )
                .order_by(_Camp.closed_at.desc())
                .first()
            )
            if last_closed and last_closed.closed_at:
                age_min = (datetime.now(tz=timezone.utc) - last_closed.closed_at).total_seconds() / 60
                if age_min < LOSS_COOLDOWN_MINUTES:
                    logger.warning(
                        "Loss cooldown BLOCKED %s entry: last losing campaign #%s closed %.0f min ago (need %d min)",
                        new_side, last_closed.id, age_min, LOSS_COOLDOWN_MINUTES,
                    )
                    return
    except Exception:
        logger.exception("Loss cooldown check failed — allowing entry")

    account = recompute_account_state()
    free_margin = account.get("free_margin") or 0.0

    current_price = get_current_price()
    if current_price is None:
        logger.warning("_handle_campaign_signal: no current price — skipping")
        return

    open_camps = list_open_campaigns(persona="main")

    if not open_camps:
        # No open campaign — open a new one if we have margin. The
        # dynamic sizer will compute the actual layer-0 margin and
        # enforce the 80%-equity cap; we just do a cheap pre-check
        # using the BASE schedule so we don't even call the sizer
        # when we're obviously out of cash.
        layer0_base = DCA_LAYERS_MARGIN[0]
        if free_margin < layer0_base * 0.5:  # even at 0.5x multiplier
            logger.info(
                "Skipping campaign open — free_margin %.2f < minimum layer0 %.2f",
                free_margin, layer0_base * 0.5,
            )
            return
        # Propagate Opus-provided TP/SL into the campaign so the auto-
        # close check can enforce them. Numeric values only; None means
        # "not specified" and the campaign just relies on the -50% margin
        # hard stop.
        opus_tp = rec.get("take_profit")
        opus_sl = rec.get("stop_loss")
        try:
            opus_tp = float(opus_tp) if opus_tp not in (None, "") else None
        except (TypeError, ValueError):
            opus_tp = None
        try:
            opus_sl = float(opus_sl) if opus_sl not in (None, "") else None
        except (TypeError, ValueError):
            opus_sl = None

        campaign_id = open_new_campaign(
            new_side,
            current_price,
            llm_confidence=conf,
            take_profit=opus_tp,
            stop_loss=opus_sl,
        )
        if campaign_id is None:
            logger.warning(
                "open_new_campaign returned None (equity cap?) — no action"
            )
            return
        _publish_position_event(
            "campaign_opened",
            {
                "id": campaign_id,
                "side": new_side,
                "entry_price": current_price,
                "take_profit": opus_tp,
                "stop_loss": opus_sl,
                "layer": 0,
                "reason": (rec.get("reasoning") or "")[:200],
                **_campaign_sizing_payload(campaign_id),
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

    # Find the age of the last layer for cooldown checks
    from shared.models.base import SessionLocal
    from shared.models.positions import Position as _Position
    last_layer_age_min = None
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
            last_layer_age_min = (datetime.now(tz=timezone.utc) - latest_pos.opened_at).total_seconds() / 60

    # Minimum cooldown — 2 min prevents stacking on literally every tick
    DCA_COOLDOWN_MINUTES = 2

    if last_layer_age_min is not None and last_layer_age_min < DCA_COOLDOWN_MINUTES:
        pass  # too soon, skip silently
    else:
        # --- TRIGGER 1: Price moved in EITHER direction ---
        # DCA on drawdown (averaging down) AND on price moving in your
        # favor (pyramiding / adding to winners). Both directions build
        # the position. The 25-layer schedule is designed for gradual
        # scaling — the bot should use it aggressively.
        if avg_entry and avg_entry > 0:
            if camp_side == "LONG":
                move_pct = abs(current_price - avg_entry) / avg_entry * 100
            else:
                move_pct = abs(avg_entry - current_price) / avg_entry * 100

            if move_pct >= DCA_DRAWDOWN_TRIGGER_PCT:
                direction = "favorable" if (
                    (camp_side == "LONG" and current_price > avg_entry) or
                    (camp_side == "SHORT" and current_price < avg_entry)
                ) else "adverse"
                should_dca = True
                reason = f"price moved {move_pct:.2f}% ({direction}) — adding layer"

        # --- TRIGGER 2: Conviction-based DCA ---
        # Same-side signal with decent confidence
        if not should_dca and conf >= 0.60:
            should_dca = True
            reason = f"conviction DCA: same-side signal conf={conf:.2f}"

        # --- TRIGGER 3: Time-based DCA ---
        # Build the position gradually even in flat markets
        if not should_dca and layers_used <= 5 and last_layer_age_min is not None:
            if last_layer_age_min >= 15:
                should_dca = True
                reason = f"time-based DCA: {last_layer_age_min:.0f} min since last layer, only {layers_used} layers"

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
                    **_campaign_sizing_payload(camp["id"]),
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
    same_side_open = [
        c for c in open_campaigns
        if str(c.get("side", "")).upper() == digest_side
    ]

    if not conflicts and not open_campaigns:
        # No positions at all — and the news is high-impact. Run an urgent
        # cycle so Opus can decide whether to OPEN a new campaign on the
        # back of this news. Previous behaviour was to do nothing, which
        # meant the bot stayed flat through every strong bullish headline
        # unless there was already a (conflicting) bearish position to
        # manage. Now we proactively evaluate entries too.
        logger.warning(
            "🚨 BREAKING NEWS — sentiment=%+.2f favors %s, NO open campaigns — "
            "triggering urgent Opus reassessment for potential entry",
            sentiment_score, digest_side,
        )
        # Fall through to the synthetic-scores cycle below.
    elif not conflicts:
        # Same-side positions already exist — the news CONFIRMS them, no
        # need to rerun Opus just to restate the thesis.
        logger.info(
            "Breaking news (sentiment=%+.2f, favors %s) — %d same-side campaign(s) already open, no action",
            sentiment_score, digest_side, len(same_side_open),
        )
        return
    else:
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

    # Start the Opus heartbeat worker — reviews open campaigns every 15 min
    from heartbeat import run_worker_loop as _heartbeat_loop
    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()
    logger.info("Heartbeat worker thread started")

    # Start the theses watcher (triggers) and resolver (outcomes)
    from theses_workers import run_watcher_loop as _theses_watcher
    from theses_workers import run_resolver_loop as _theses_resolver
    threading.Thread(target=_theses_watcher, daemon=True, name="theses-watcher").start()
    threading.Thread(target=_theses_resolver, daemon=True, name="theses-resolver").start()
    logger.info("Theses watcher + resolver threads started")

    # Daily P/L summary — fires at 22:00 UTC (5pm ET) with combined main+scalper stats
    from daily_summary import run_daily_summary_loop as _daily_summary
    threading.Thread(target=_daily_summary, daemon=True, name="daily-summary").start()
    logger.info("Daily summary worker thread started")

    # Pre-market prep — fires Sunday 21:00 UTC (1h before open) with position
    # risk analysis, weekend news summary, and hot window arming
    from pre_market import run_pre_market_loop as _pre_market
    threading.Thread(target=_pre_market, daemon=True, name="pre-market").start()
    logger.info("Pre-market worker thread started")

    # Scalper background poller — hits /api/scalp-brain every 30s so the
    # scalper executor fires even when the dashboard tab is backgrounded
    # or nobody is viewing it. Without this, the scalper goes blind when
    # the visibility guard pauses frontend polling.
    def _scalper_poller():
        import httpx
        from shared.market_hours import is_market_open
        _time.sleep(60)  # let dashboard boot
        logger.info("Scalper poller started (30s cadence, pauses when market closed)")
        while True:
            if not is_market_open():
                _time.sleep(30)  # sleep 5 min when market closed
                continue
            try:
                httpx.get("http://dashboard:8000/api/scalp-brain", timeout=15)
            except Exception:
                pass  # dashboard might be restarting
            _time.sleep(30)

    threading.Thread(target=_scalper_poller, daemon=True, name="scalper-poller").start()
    logger.info("Scalper poller thread started")

    for msg_id, data in subscribe(STREAM_IN, group=GROUP, consumer=CONSUMER, block=10_000):
        logger.info("Received scores event %s", msg_id)
        try:
            process_scores(data)
        except Exception:
            logger.exception("Failed to process scores event %s", msg_id)


if __name__ == "__main__":
    main()

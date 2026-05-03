"""Heartbeat Opus position manager.

Every HEARTBEAT_INTERVAL_MINUTES (default 15), ask Claude Opus 4.6 to
review every open campaign and decide hold / close / update_levels.
Opus has full execution authority — the worker executes its decisions
immediately, logs everything to heartbeat_runs, and fires a Telegram
alert on any action.

Kill-switch: Redis key `heartbeat:enabled` (default "true"). The dashboard
has a pause/resume button that flips this. Env var HEARTBEAT_ENABLED=false
forces off regardless of Redis.

Guardrails (enforced on this side, never trusted to Opus):
  - Max 1 close per campaign per 30 min (close cooldown)
  - Refuse close when |unrealized_pnl_pct| < 0.5%  (indecision guard)
  - Immutable -50% hard-stop still runs independently on every score event
  - Malformed tool calls are logged as decision='error'
  - Redis lock `heartbeat:running` (60s TTL) prevents overlapping ticks

The -50% hard-stop in shared/position_manager.check_tp_sl_hits() remains
the last-resort safety net — it runs on every score event and does NOT
depend on the heartbeat loop working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.campaigns import Campaign
from shared.models.heartbeat_runs import HeartbeatRun
from shared.models.ohlcv import OHLCV
from shared.models.signals import AnalysisScore
from shared.models.knowledge import KnowledgeSummary
from shared.account_manager import recompute_account_state
from shared.position_manager import (
    check_tp_sl_hits,
    close_campaign,
    compute_campaign_state,
    get_current_price,
    update_campaign_levels,
)
from shared.redis_streams import get_redis, publish
from shared.llm_usage import record_anthropic_call, record_failure
from shared.account_manager import DEFAULT_LEVERAGE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Sonnet is 5x cheaper than Opus and handles hold/DCA/update_levels
# decisions just as well. Opus was costing $51/day on heartbeat alone.
# Sonnet at the same volume = ~$10/day. The 13-agent committee still
# uses Opus for the judge when you need deep reasoning.
MODEL = "claude-sonnet-4-6"
# 15-minute default cadence — balanced between responsiveness and token cost.
# The hash gate skips Opus when nothing changed, and the hot window (30s)
# activates automatically on campaign open/close for rapid monitoring.
HEARTBEAT_INTERVAL_MINUTES = int(os.environ.get("HEARTBEAT_INTERVAL_MINUTES", "60"))
HEARTBEAT_INTERVAL_SECONDS = HEARTBEAT_INTERVAL_MINUTES * 60

# Kill-switch defaults
REDIS_KEY_ENABLED = "heartbeat:enabled"
REDIS_KEY_LOCK = "heartbeat:running"
REDIS_KEY_LAST_RUN = "heartbeat:last_run_at"
REDIS_KEY_NEXT_RUN = "heartbeat:next_run_at"
REDIS_KEY_LAST_HASH = "heartbeat:last_context_hash"
REDIS_KEY_LAST_HASH_TS = "heartbeat:last_context_hash_ts"
# Hot window — when a campaign opens or closes, we switch to a faster
# tick cadence (every 30s) for a short window so Opus is actively
# monitoring the new position or validating the close. Set by ai-brain
# when it publishes campaign_opened / campaign_*_close events.
REDIS_KEY_HOT_UNTIL = "heartbeat:hot_until"
HOT_WINDOW_SECONDS = 5 * 60  # 5 minutes of aggressive monitoring
HOT_TICK_INTERVAL_SECONDS = 30  # tick every 30 seconds while hot
LOCK_TTL_SECONDS = 120  # longer than a single Opus call (~30-60s)

# Hash-gating config — skip the Opus call entirely when the decision
# signal is unchanged AND a decision is less than this many seconds old.
# We still run Opus at least every HASH_MAX_SKIP_SECONDS even if the
# hash matches, so slow-creeping state changes aren't missed.
# 15 min hard ceiling — with 5-min ticks, that means at most 3 ticks
# in a row can skip before Opus is forced to re-reason.
HASH_MAX_SKIP_SECONDS = 15 * 60

# Status ping config — even when Opus is holding quietly, we want to
# see the position state on Telegram regularly. Per-campaign cadence,
# independent of whether Opus was called or the hash gate fired.
STATUS_PING_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours between status pings per campaign

# Early-wake rules — conditions that force an IMMEDIATE status ping
# regardless of the cooldown timer. These are "something just happened
# that the user needs to see NOW" thresholds.
EARLY_WAKE_PNL_CHANGE_PCT = 3.0     # P/L moved ≥ 3% since last ping
EARLY_WAKE_PRICE_TO_SL_PCT = 1.0    # price within 1% of SL
EARLY_WAKE_PRICE_TO_TP_PCT = 1.0    # price within 1% of TP
REDIS_KEY_STATUS_PING_PREFIX = "heartbeat:status_ping:"  # + campaign_id

# Guardrails
CLOSE_COOLDOWN_MINUTES = 30
INDECISION_PCT_THRESHOLD = 0.5  # refuse close when |pnl_pct| < this

# Redis stream for Telegram alerts (matches main.py STREAM_POSITION)
STREAM_POSITION = "position.event"


# ---------------------------------------------------------------------------
# Opus tool schema — one of these must be returned per open campaign
# ---------------------------------------------------------------------------

MANAGE_CAMPAIGNS_TOOL = {
    "name": "manage_campaigns",
    "description": (
        "Return management decisions for every open campaign. You MUST include "
        "exactly one decision per open campaign — do not forget any. Be "
        "conservative with 'close': only close when the thesis is clearly broken. "
        "Prefer 'update_levels' (tighten SL) over premature 'close'. Use 'hold' "
        "when the thesis still applies and levels do not need adjustment.\n\n"
        "You may ALSO return up to 3 propose_theses entries — forward-looking "
        "conditional plans like 'if price drops to 94 I would reconsider long'. "
        "These do not execute anything; they just get saved for the user to review "
        "and the system will notify them when the trigger fires."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "description": "One decision per open campaign.",
                "items": {
                    "type": "object",
                    "properties": {
                        "campaign_id": {
                            "type": "integer",
                            "description": "The campaign id this decision applies to.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["hold", "close", "update_levels", "add_dca"],
                            "description": (
                                "hold = no action, just log reasoning. "
                                "close = market-close all layers. "
                                "update_levels = adjust TP and/or SL in place. "
                                "add_dca = add the next DCA layer at current price "
                                "to build the position. Use when thesis is intact and "
                                "position is small relative to conviction. The 25-layer "
                                "schedule starts at $300 and grows — use add_dca liberally "
                                "when you believe in the thesis."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "Short rationale citing specific evidence "
                                "(score value, news headline, price level). "
                                "1-2 sentences."
                            ),
                        },
                        "new_take_profit": {
                            "type": ["number", "null"],
                            "description": (
                                "Only set when action=update_levels. "
                                "New TP price. Must be above current price for "
                                "LONG, below for SHORT. Pass null to leave unchanged."
                            ),
                        },
                        "new_stop_loss": {
                            "type": ["number", "null"],
                            "description": (
                                "Only set when action=update_levels. "
                                "New SL price. Must be below current price for "
                                "LONG, above for SHORT. Pass null to leave unchanged."
                            ),
                        },
                    },
                    "required": ["campaign_id", "action", "reason"],
                },
            },
            "overall_rationale": {
                "type": "string",
                "description": "One paragraph summarising the market read for this tick.",
            },
            "propose_theses": {
                "type": "array",
                "description": (
                    "Optional — up to 3 forward-looking conditional plans. "
                    "Each proposal becomes a pending thesis row in the campaign "
                    "domain. The system will watch the trigger condition and "
                    "alert the user when it fires. Never executes anything."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short title, ≤ 120 chars"},
                        "thesis_text": {"type": "string", "description": "Full reasoning, 2-4 sentences"},
                        "trigger_type": {
                            "type": "string",
                            "enum": [
                                "price_cross_above",
                                "price_cross_below",
                                "score_above",
                                "score_below",
                                "time_elapsed",
                            ],
                        },
                        "trigger_params": {
                            "type": "object",
                            "description": (
                                "price_cross_*: {price: number}. "
                                "score_*: {score: number, score_key?: 'unified'}. "
                                "time_elapsed: {minutes: number}."
                            ),
                        },
                        "planned_action": {"type": "string", "enum": ["LONG", "SHORT", "CLOSE_EXISTING", "WATCH"]},
                        "planned_entry": {"type": ["number", "null"]},
                        "planned_stop_loss": {"type": ["number", "null"]},
                        "planned_take_profit": {"type": ["number", "null"]},
                        "planned_size_margin": {"type": ["number", "null"]},
                    },
                    "required": ["title", "thesis_text", "trigger_type", "trigger_params", "planned_action"],
                },
            },
        },
        "required": ["decisions", "overall_rationale"],
    },
}


SYSTEM_PROMPT = """You are the LIVE POSITION MANAGER for a WTI crude oil trading bot.

Every 15 minutes you review every open campaign and decide what to do.
Your decisions are EXECUTED IMMEDIATELY — there is no human approval step.
You have full authority to close campaigns or adjust their take-profit /
stop-loss levels. Be deliberate.

Your toolbox per campaign:

  • hold            — do nothing, just log your reasoning
  • close           — market-close all DCA layers in this campaign
  • update_levels   — change take_profit and/or stop_loss in place
  • add_dca         — add the next DCA layer at current price to BUILD
                      the position. The 25-layer schedule starts at $300
                      and grows. Use add_dca LIBERALLY when the thesis is
                      intact and the position is small. A 1-layer $300
                      campaign on a strong thesis is UNDER-SIZED — keep
                      adding layers every few ticks to build conviction.

You must return EXACTLY ONE decision per open campaign in the `decisions`
array. Do not forget any campaign. Do not return two decisions for the
same campaign.

RULES:

1. CONSERVATIVE CLOSE BIAS. Only close when the original thesis is clearly
   broken — e.g. a news catalyst the position was riding just reversed, or
   a key level broke with conviction. If you're uncertain, prefer
   `update_levels` (tighten SL to lock in gains or limit further loss)
   over `close`.

2. PREFER UPDATE_LEVELS OVER CLOSE. Moving SL to break-even or just above
   entry is often the right move — it locks in profit without giving up
   upside. This is your trailing-stop tool.

3. RESPECT THE ENTRY THESIS. Each campaign has an `entry_snapshot` with
   the original reasoning. Do not close just because a single indicator
   flipped — you need a meaningful change from the original setup.

4. CITE EVIDENCE. Every reason must reference specific data: a score
   value, a news headline from recent_news, a price level, the P/L %.
   Vague rationales are useless for audit.

5. INDECISION. If a campaign's unrealized P/L is within ±0.5% of
   break-even, prefer `hold` — the market hasn't told you anything yet.

6. LEVEL VALIDATION. When action=update_levels, your new levels MUST be
   on the correct side of the CURRENT price:
     - LONG:  TP > current_price, SL < current_price
     - SHORT: TP < current_price, SL > current_price
   Invalid levels are rejected by the system and logged as errors.

7. LAST HEARTBEAT CONTEXT. Each campaign shows last_heartbeat_decision.
   Don't flip-flop between ticks without new evidence — if you held last
   tick, holding again is fine; but if you're changing your mind, cite
   what changed.

8. RANGE BIAS. The context contains `range_bias` — the 30-day rolling
   range position. Check `position_pct` (0=bottom, 100=top) and `bias`.
   A LONG campaign near the top of the range (>75%) should have a
   TIGHTER SL — mean-reversion risk is high. A SHORT near the bottom
   (<25%) same. Reference range levels in your reasoning when relevant
   (e.g. "price at 82% of 30-day range, nearing resistance").

9. PENDING THESES ARE IDEAS YOU ALREADY HAVE. The context contains a
   `pending_theses` list — forward-looking plans from the user (chat),
   previous heartbeat ticks (including your own earlier proposals),
   and the scalp brain. These are CONDITIONS THE BOT IS ALREADY
   WATCHING. You should:
     a) USE them when reasoning — if a pending thesis says 'close if
        unified score ≥ 15', and you're considering a close because
        score is rising, reference the thesis in your reason.
     b) NEVER PROPOSE DUPLICATES. Before emitting a propose_theses
        entry, scan the pending list: is there already a thesis with
        the same trigger_type + similar trigger_params + same planned
        side? If yes, don't propose another one.
     c) PROPOSE SPARINGLY. At most 1-2 new theses per tick — only when
        you identify a genuinely new condition worth watching.
     d) The user sees pending theses in the dashboard Theses tab; you
        can assume they know about them.

Use the manage_campaigns tool to return your decisions.
"""


# ---------------------------------------------------------------------------
# Redis kill-switch helpers
# ---------------------------------------------------------------------------

def _redis_str(val) -> str | None:
    """Coerce a Redis GET result (bytes or str) to a plain string."""
    if val is None:
        return None
    if isinstance(val, bytes):
        return val.decode("utf-8")
    return str(val)


def is_enabled() -> bool:
    """Return True unless the kill-switch is explicitly off.

    Precedence: HEARTBEAT_ENABLED env var (if set to 'false'/'0') > Redis flag.
    """
    env_override = os.environ.get("HEARTBEAT_ENABLED", "").lower()
    if env_override in ("false", "0", "no", "off"):
        return False

    try:
        r = get_redis()
        val = _redis_str(r.get(REDIS_KEY_ENABLED))
        if val is None:
            # Default to ENABLED on first run
            r.set(REDIS_KEY_ENABLED, "true")
            return True
        return val.lower() == "true"
    except Exception:
        logger.exception("Failed to read heartbeat:enabled, defaulting to True")
        return True


def set_enabled(enabled: bool) -> None:
    try:
        get_redis().set(REDIS_KEY_ENABLED, "true" if enabled else "false")
    except Exception:
        logger.exception("Failed to set heartbeat:enabled")


def set_hot_window(duration_seconds: int = HOT_WINDOW_SECONDS) -> None:
    """Activate fast-tick mode for `duration_seconds` from now.

    Call this from ai-brain main.py whenever a campaign opens or closes —
    the heartbeat loop will tick every HOT_TICK_INTERVAL_SECONDS instead
    of HEARTBEAT_INTERVAL_SECONDS until the window expires. Used to
    aggressively monitor new positions and validate closes.
    """
    try:
        r = get_redis()
        hot_until = time.time() + max(1, int(duration_seconds))
        r.set(REDIS_KEY_HOT_UNTIL, str(hot_until))
        logger.info("Heartbeat HOT window armed for %ds", duration_seconds)
    except Exception:
        logger.exception("Failed to set heartbeat hot window")


def is_hot_window_active() -> bool:
    """True if the hot window is currently in effect."""
    try:
        raw = _redis_str(get_redis().get(REDIS_KEY_HOT_UNTIL))
        if raw is None:
            return False
        return time.time() < float(raw)
    except Exception:
        return False


def hot_window_seconds_left() -> float:
    try:
        raw = _redis_str(get_redis().get(REDIS_KEY_HOT_UNTIL))
        if raw is None:
            return 0.0
        remaining = float(raw) - time.time()
        return max(0.0, remaining)
    except Exception:
        return 0.0


def _acquire_lock() -> bool:
    """Try to acquire the heartbeat lock. Returns False if another tick is running."""
    try:
        r = get_redis()
        # SET NX EX — atomic acquire-or-fail
        acquired = r.set(REDIS_KEY_LOCK, "1", nx=True, ex=LOCK_TTL_SECONDS)
        return bool(acquired)
    except Exception:
        logger.exception("Failed to acquire heartbeat lock")
        return False


def _release_lock() -> None:
    try:
        get_redis().delete(REDIS_KEY_LOCK)
    except Exception:
        logger.exception("Failed to release heartbeat lock")


def _set_timestamps(last_run: datetime, next_run: datetime) -> None:
    try:
        r = get_redis()
        r.set(REDIS_KEY_LAST_RUN, last_run.isoformat())
        r.set(REDIS_KEY_NEXT_RUN, next_run.isoformat())
    except Exception:
        logger.exception("Failed to write heartbeat timestamps")


# ---------------------------------------------------------------------------
# Context assembly — everything Opus needs to decide
# ---------------------------------------------------------------------------

def _get_open_campaign_ids() -> list[int]:
    with SessionLocal() as session:
        rows = (
            session.query(Campaign.id)
            .filter(Campaign.status == "open")
            .order_by(Campaign.id)
            .all()
        )
        return [r[0] for r in rows]


def _get_last_heartbeat_for_campaign(campaign_id: int) -> dict | None:
    with SessionLocal() as session:
        row = (
            session.query(HeartbeatRun)
            .filter(HeartbeatRun.campaign_id == campaign_id)
            .order_by(HeartbeatRun.ran_at.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "decision": row.decision,
            "reason": row.reason,
            "ran_at": row.ran_at.isoformat(),
            "executed": row.executed,
        }


def _get_latest_scores() -> dict | None:
    with SessionLocal() as session:
        row = (
            session.query(AnalysisScore)
            .order_by(AnalysisScore.timestamp.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "timestamp": row.timestamp.isoformat(),
            "unified_score": row.unified_score,
            "technical_score": row.technical_score,
            "fundamental_score": row.fundamental_score,
            "sentiment_score": row.sentiment_score,
            "shipping_score": row.shipping_score,
        }


def _get_recent_news(minutes: int = 30) -> list[dict]:
    since = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    with SessionLocal() as session:
        rows = (
            session.query(KnowledgeSummary)
            .filter(KnowledgeSummary.timestamp >= since)
            .order_by(KnowledgeSummary.timestamp.desc())
            .limit(3)  # 3 not 5 — saves ~800 tokens per tick
            .all()
        )
        return [
            {
                "ts": r.timestamp.isoformat(),
                "summary": (r.summary or "")[:200],  # 200 not 400
                "sentiment": r.sentiment_label,
            }
            for r in rows
        ]


def _get_account_snapshot() -> dict | None:
    try:
        state = recompute_account_state()
        return {
            "equity": state.get("equity"),
            "cash": state.get("cash"),
            "free_margin": state.get("free_margin"),
            "margin_used": state.get("margin_used"),
            "account_drawdown_pct": state.get("account_drawdown_pct"),
        }
    except Exception:
        logger.exception("Failed to recompute account state for heartbeat")
        return None


def _get_pending_theses_for_context(limit: int = 20) -> list[dict]:
    """Load currently-pending theses so Opus can use them as ideas.

    Returns a compact list (campaign + scalp domains) sorted by most
    recent first, with enough fields for Opus to understand each
    thesis's intent and trigger condition but not so many that the
    context bloats.

    Filters out smoke-test rows so Opus never considers test noise.
    """
    try:
        from shared.models.theses import Thesis
        with SessionLocal() as session:
            rows = (
                session.query(Thesis)
                .filter(Thesis.status == "pending")
                .filter(~Thesis.created_by.like("smoke%"))
                .order_by(Thesis.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "domain": r.domain,
                    "created_by": r.created_by,
                    "title": r.title,
                    "thesis_text": (r.thesis_text or "")[:500],
                    "trigger_type": r.trigger_type,
                    "trigger_params": r.trigger_params,
                    "planned_action": r.planned_action,
                    "planned_entry": r.planned_entry,
                    "planned_stop_loss": r.planned_stop_loss,
                    "planned_take_profit": r.planned_take_profit,
                }
                for r in rows
            ]
    except Exception:
        logger.exception("Failed to load pending theses for heartbeat context")
        return []


def _build_campaign_snapshot(campaign_id: int, current_price: float | None) -> dict | None:
    """Build the per-campaign payload for Opus."""
    state = compute_campaign_state(campaign_id, current_price)
    if state is None:
        return None

    # Need entry_snapshot — compute_campaign_state doesn't include it
    with SessionLocal() as session:
        camp = session.query(Campaign).filter(Campaign.id == campaign_id).first()
        entry_snapshot = camp.entry_snapshot if camp is not None else None
        opened_at_dt = camp.opened_at if camp is not None else None

    age_hours = None
    if opened_at_dt is not None:
        age_hours = round(
            (datetime.now(tz=timezone.utc) - opened_at_dt).total_seconds() / 3600.0, 2
        )

    return {
        "id": campaign_id,
        "side": state.get("side"),
        "avg_entry": state.get("avg_entry_price"),
        "layers": state.get("layers_used"),
        "max_layers": state.get("max_layers"),
        "total_lots": state.get("total_lots"),
        "total_margin": state.get("total_margin"),
        "total_nominal": state.get("total_nominal"),
        "leverage": DEFAULT_LEVERAGE,
        "unrealized_pnl_usd": state.get("unrealised_pnl"),
        "unrealized_pnl_pct": state.get("unrealised_pnl_pct"),
        "take_profit": state.get("take_profit"),
        "stop_loss": state.get("stop_loss"),
        "size_multiplier": state.get("size_multiplier"),
        "opened_at": state.get("opened_at"),
        "age_hours": age_hours,
        "entry_thesis": (
            entry_snapshot.get("reason") if isinstance(entry_snapshot, dict) else None
        ),
        "entry_snapshot": entry_snapshot if isinstance(entry_snapshot, dict) else None,
        "last_heartbeat_decision": _get_last_heartbeat_for_campaign(campaign_id),
    }


def _build_context(open_campaign_ids: list[int]) -> dict:
    current_price = get_current_price()
    campaigns = [
        _build_campaign_snapshot(cid, current_price)
        for cid in open_campaign_ids
    ]
    campaigns = [c for c in campaigns if c is not None]

    return {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "heartbeat_interval_minutes": HEARTBEAT_INTERVAL_MINUTES,
        "current_price": current_price,
        "account": _get_account_snapshot(),
        "latest_scores": _get_latest_scores(),
        "recent_news": _get_recent_news(30),
        "open_campaigns": campaigns,
        # Pending forward-looking plans from user, prior heartbeats, and
        # the scalp brain. Opus should read these as IDEAS — hints about
        # conditions the bot is already watching for. Opus can validate,
        # absorb, or ignore them, but must NEVER duplicate one already
        # in this list (the propose_theses dedupe does the mechanical
        # check but Opus should self-filter first).
        # Limit to 5 theses to keep context small (was 20)
        "pending_theses": _get_pending_theses_for_context(limit=5),
        # 30-day range position — tells Opus where price sits in the
        # broader range so it can factor mean-reversion risk into its
        # hold/close/update_levels decisions.
        "range_bias": _get_range_bias(),
    }


def _get_range_bias() -> dict | None:
    try:
        from shared.range_bias import compute_range_bias
        return compute_range_bias()
    except Exception:
        logger.exception("Failed to compute range bias for heartbeat")
        return None


# ---------------------------------------------------------------------------
# Opus call
# ---------------------------------------------------------------------------

def _call_opus(context: dict) -> dict | None:
    """Ask Opus to manage the open campaigns. Returns parsed tool-use input or None."""
    client = Anthropic(api_key=settings.anthropic_api_key)

    user_prompt = (
        "Review the open campaigns and return your management decisions.\n\n"
        "## Context\n"
        f"{json.dumps(context, indent=2, default=str)}\n\n"
        "Return EXACTLY one decision per open campaign using the "
        "manage_campaigns tool."
    )

    call_start = time.time()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            # Prompt caching: the SYSTEM_PROMPT and tool schema don't change
            # between ticks. Wrapping system as a cache-controlled block
            # makes Anthropic bill cached input at 10% of normal rate on
            # hits within the 5-min cache window. Since we tick every
            # 15 min, we still get a cache hit on any burst of testing
            # or back-to-back runs — and zero cost for hits that happen.
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[MANAGE_CAMPAIGNS_TOOL],
            tool_choice={"type": "tool", "name": "manage_campaigns"},
            messages=[{"role": "user", "content": user_prompt}],
        )
        record_anthropic_call(
            call_site="heartbeat.opus",
            model=MODEL,
            usage=response.usage,
            duration_ms=(time.time() - call_start) * 1000,
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "manage_campaigns":
                return block.input
    except Exception:
        logger.exception("Heartbeat Opus call failed")
        record_failure(
            call_site="heartbeat.opus",
            model=MODEL,
            provider="anthropic",
            duration_ms=(time.time() - call_start) * 1000,
        )
        return None

    logger.error("Heartbeat Opus did not return a manage_campaigns tool_use block")
    return None


# ---------------------------------------------------------------------------
# Guardrails + execution
# ---------------------------------------------------------------------------

def _has_recent_close(campaign_id: int) -> bool:
    """True if this campaign had an executed close decision in the last 30 min."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=CLOSE_COOLDOWN_MINUTES)
    with SessionLocal() as session:
        row = (
            session.query(HeartbeatRun)
            .filter(
                HeartbeatRun.campaign_id == campaign_id,
                HeartbeatRun.decision == "close",
                HeartbeatRun.executed == True,  # noqa: E712
                HeartbeatRun.ran_at >= cutoff,
            )
            .first()
        )
        return row is not None


def _record_run(
    ran_at: datetime,
    campaign_id: int | None,
    decision: str,
    reason: str | None,
    opus_raw: dict | None,
    executed: bool,
    duration_seconds: float | None,
) -> None:
    try:
        with SessionLocal() as session:
            row = HeartbeatRun(
                ran_at=ran_at,
                campaign_id=campaign_id,
                decision=decision,
                reason=(reason or "")[:2000],
                opus_raw=opus_raw,
                executed=executed,
                duration_seconds=duration_seconds,
            )
            session.add(row)
            session.commit()
    except Exception:
        logger.exception("Failed to persist HeartbeatRun")


def _publish_heartbeat_action(
    campaign_id: int,
    action: str,
    reason: str,
    extra: dict | None = None,
) -> None:
    payload = {
        "type": "heartbeat_action",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "campaign_id": campaign_id,
        "action": action,
        "reason": reason,
    }
    if extra:
        payload.update(extra)
    try:
        publish(STREAM_POSITION, payload)
        logger.info("Published heartbeat_action for campaign #%s: %s", campaign_id, action)
    except Exception:
        logger.exception("Failed to publish heartbeat_action event")


def _execute_decision(
    decision: dict,
    context_campaigns: list[dict],
    ran_at: datetime,
    opus_raw: dict,
) -> None:
    """Execute one per-campaign decision from Opus, logging and alerting."""
    campaign_id = decision.get("campaign_id")
    action = decision.get("action")
    reason = decision.get("reason") or ""

    if not isinstance(campaign_id, int) or action not in ("hold", "close", "update_levels", "add_dca"):
        logger.error("Malformed heartbeat decision: %r", decision)
        _record_run(ran_at, campaign_id, "error", f"malformed: {decision!r}", opus_raw, False, None)
        return

    # Find the campaign in our context — Opus shouldn't invent ids
    camp_ctx = next((c for c in context_campaigns if c.get("id") == campaign_id), None)
    if camp_ctx is None:
        logger.warning(
            "Heartbeat: Opus returned decision for unknown campaign #%s — ignoring",
            campaign_id,
        )
        _record_run(
            ran_at, campaign_id, "error",
            "opus returned unknown campaign_id", opus_raw, False, None,
        )
        return

    pnl_pct = camp_ctx.get("unrealized_pnl_pct") or 0.0

    # --- HOLD: just log, no action ---
    if action == "hold":
        logger.info("Heartbeat hold #%s: %s", campaign_id, reason[:120])
        _record_run(ran_at, campaign_id, "hold", reason, opus_raw, True, None)
        return

    # --- ADD_DCA: scale into the position ---
    if action == "add_dca":
        # Cooldown: max 1 DCA per 10 minutes from heartbeat.
        # Without this, the hot window (30s ticks) causes Sonnet to
        # add a layer every tick because it keeps seeing "undersized".
        HEARTBEAT_DCA_COOLDOWN_KEY = f"heartbeat:last_dca:{campaign_id}"
        try:
            r = get_redis()
            last_raw = _redis_str(r.get(HEARTBEAT_DCA_COOLDOWN_KEY))
            if last_raw is not None:
                try:
                    age = time.time() - float(last_raw)
                    if age < 10 * 60:  # 10 min cooldown
                        logger.info(
                            "Heartbeat DCA #%s COOLDOWN: last DCA %.0fs ago (need 600s)",
                            campaign_id, age,
                        )
                        _record_run(ran_at, campaign_id, "hold", f"DCA cooldown ({age:.0f}s ago): {reason}", opus_raw, True, None)
                        return
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass

        try:
            from shared.position_manager import add_dca_layer
            current_price = get_current_price()
            if current_price is None:
                _record_run(ran_at, campaign_id, "add_dca", f"FAILED (no price): {reason}", opus_raw, False, None)
                return
            new_pos_id = add_dca_layer(campaign_id, current_price)
            if new_pos_id is not None:
                # Fetch updated campaign state to show full position info
                updated = compute_campaign_state(campaign_id, current_price)
                layers_now = (updated or {}).get("layers_used", 0)
                max_lay = (updated or {}).get("max_layers", 25)
                total_margin = (updated or {}).get("total_margin", 0)
                total_lots = (updated or {}).get("total_lots", 0)
                total_nominal = (updated or {}).get("total_nominal", 0)
                avg_entry = (updated or {}).get("avg_entry_price", 0)
                unrealised = (updated or {}).get("unrealised_pnl", 0)
                unrealised_pct = (updated or {}).get("unrealised_pnl_pct", 0)

                logger.info("Heartbeat DCA #%s: layer %d added @ %.3f — %s", campaign_id, layers_now, current_price, reason[:120])
                _record_run(ran_at, campaign_id, "add_dca", reason, opus_raw, True, None)
                try:
                    get_redis().set(HEARTBEAT_DCA_COOLDOWN_KEY, str(time.time()))
                except Exception:
                    pass
                _publish_heartbeat_action(
                    campaign_id, "add_dca", reason,
                    extra={
                        "side": camp_ctx.get("side"),
                        "price": current_price,
                        "avg_entry": avg_entry,
                        "layers": layers_now,
                        "max_layers": max_lay,
                        "total_margin": total_margin,
                        "total_lots": total_lots,
                        "total_nominal": total_nominal,
                        "leverage": DEFAULT_LEVERAGE,
                        "unrealized_pnl_usd": unrealised,
                        "unrealized_pnl_pct": unrealised_pct,
                    },
                )
            else:
                logger.info("Heartbeat DCA #%s: add_dca_layer returned None (layers exhausted or equity cap)", campaign_id)
                _record_run(ran_at, campaign_id, "add_dca", f"REJECTED (cap/exhausted): {reason}", opus_raw, False, None)
        except Exception:
            logger.exception("Heartbeat add_dca(#%s) raised", campaign_id)
            _record_run(ran_at, campaign_id, "add_dca", f"FAILED: {reason}", opus_raw, False, None)
        return

    # --- CLOSE: guardrails then execute ---
    if action == "close":
        # Indecision guard
        if abs(pnl_pct) < INDECISION_PCT_THRESHOLD:
            logger.info(
                "Heartbeat close BLOCKED for #%s (indecision guard, pnl %.2f%%): %s",
                campaign_id, pnl_pct, reason[:120],
            )
            _record_run(
                ran_at, campaign_id, "close",
                f"BLOCKED (indecision {pnl_pct:.2f}%): {reason}",
                opus_raw, False, None,
            )
            return

        # Cooldown guard
        if _has_recent_close(campaign_id):
            logger.info(
                "Heartbeat close BLOCKED for #%s (cooldown): %s",
                campaign_id, reason[:120],
            )
            _record_run(
                ran_at, campaign_id, "close",
                f"BLOCKED (cooldown): {reason}", opus_raw, False, None,
            )
            return

        # Execute close
        try:
            snap = close_campaign(campaign_id, status="closed_strategy", notes=f"heartbeat: {reason}")
        except Exception:
            logger.exception("Heartbeat close_campaign(#%s) raised", campaign_id)
            _record_run(
                ran_at, campaign_id, "close",
                f"EXECUTION FAILED: {reason}", opus_raw, False, None,
            )
            return

        if snap is None:
            _record_run(
                ran_at, campaign_id, "close",
                f"close_campaign returned None: {reason}", opus_raw, False, None,
            )
            return

        logger.warning(
            "Heartbeat CLOSED campaign #%s: %s (pnl %.2f%%)",
            campaign_id, reason[:120], pnl_pct,
        )
        _record_run(ran_at, campaign_id, "close", reason, opus_raw, True, None)
        _publish_heartbeat_action(
            campaign_id,
            "close",
            reason,
            extra={
                "side": camp_ctx.get("side"),
                "realized_pnl": snap.get("realized_pnl") or snap.get("realised_pnl"),
                "pnl_pct_at_close": pnl_pct,
                "total_margin": camp_ctx.get("total_margin"),
                "total_lots": camp_ctx.get("total_lots"),
                "total_nominal": camp_ctx.get("total_nominal"),
                "leverage": camp_ctx.get("leverage") or DEFAULT_LEVERAGE,
                "avg_entry": camp_ctx.get("avg_entry"),
            },
        )
        # Arm the hot window — we just closed a position and want to
        # aggressively reconsider for the next 5 min (re-entry? opposite?)
        try:
            from shared.heartbeat_hot import arm_hot_window
            arm_hot_window(reason=f"heartbeat closed #{campaign_id}")
        except Exception:
            logger.exception("Failed to arm hot window after heartbeat close")
        return

    # --- UPDATE_LEVELS: validate + execute ---
    if action == "update_levels":
        new_tp = decision.get("new_take_profit")
        new_sl = decision.get("new_stop_loss")
        if new_tp is None and new_sl is None:
            logger.warning(
                "Heartbeat update_levels #%s with no new levels — treating as hold",
                campaign_id,
            )
            _record_run(
                ran_at, campaign_id, "hold",
                f"update_levels with no new levels: {reason}",
                opus_raw, True, None,
            )
            return

        try:
            result = update_campaign_levels(campaign_id, take_profit=new_tp, stop_loss=new_sl)
        except Exception:
            logger.exception("Heartbeat update_campaign_levels(#%s) raised", campaign_id)
            _record_run(
                ran_at, campaign_id, "update_levels",
                f"EXECUTION FAILED: {reason}", opus_raw, False, None,
            )
            return

        if result is None:
            logger.warning(
                "Heartbeat update_levels REJECTED for #%s (validation): tp=%s sl=%s",
                campaign_id, new_tp, new_sl,
            )
            _record_run(
                ran_at, campaign_id, "update_levels",
                f"REJECTED (validation) tp={new_tp} sl={new_sl}: {reason}",
                opus_raw, False, None,
            )
            return

        logger.info(
            "Heartbeat UPDATED levels #%s: TP %s->%s, SL %s->%s (%s)",
            campaign_id,
            result["old_take_profit"], result["new_take_profit"],
            result["old_stop_loss"], result["new_stop_loss"],
            reason[:80],
        )
        _record_run(ran_at, campaign_id, "update_levels", reason, opus_raw, True, None)
        _publish_heartbeat_action(
            campaign_id,
            "update_levels",
            reason,
            extra={
                "side": camp_ctx.get("side"),
                "old_take_profit": result["old_take_profit"],
                "new_take_profit": result["new_take_profit"],
                "old_stop_loss": result["old_stop_loss"],
                "new_stop_loss": result["new_stop_loss"],
                "total_margin": camp_ctx.get("total_margin"),
                "total_lots": camp_ctx.get("total_lots"),
                "total_nominal": camp_ctx.get("total_nominal"),
                "leverage": camp_ctx.get("leverage") or DEFAULT_LEVERAGE,
                "avg_entry": camp_ctx.get("avg_entry"),
                "unrealized_pnl_usd": camp_ctx.get("unrealized_pnl_usd"),
                "unrealized_pnl_pct": camp_ctx.get("unrealized_pnl_pct"),
            },
        )
        return


# ---------------------------------------------------------------------------
# Opus-proposed theses — save forward-looking plans the user can review
# ---------------------------------------------------------------------------


def _proposal_matches_existing(
    proposal: dict, existing_pending: list[dict]
) -> bool:
    """True if a similar pending thesis already exists.

    Match criteria (all must agree):
      - same trigger_type
      - same planned_action (LONG/SHORT/etc)
      - same trigger price rounded to $0.50 (for price triggers)
        OR same trigger score rounded to 5 (for score triggers)
        OR same minutes bucket rounded to 15 (for time_elapsed)

    Keeps Opus from re-proposing "close if unified > 15" every single
    tick when there's already a pending thesis with that condition.
    """
    tt = proposal.get("trigger_type")
    action = proposal.get("planned_action")
    params = proposal.get("trigger_params") or {}

    def _bucket(val, step):
        if val is None:
            return None
        try:
            return round(float(val) / step) * step
        except (TypeError, ValueError):
            return None

    proposed_price = _bucket(params.get("price"), 0.5) if "price" in params else None
    proposed_score = _bucket(params.get("score"), 5) if "score" in params else None
    proposed_mins = _bucket(params.get("minutes"), 15) if "minutes" in params else None

    for existing in existing_pending:
        if existing.get("trigger_type") != tt:
            continue
        if existing.get("planned_action") != action:
            continue
        ex_params = existing.get("trigger_params") or {}
        if proposed_price is not None:
            if _bucket(ex_params.get("price"), 0.5) == proposed_price:
                return True
        if proposed_score is not None:
            if _bucket(ex_params.get("score"), 5) == proposed_score:
                return True
        if proposed_mins is not None:
            if _bucket(ex_params.get("minutes"), 15) == proposed_mins:
                return True
    return False


def _process_proposed_theses(proposals: list, existing_pending: list[dict] | None = None) -> None:
    """Save Opus-proposed theses from a heartbeat tick.

    Caps at 2 per tick and dedupes against existing pending theses so
    we don't spam the theses table (or the Telegram feed) with the
    same 'close if unified > 15' proposal every 5 minutes.

    No thesis_created event is published any more — that was removed
    in favour of a silent save (user sees pending theses in the
    dashboard Theses tab). Only thesis_triggered hits Telegram.
    """
    if not isinstance(proposals, list) or not proposals:
        return

    try:
        from shared.theses import create_thesis
    except Exception:
        logger.exception("heartbeat: failed to import create_thesis")
        return

    existing = existing_pending or []

    accepted = 0
    for proposal in proposals:
        if accepted >= 2:  # hard cap per tick — was 3, dropped to 2
            logger.info("Heartbeat: hit 2-proposal cap, dropping remaining %d", len(proposals) - accepted)
            break
        if not isinstance(proposal, dict):
            continue
        try:
            title = (proposal.get("title") or "").strip()
            thesis_text = (proposal.get("thesis_text") or "").strip()
            trigger_type = proposal.get("trigger_type")
            trigger_params = proposal.get("trigger_params") or {}
            planned_action = proposal.get("planned_action") or "WATCH"
            if not title or not thesis_text or not trigger_type:
                continue

            # Dedupe against existing pending theses
            if _proposal_matches_existing(proposal, existing):
                logger.info(
                    "Heartbeat: dedupe — proposal %r already has a matching pending thesis",
                    title[:60],
                )
                continue

            new_id = create_thesis(
                created_by="heartbeat",
                domain="campaign",
                title=title,
                thesis_text=thesis_text,
                reasoning=None,
                trigger_type=trigger_type,
                trigger_params=trigger_params,
                planned_action=planned_action,
                planned_entry=proposal.get("planned_entry"),
                planned_stop_loss=proposal.get("planned_stop_loss"),
                planned_take_profit=proposal.get("planned_take_profit"),
                planned_size_margin=proposal.get("planned_size_margin"),
            )
            if new_id is None:
                continue
            accepted += 1
            logger.info("Heartbeat proposed thesis #%s: %r", new_id, title[:80])
            # NO thesis_created event — silent save. User sees it in the
            # Theses tab; only thesis_triggered reaches Telegram.
        except Exception:
            logger.exception("heartbeat: failed to save a proposed thesis")


# ---------------------------------------------------------------------------
# Decision-signal hash — skip Opus when nothing materially changed
# ---------------------------------------------------------------------------


def _compute_decision_signal(context: dict) -> str:
    """Build a stable hash of the parts of the context that would actually
    change Opus's decision.

    Deliberately bucketed so trivial drift (a $0.02 price move, a 0.3%
    P/L wiggle) doesn't invalidate the cache. Excluded: the tick's own
    timestamp, and any full-text free-form fields.

    Included:
      - Per campaign: id, side, TP (exact), SL (exact),
        pnl_pct bucketed to 2%, layers_used.
      - Latest unified score bucketed to 5.
      - Current price bucketed to $0.50.
      - Recent news count + the first news summary string (if any) so a
        new headline flips the hash even if the count is unchanged.
    """
    def _bucket(val, step):
        if val is None:
            return None
        try:
            return round(float(val) / step) * step
        except (TypeError, ValueError):
            return None

    signal: dict = {}

    # Price bucket (round to $0.50)
    signal["price_bucket"] = _bucket(context.get("current_price"), 0.5)

    # Unified score bucket (round to 5)
    scores = context.get("latest_scores") or {}
    signal["unified_bucket"] = _bucket(scores.get("unified_score"), 5)

    # Recent news — count + top summary prefix
    news = context.get("recent_news") or []
    signal["news_count"] = len(news)
    signal["news_top"] = (news[0].get("summary", "")[:120] if news else "")

    # Campaigns — the most important signal. Sort by id for stability.
    camps = context.get("open_campaigns") or []
    camp_sigs = []
    for c in sorted(camps, key=lambda x: x.get("id", 0)):
        camp_sigs.append({
            "id": c.get("id"),
            "side": c.get("side"),
            "tp": c.get("take_profit"),
            "sl": c.get("stop_loss"),
            "pnl_pct_bucket": _bucket(c.get("unrealized_pnl_pct"), 2),
            "layers": c.get("layers"),
        })
    signal["campaigns"] = camp_sigs

    payload = json.dumps(signal, sort_keys=True, default=str)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _should_skip_opus(new_hash: str) -> tuple[bool, dict]:
    """Return (skip, detail). Skips if Redis stored hash matches AND the
    stored decision is still fresh (< HASH_MAX_SKIP_SECONDS old).

    During the hot window (just after campaign open/close), we bypass the
    hash gate entirely — we WANT fresh Opus reasoning during transitions.
    """
    if is_hot_window_active():
        return False, {"reason": "hot window active — bypassing hash gate"}
    try:
        r = get_redis()
        prev_hash = _redis_str(r.get(REDIS_KEY_LAST_HASH))
        prev_ts_raw = _redis_str(r.get(REDIS_KEY_LAST_HASH_TS))
        if not prev_hash or not prev_ts_raw:
            return False, {"reason": "no prior hash"}
        try:
            prev_ts = float(prev_ts_raw)
        except (TypeError, ValueError):
            return False, {"reason": "bad stored ts"}
        age = time.time() - prev_ts
        if prev_hash == new_hash and age < HASH_MAX_SKIP_SECONDS:
            return True, {"hash": new_hash, "age_seconds": round(age, 1)}
        return False, {
            "reason": "hash changed" if prev_hash != new_hash else "stored hash stale",
            "age_seconds": round(age, 1),
        }
    except Exception:
        logger.exception("Failed to read heartbeat hash gate")
        return False, {"reason": "redis read failed"}


def _store_decision_hash(new_hash: str) -> None:
    try:
        r = get_redis()
        r.set(REDIS_KEY_LAST_HASH, new_hash)
        r.set(REDIS_KEY_LAST_HASH_TS, str(time.time()))
    except Exception:
        logger.exception("Failed to store heartbeat hash")


# ---------------------------------------------------------------------------
# Status ping — compact per-campaign Telegram message every 20 min
# ---------------------------------------------------------------------------


def _status_ping_due(campaign_id: int) -> bool:
    """Return True if it's been >= STATUS_PING_INTERVAL_SECONDS since
    the last status ping for this campaign (or if no prior ping exists).

    Note: early-wake rules can override this — they're checked separately
    in _should_early_wake() and bypass the timer entirely.
    """
    try:
        r = get_redis()
        last_raw = _redis_str(r.get(f"{REDIS_KEY_STATUS_PING_PREFIX}{campaign_id}"))
        if last_raw is None:
            return True
        try:
            last_ts = float(last_raw)
        except (TypeError, ValueError):
            return True
        return (time.time() - last_ts) >= STATUS_PING_INTERVAL_SECONDS
    except Exception:
        logger.exception("Failed to read status_ping ts for #%s", campaign_id)
        return False  # fail-closed — don't spam on redis errors


def _should_early_wake(camp_ctx: dict) -> tuple[bool, str]:
    """Check if an immediate (early-wake) status ping should fire for
    this campaign even though the normal timer hasn't elapsed yet.

    Rules (any one triggers):
      1. P/L moved ≥ EARLY_WAKE_PNL_CHANGE_PCT since the last ping
      2. Price is within EARLY_WAKE_PRICE_TO_SL_PCT of the stop-loss
      3. Price is within EARLY_WAKE_PRICE_TO_TP_PCT of the take-profit

    Returns (should_wake, reason).
    """
    current_price = None
    try:
        current_price = get_current_price()
    except Exception:
        pass
    if current_price is None:
        return False, ""

    # Rule 2: price close to SL
    sl = camp_ctx.get("stop_loss")
    if sl is not None:
        try:
            sl_f = float(sl)
            if sl_f > 0:
                dist_to_sl_pct = abs(current_price - sl_f) / current_price * 100
                if dist_to_sl_pct <= EARLY_WAKE_PRICE_TO_SL_PCT:
                    return True, f"price ${current_price:.3f} within {dist_to_sl_pct:.2f}% of SL ${sl_f:.3f}"
        except (TypeError, ValueError):
            pass

    # Rule 3: price close to TP
    tp = camp_ctx.get("take_profit")
    if tp is not None:
        try:
            tp_f = float(tp)
            if tp_f > 0:
                dist_to_tp_pct = abs(current_price - tp_f) / current_price * 100
                if dist_to_tp_pct <= EARLY_WAKE_PRICE_TO_TP_PCT:
                    return True, f"price ${current_price:.3f} within {dist_to_tp_pct:.2f}% of TP ${tp_f:.3f}"
        except (TypeError, ValueError):
            pass

    # Rule 1: P/L moved significantly since last ping
    # We track the P/L at last ping in a Redis key per campaign
    pnl_pct = camp_ctx.get("unrealized_pnl_pct")
    if pnl_pct is not None:
        try:
            r = get_redis()
            key = f"{REDIS_KEY_STATUS_PING_PREFIX}{camp_ctx.get('id')}:last_pnl_pct"
            last_raw = _redis_str(r.get(key))
            if last_raw is not None:
                last_pnl_pct = float(last_raw)
                delta = abs(float(pnl_pct) - last_pnl_pct)
                if delta >= EARLY_WAKE_PNL_CHANGE_PCT:
                    return True, f"P/L moved {delta:.1f}% since last ping (was {last_pnl_pct:+.1f}%, now {float(pnl_pct):+.1f}%)"
        except Exception:
            pass

    return False, ""


def _mark_status_ping(campaign_id: int, pnl_pct: float | None = None) -> None:
    try:
        r = get_redis()
        r.set(f"{REDIS_KEY_STATUS_PING_PREFIX}{campaign_id}", str(time.time()))
        # Store current P/L % so the early-wake rule can detect large moves
        if pnl_pct is not None:
            r.set(
                f"{REDIS_KEY_STATUS_PING_PREFIX}{campaign_id}:last_pnl_pct",
                str(float(pnl_pct)),
            )
    except Exception:
        logger.exception("Failed to store status_ping ts for #%s", campaign_id)


def _build_status_ping_payload(camp_ctx: dict, latest_reason: str | None) -> dict:
    """Compact payload for the heartbeat_status Telegram message.

    `camp_ctx` is the per-campaign dict from the heartbeat context —
    contains id, side, avg_entry, unrealized_pnl_pct, unrealized_pnl_usd,
    take_profit, stop_loss, layers, age_hours. We derive distance-to-TP
    and distance-to-SL in percent so the user sees proximity at a glance.
    """
    current_price = None
    # camp_ctx doesn't store current_price directly, but the top-level
    # context does. Fall back to re-reading if needed.
    try:
        current_price = get_current_price()
    except Exception:
        pass

    tp = camp_ctx.get("take_profit")
    sl = camp_ctx.get("stop_loss")

    def _pct_distance(target):
        if target is None or current_price is None or current_price <= 0:
            return None
        return round((target - current_price) / current_price * 100, 2)

    return {
        "type": "heartbeat_status",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "campaign_id": camp_ctx.get("id"),
        "side": camp_ctx.get("side"),
        "current_price": current_price,
        "avg_entry": camp_ctx.get("avg_entry"),
        "unrealized_pnl_usd": camp_ctx.get("unrealized_pnl_usd"),
        "unrealized_pnl_pct": camp_ctx.get("unrealized_pnl_pct"),
        "take_profit": tp,
        "stop_loss": sl,
        "distance_to_tp_pct": _pct_distance(tp),
        "distance_to_sl_pct": _pct_distance(sl),
        "layers": camp_ctx.get("layers"),
        "max_layers": camp_ctx.get("max_layers"),
        "age_hours": camp_ctx.get("age_hours"),
        # Sizing — so the user sees "margin × leverage = notional exposure"
        "total_margin": camp_ctx.get("total_margin"),
        "total_lots": camp_ctx.get("total_lots"),
        "total_nominal": camp_ctx.get("total_nominal"),
        "leverage": camp_ctx.get("leverage") or DEFAULT_LEVERAGE,
        # Keep the full Opus reason — Telegram chunking (3800 chars) handles
        # any overflow, so we stop truncating here.
        "latest_reason": (latest_reason or ""),
    }


def _fire_status_pings(
    context_campaigns: list[dict],
    campaigns_with_actions: set[int],
    opus_out: dict | None,
) -> None:
    """Fire Telegram status pings for campaigns that need one.

    Rules:
      - Skip campaigns that had an action (close / update_levels) on this
        tick — they already got a dedicated Telegram alert.
      - Fire only if ≥ STATUS_PING_INTERVAL_SECONDS since the last ping
        for this campaign.
      - Use the Opus decision reason from opus_out if available, else
        fall back to the most recent heartbeat_runs row for the campaign.
    """
    # Build a lookup: campaign_id -> latest reason from this tick's Opus output
    opus_reasons: dict[int, str] = {}
    if isinstance(opus_out, dict):
        for dec in (opus_out.get("decisions") or []):
            cid = dec.get("campaign_id")
            reason = dec.get("reason")
            if isinstance(cid, int) and reason:
                opus_reasons[cid] = str(reason)

    for camp in context_campaigns:
        cid = camp.get("id")
        if not isinstance(cid, int):
            continue
        if cid in campaigns_with_actions:
            continue  # action already fired its own alert

        # Check early-wake rules first (bypass the timer)
        early_wake, wake_reason = _should_early_wake(camp)
        if not early_wake and not _status_ping_due(cid):
            continue
        if early_wake:
            logger.info("Heartbeat early-wake for #%s: %s", cid, wake_reason)

        reason = opus_reasons.get(cid)
        if not reason:
            # Fall back to latest heartbeat_runs row
            try:
                with SessionLocal() as session:
                    row = (
                        session.query(HeartbeatRun)
                        .filter(HeartbeatRun.campaign_id == cid)
                        .order_by(HeartbeatRun.ran_at.desc())
                        .first()
                    )
                    if row is not None:
                        reason = row.reason
            except Exception:
                logger.exception("Failed to read latest heartbeat_run for #%s", cid)

        payload = _build_status_ping_payload(camp, reason)
        if early_wake and wake_reason:
            payload["early_wake_reason"] = wake_reason
        try:
            publish(STREAM_POSITION, payload)
            _mark_status_ping(cid, pnl_pct=camp.get("unrealized_pnl_pct"))
            logger.info(
                "Fired heartbeat_status ping for campaign #%s%s",
                cid, f" [EARLY WAKE: {wake_reason}]" if early_wake else "",
            )
        except Exception:
            logger.exception("Failed to publish heartbeat_status for #%s", cid)


# ---------------------------------------------------------------------------
# Urgent entry — open a campaign without waiting for the analyzer
# ---------------------------------------------------------------------------


def _maybe_urgent_entry() -> None:
    """When no campaigns are open and Opus wants to trade with high
    conviction, trigger the entry immediately instead of waiting for
    the analyzer's throttled score cycle (which can be 8+ min).

    Conditions (ALL must be true):
      - No open main campaigns
      - Latest AIRecommendation action is BUY or SELL
      - Recommendation is fresh (< 30 min old)
      - Confidence ≥ 0.60
      - Market is open
      - Cooldown on urgent entries (max 1 per 15 min to prevent spam)

    When fired, it calls _handle_campaign_signal from main.py which
    evaluates all entry gates (range bias, tech score with opus override,
    loss cooldown, staleness). If the gates block it, nothing happens.
    """
    # Check market hours
    try:
        from shared.market_hours import is_market_open
        if not is_market_open():
            return
    except Exception:
        return

    # Check cooldown — max 1 urgent entry per 15 min
    URGENT_COOLDOWN_KEY = "heartbeat:last_urgent_entry"
    try:
        r = get_redis()
        last_raw = _redis_str(r.get(URGENT_COOLDOWN_KEY))
        if last_raw is not None:
            try:
                if (time.time() - float(last_raw)) < 15 * 60:
                    return  # still cooling down
            except (TypeError, ValueError):
                pass
    except Exception:
        return

    # Get latest recommendation
    try:
        from shared.models.signals import AIRecommendation
        with SessionLocal() as session:
            rec = (
                session.query(AIRecommendation)
                .order_by(AIRecommendation.timestamp.desc())
                .first()
            )
            if rec is None:
                return

            # Must be fresh (< 30 min)
            if rec.timestamp is not None:
                age_min = (datetime.now(tz=timezone.utc) - rec.timestamp).total_seconds() / 60
                if age_min > 30:
                    return

            action = (rec.action or "").upper()
            if action not in ("BUY", "SELL"):
                return

            conf = rec.confidence or 0
            if conf < 0.60:
                return

            # Build the rec dict that _handle_campaign_signal expects
            rec_dict = {
                "action": action,
                "confidence": conf,
                "opus_override_score": rec.opus_override_score,
                "unified_score": rec.unified_score,
                "take_profit": rec.take_profit,
                "stop_loss": rec.stop_loss,
                "entry_price": rec.entry_price,
                "analysis_text": rec.analysis_text,
            }
    except Exception:
        logger.exception("Heartbeat urgent entry: failed to read recommendation")
        return

    # Map action to side
    side = "LONG" if action == "BUY" else "SHORT"

    logger.warning(
        "Heartbeat URGENT ENTRY: Opus says %s (conf=%.2f) with no open campaigns — "
        "triggering immediately instead of waiting for analyzer",
        action, conf,
    )

    # Mark cooldown BEFORE attempting so we don't spam if it fails
    try:
        get_redis().set(URGENT_COOLDOWN_KEY, str(time.time()))
    except Exception:
        pass

    # Import and call the signal handler from main.py
    try:
        from main import _handle_campaign_signal
        _handle_campaign_signal(action, conf, rec_dict)
    except Exception:
        logger.exception("Heartbeat urgent entry: _handle_campaign_signal failed")


# ---------------------------------------------------------------------------
# One full heartbeat tick
# ---------------------------------------------------------------------------

def run_tick() -> dict:
    """Run one heartbeat tick. Safe to call manually for smoke tests.

    Returns a summary dict describing what happened.
    """
    ran_at = datetime.now(tz=timezone.utc)
    tick_start = time.time()

    if not is_enabled():
        logger.info("Heartbeat: kill-switch OFF, skipping tick")
        return {"status": "disabled", "ran_at": ran_at.isoformat()}

    open_ids = _get_open_campaign_ids()
    if not open_ids:
        # --- URGENT ENTRY CHECK ---
        # No open campaigns. Check if the latest Opus recommendation
        # wants to enter AND all gates pass — if so, trigger it NOW
        # instead of waiting for the analyzer's throttled score cycle.
        # This catches the scenario where Opus is screaming BUY at 72%
        # on breaking Hormuz news but the analyzer won't fire for 8 min.
        try:
            _maybe_urgent_entry()
        except Exception:
            logger.exception("Heartbeat urgent entry check failed")
        return {"status": "flat", "ran_at": ran_at.isoformat()}

    if not _acquire_lock():
        logger.info("Heartbeat: another tick is running, skipping")
        return {"status": "locked", "ran_at": ran_at.isoformat()}

    try:
        # --- SAFETY-CRITICAL: TP/SL check on EVERY tick ---
        # This MUST run before anything else, including the hash gate
        # and context building. The previous design relied on score events
        # to trigger check_tp_sl_hits(), but score events can be 10+ min
        # apart — long enough for price to blow right through an SL
        # without firing. The heartbeat's 5-min (or 30s hot) cadence is
        # the most frequent loop in the system, so TP/SL checks here
        # catch what score events might miss.
        try:
            tp_sl_closed = check_tp_sl_hits()
            for snap in tp_sl_closed:
                kind = "campaign_tp" if snap.get("status") == "closed_tp" else "campaign_hard_stop" if snap.get("status") == "closed_hard_stop" else "sl_hit"
                _publish_heartbeat_action(
                    snap.get("campaign_id") or snap.get("id"),
                    "close",
                    f"TP/SL hit detected by heartbeat tick: {snap.get('status')}",
                    extra={"side": snap.get("side"), "realized_pnl": snap.get("realized_pnl")},
                )
                logger.warning(
                    "Heartbeat TP/SL check closed campaign: %s",
                    snap.get("status"),
                )
        except Exception:
            logger.exception("Heartbeat TP/SL check failed")

        # Re-check open ids — the TP/SL check might have closed some
        open_ids = _get_open_campaign_ids()
        if not open_ids:
            logger.info("Heartbeat: all campaigns closed by TP/SL check")
            return {"status": "tp_sl_closed_all", "ran_at": ran_at.isoformat()}

        context = _build_context(open_ids)
        if not context.get("open_campaigns"):
            logger.info("Heartbeat: context has no open campaigns after build, skipping")
            return {"status": "flat_after_build", "ran_at": ran_at.isoformat()}

        # --- Decision-signal hash gate ---
        # Skip the expensive Opus call entirely when nothing materially
        # changed since the last decision. The hash buckets price/pnl/
        # score so trivial drift doesn't invalidate the cache, and has
        # a 15-min hard ceiling so slow-creeping state is still caught.
        # The TP/SL check above already ran unconditionally, so skipping
        # Opus here cannot miss a stop-loss event.
        decision_hash = _compute_decision_signal(context)
        skip, skip_detail = _should_skip_opus(decision_hash)
        if skip:
            duration = time.time() - tick_start
            logger.info(
                "Heartbeat: context hash unchanged (age %.0fs) — skipping Opus call",
                skip_detail.get("age_seconds", 0),
            )
            _record_run(
                ran_at, None, "skipped_unchanged",
                f"hash={decision_hash[:12]} age={skip_detail.get('age_seconds')}s",
                {"hash": decision_hash, **skip_detail},
                True, duration,
            )
            # Even on skipped ticks, fire status pings for any campaign
            # that's due — the user wants to see position updates on
            # Telegram even when Opus is quietly holding.
            _fire_status_pings(context["open_campaigns"], set(), None)
            return {
                "status": "skipped_unchanged",
                "ran_at": ran_at.isoformat(),
                "duration_seconds": round(duration, 2),
                "hash": decision_hash[:12],
                "hash_age_seconds": skip_detail.get("age_seconds"),
            }

        opus_out = _call_opus(context)
        duration = time.time() - tick_start

        if opus_out is None:
            _record_run(ran_at, None, "error", "opus_call_failed", None, False, duration)
            return {"status": "opus_error", "ran_at": ran_at.isoformat()}

        decisions = opus_out.get("decisions") or []
        if not isinstance(decisions, list) or not decisions:
            _record_run(ran_at, None, "error", "empty_decisions", opus_out, False, duration)
            return {"status": "empty_decisions", "ran_at": ran_at.isoformat()}

        # Execute each decision
        for dec in decisions:
            _execute_decision(dec, context["open_campaigns"], ran_at, opus_out)

        # Process optional forward-looking thesis proposals from Opus.
        # Dedupe against the pending theses that were already in context,
        # so Opus can't re-propose the same 'close if score > 15' on
        # every single tick.
        _process_proposed_theses(
            opus_out.get("propose_theses") or [],
            existing_pending=context.get("pending_theses") or [],
        )

        # Build the set of campaigns that had an executed action this tick.
        # These campaigns already fire their own Telegram alerts, so the
        # status ping path skips them to avoid duplicate notifications.
        # A HOLD decision still qualifies for a status ping.
        campaigns_with_actions: set[int] = set()
        for dec in decisions:
            cid = dec.get("campaign_id")
            act = dec.get("action")
            if isinstance(cid, int) and act in ("close", "update_levels", "add_dca"):
                campaigns_with_actions.add(cid)

        # Fire status pings for HOLD campaigns that are due for one
        _fire_status_pings(context["open_campaigns"], campaigns_with_actions, opus_out)

        # Write a tick-summary row with the overall rationale + duration
        _record_run(
            ran_at, None, "skipped",
            (opus_out.get("overall_rationale") or "")[:1000],
            opus_out, True, duration,
        )

        # Store the decision hash so the NEXT tick can skip if nothing
        # material changed. Only update on successful runs — if Opus
        # failed we want the next tick to retry immediately.
        _store_decision_hash(decision_hash)

        return {
            "status": "ok",
            "ran_at": ran_at.isoformat(),
            "duration_seconds": round(duration, 2),
            "decisions": len(decisions),
            "campaigns": len(open_ids),
        }
    finally:
        _release_lock()


# ---------------------------------------------------------------------------
# Background worker loop
# ---------------------------------------------------------------------------

def run_worker_loop() -> None:
    """Long-running loop — call run_tick() every HEARTBEAT_INTERVAL_SECONDS
    in normal mode, or every HOT_TICK_INTERVAL_SECONDS while the hot window
    is active (~5 min after a campaign opens or closes).

    Designed to be started in a daemon thread from ai-brain/main.py.
    """
    logger.info(
        "Heartbeat worker starting (normal=%dmin, hot=%ds, env_override=%s)",
        HEARTBEAT_INTERVAL_MINUTES,
        HOT_TICK_INTERVAL_SECONDS,
        os.environ.get("HEARTBEAT_ENABLED", "<unset>"),
    )

    # Initial delay — let the service finish booting + other workers start
    time.sleep(30)

    while True:
        # --- MARKET HOURS CHECK ---
        # When WTI is closed (weekends Fri 22:00 → Sun 22:00 UTC), the
        # heartbeat sleeps for 5 min between checks instead of running
        # Opus. No tokens wasted on a closed market. The TP/SL check
        # still runs (it's the first thing in run_tick) so positions
        # are protected even during off-hours.
        try:
            from shared.market_hours import is_market_open
            if not is_market_open():
                logger.info("Heartbeat: market closed — sleeping 5 min (no Opus)")
                time.sleep(300)
                continue
        except Exception:
            pass  # if market_hours check fails, keep running

        hot = is_hot_window_active()
        tick_interval = HOT_TICK_INTERVAL_SECONDS if hot else HEARTBEAT_INTERVAL_SECONDS

        try:
            next_run = datetime.now(tz=timezone.utc) + timedelta(seconds=tick_interval)
            _set_timestamps(datetime.now(tz=timezone.utc), next_run)
            result = run_tick()
            if hot:
                hot_left = hot_window_seconds_left()
                logger.info(
                    "Heartbeat [HOT %ds left] tick result: %s",
                    int(hot_left), result,
                )
            else:
                logger.info("Heartbeat tick result: %s", result)
        except Exception:
            logger.exception("Heartbeat tick crashed unexpectedly")

        time.sleep(tick_interval)

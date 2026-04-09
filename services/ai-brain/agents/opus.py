"""Opus agent — synthesises a final trading recommendation."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from anthropic import Anthropic

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.knowledge import KnowledgeSummary
from shared.models.ohlcv import OHLCV
from shared.models.signals import AIRecommendation

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-6"

SYSTEM_PROMPT = (
    "You are a senior oil market strategist with 20+ years of experience trading WTI crude oil. "
    "Your role is to synthesise quantitative scores, qualitative analysis, and social-media sentiment "
    "into a clear, actionable trading recommendation. You also actively manage existing positions.\n\n"
    "IMPORTANT: All scores in this prompt are on a -100..+100 scale. "
    "0 = neutral, ±50 = strong signal, ±100 = extreme. "
    "Return unified_score on the same -100..+100 scale.\n\n"
    "PRIORITISE the @marketfeed knowledge base — these are the most recent breaking-news events "
    "that move the oil market. In your analysis_text, cite at least one specific key_event by name.\n\n"
    "Use the submit_trading_recommendation tool to return your structured recommendation."
)

FALLBACK_REC: dict = {
    "unified_score": None,
    "opus_override_score": None,
    "confidence": 0.0,
    "action": "WAIT",
    "analysis_text": "Opus synthesis failed — recommend waiting for next cycle.",
    "base_scenario": None,
    "alt_scenario": None,
    "risk_factors": [],
    "entry_price": None,
    "stop_loss": None,
    "take_profit": None,
    "manage_positions": [],
}

RECOMMENDATION_TOOL = {
    "name": "submit_trading_recommendation",
    "description": "Submit a trading recommendation for WTI crude oil based on the analysis",
    "input_schema": {
        "type": "object",
        "properties": {
            "unified_score": {
                "type": "number",
                "description": "Synthesised score on -100..+100 scale",
            },
            "opus_override_score": {
                "type": ["number", "null"],
                "description": "Your score if you disagree with input unified_score",
            },
            "confidence": {"type": "number", "description": "0.0 to 1.0"},
            "action": {
                "type": "string",
                "enum": ["BUY", "SELL", "HOLD", "WAIT"],
            },
            "analysis_text": {"type": "string"},
            "base_scenario": {"type": "string"},
            "alt_scenario": {"type": "string"},
            "risk_factors": {"type": "array", "items": {"type": "string"}},
            "entry_price": {"type": ["number", "null"]},
            "stop_loss": {"type": ["number", "null"]},
            "take_profit": {"type": ["number", "null"]},
            "manage_positions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "action": {"type": "string", "enum": ["hold", "close"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "action", "reason"],
                },
            },
        },
        "required": [
            "unified_score",
            "confidence",
            "action",
            "analysis_text",
            "manage_positions",
        ],
    },
}


def parse_opus_response(text: str) -> dict:
    """Parse the JSON blob returned by Opus (kept as dead code for rollback).

    Parameters
    ----------
    text:
        Raw string returned by the model.

    Returns
    -------
    dict
        Parsed recommendation dictionary.

    Raises
    ------
    ValueError
        If no valid JSON object can be extracted.
    """
    # Strip markdown code fences if present
    cleaned = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # Try to extract a JSON object directly
    brace_match = re.search(r"\{[\s\S]*\}", cleaned)
    if brace_match:
        cleaned = brace_match.group(0)

    return json.loads(cleaned)


def _get_current_price() -> float | None:
    """Return the most recent WTI close (Binance CLUSDT)."""
    try:
        with SessionLocal() as session:
            row = (
                session.query(OHLCV)
                .filter(OHLCV.timeframe == "1min", OHLCV.source == "yahoo")
                .order_by(OHLCV.timestamp.desc())
                .first()
            )
            if row is None:
                row = (
                    session.query(OHLCV)
                    .filter(OHLCV.timeframe == "1min")
                    .order_by(OHLCV.timestamp.desc())
                    .first()
                )
            return float(row.close) if row else None
    except Exception:
        logger.exception("Failed to read current price for Opus price guard")
        return None


def get_market_snapshot() -> dict:
    """Return current market snapshot: latest price + recent OHLCV stats.

    Uses Binance CLUSDT (NYMEX WTI front-month) — real front-month future that
    matches XTB OIL.WTI CFD virtually 1:1.
    """
    try:
        with SessionLocal() as session:
            latest = (
                session.query(OHLCV)
                .filter(OHLCV.timeframe == "1min", OHLCV.source == "yahoo")
                .order_by(OHLCV.timestamp.desc())
                .first()
            )
            if latest is None:
                return {}

            # Last 60 1-min bars for short-term range
            recent = (
                session.query(OHLCV)
                .filter(OHLCV.timeframe == "1min")
                .order_by(OHLCV.timestamp.desc())
                .limit(60)
                .all()
            )
            recent_highs = [r.high for r in recent]
            recent_lows = [r.low for r in recent]

            # Last 24 1H bars for medium-term context
            hourly = (
                session.query(OHLCV)
                .filter(OHLCV.timeframe == "1H")
                .order_by(OHLCV.timestamp.desc())
                .limit(24)
                .all()
            )
            hourly_highs = [r.high for r in hourly]
            hourly_lows = [r.low for r in hourly]

            return {
                "current_price": round(latest.close, 2),
                "current_price_source": latest.source,
                "current_timestamp": latest.timestamp.isoformat(),
                "last_60min_high": round(max(recent_highs), 2) if recent_highs else None,
                "last_60min_low": round(min(recent_lows), 2) if recent_lows else None,
                "last_24h_high": round(max(hourly_highs), 2) if hourly_highs else None,
                "last_24h_low": round(min(hourly_lows), 2) if hourly_lows else None,
            }
    except Exception:
        logger.exception("get_market_snapshot failed")
        return {}


def get_recent_knowledge_summaries(limit: int = 6) -> list[dict]:
    """Return the most recent KnowledgeSummary rows (newest first)."""
    try:
        with SessionLocal() as session:
            rows = (
                session.query(KnowledgeSummary)
                .order_by(KnowledgeSummary.timestamp.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                    "source": r.source,
                    "window": r.window,
                    "msgs": r.message_count,
                    "sentiment_label": r.sentiment_label,
                    "sentiment_score": r.sentiment_score,
                    "summary": r.summary,
                    "key_events": r.key_events,
                }
                for r in rows
            ]
    except Exception:
        logger.exception("get_recent_knowledge_summaries failed")
        return []


def _format_knowledge_for_prompt(summaries: list[dict], max_summaries: int = 3) -> str:
    """Render knowledge summaries compactly to avoid bloating the prompt."""
    if not summaries:
        return "No knowledge summaries yet."
    lines = []
    for s in summaries[:max_summaries]:
        ts = (s.get("timestamp") or "")[:19].replace("T", " ")
        label = s.get("sentiment_label", "neutral")
        score = s.get("sentiment_score") or 0
        summary_txt = (s.get("summary") or "")[:400]
        # Parse key_events back from JSON-encoded string
        key_events_raw = s.get("key_events")
        events: list = []
        if isinstance(key_events_raw, str):
            try:
                import json as _json
                events = _json.loads(key_events_raw)
            except Exception:
                events = []
        elif isinstance(key_events_raw, list):
            events = key_events_raw
        events_block = "\n".join(f"  - {e}" for e in events[:5])
        lines.append(
            f"[{ts} | {label} {score:+.2f}]\n{summary_txt}\n{events_block}"
        )
    return "\n\n".join(lines)


def get_recent_signals(limit: int = 5) -> list:
    """Return the most recent AIRecommendation rows from the database.

    Parameters
    ----------
    limit:
        Maximum number of rows to return.

    Returns
    -------
    list[dict]
        List of recommendation dicts (most recent first).
    """
    try:
        with SessionLocal() as session:
            rows = (
                session.query(AIRecommendation)
                .order_by(AIRecommendation.timestamp.desc())
                .limit(limit)
                .all()
            )
            result = []
            for row in rows:
                result.append(
                    {
                        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                        "action": row.action,
                        "unified_score": row.unified_score,
                        "opus_override_score": row.opus_override_score,
                        "confidence": row.confidence,
                    }
                )
            return result
    except Exception:
        logger.exception("get_recent_signals failed")
        return []


def synthesize_recommendation(
    scores: dict,
    haiku_summary: str,
    grok_narrative: str,
    open_positions: list[dict] | None = None,
    breaking_news: dict | None = None,
) -> dict:
    """Call Claude Opus to synthesise a trading recommendation.

    Parameters
    ----------
    scores:
        ScoresEvent dict with technical/fundamental/sentiment/unified scores.
    haiku_summary:
        Short outlook summary from the Haiku agent.
    grok_narrative:
        Twitter/X narrative from the Grok agent.

    Returns
    -------
    dict
        Recommendation dictionary ready to be published and stored.
    """
    # Task 5: Hard price grounding — refuse if no market data
    market = get_market_snapshot()
    if not market or market.get("current_price") is None:
        logger.warning("No current price available — refusing to call Opus (would hallucinate prices)")
        rec = dict(FALLBACK_REC)
        rec["analysis_text"] = "skipped — no market data"
        rec["timestamp"] = datetime.now(timezone.utc).isoformat()
        rec["haiku_summary"] = haiku_summary
        rec["grok_narrative"] = grok_narrative
        rec.setdefault("unified_score", scores.get("unified_score"))
        return rec

    client = Anthropic(api_key=settings.anthropic_api_key)

    recent = get_recent_signals()
    if recent:
        actions = [r.get("action", "?") for r in recent]
        confs = [r.get("confidence") or 0 for r in recent]
        avg_conf = sum(confs) / len(confs) if confs else 0
        action_counts = {a: actions.count(a) for a in set(actions)}
        summary = ", ".join(f"{a}×{n}" for a, n in action_counts.items())
        recent_text = f"Last {len(recent)} actions: {summary} | avg confidence {avg_conf:.2f}"
    else:
        recent_text = "No recent signals."

    # Task 2: Compact knowledge base rendering (max 3 summaries, truncated)
    knowledge = get_recent_knowledge_summaries()
    knowledge_text = _format_knowledge_for_prompt(knowledge, max_summaries=3)

    scores_text = json.dumps(scores, indent=2)
    market_text = json.dumps(market, indent=2)

    if open_positions:
        positions_text = json.dumps(open_positions, indent=2, default=str)
        positions_block = (
            f"## Currently Open Positions ({len(open_positions)})\n"
            f"{positions_text}\n\n"
            "For each open position, decide whether to HOLD or CLOSE based on the "
            "current market state and unrealised P/L. Return your decisions in "
            "manage_positions. If you choose to add a NEW position on top, set "
            "action=BUY/SELL with entry_price; otherwise set action=HOLD or WAIT.\n\n"
        )
    else:
        positions_block = (
            "## Currently Open Positions\nNone — feel free to open a new position if "
            "the setup is strong, otherwise WAIT.\n\n"
        )

    # Optional breaking-news urgent block — only present when this cycle was
    # triggered by a high-impact knowledge digest that contradicts an open campaign.
    breaking_block = ""
    if breaking_news:
        bn_summary = (breaking_news.get("summary") or "")[:500]
        bn_score = breaking_news.get("sentiment_score") or 0
        bn_label = breaking_news.get("sentiment_label") or "?"
        bn_events = breaking_news.get("key_events") or []
        bn_favors = breaking_news.get("favors_side") or "?"
        bn_conflicts = breaking_news.get("conflicting_campaign_ids") or []
        events_str = "\n".join(f"  - {e}" for e in bn_events[:6])
        breaking_block = (
            "## 🚨 URGENT — BREAKING NEWS CONTRADICTS OPEN POSITION\n"
            f"This cycle was triggered out-of-band by a high-impact @marketfeed digest.\n\n"
            f"**Sentiment**: {bn_label.upper()} ({bn_score:+.2f}) — favors **{bn_favors}**\n"
            f"**Summary**: {bn_summary}\n"
            f"**Key events**:\n{events_str}\n\n"
            f"**Conflicting campaign IDs**: {bn_conflicts}\n\n"
            "DECISION REQUIRED: For each conflicting campaign, decide via `manage_positions`:\n"
            "  - `close` — exit immediately if the news has materially invalidated the original thesis\n"
            "  - `hold` — keep the position if the news is noise or already priced in\n"
            "Be decisive. If you decide to close, explain the specific event in the reason field.\n\n"
        )

    user_prompt = (
        f"{breaking_block}"
        f"## Current WTI Crude Market Snapshot (USE THESE EXACT PRICES)\n{market_text}\n\n"
        f"{positions_block}"
        f"## Knowledge Base — Recent @marketfeed Digests (newest first)\n{knowledge_text}\n\n"
        f"## Quantitative Scores\n{scores_text}\n\n"
        f"## Haiku Analyst Summary\n{haiku_summary}\n\n"
        f"## Twitter/X Sentiment (Grok)\n{grok_narrative}\n\n"
        f"## Recent Recommendations (last {len(recent)})\n{recent_text}\n\n"
        "CRITICAL: entry_price, stop_loss, and take_profit MUST be derived from the "
        "current_price above (within ±5% for entry, realistic SL/TP for the timeframe). "
        "Do NOT invent prices from training data. If you cannot anchor to current_price, "
        "set them to null.\n\n"
        "PRIORITISE the @marketfeed knowledge base — these are the most recent breaking-news "
        "events that move the oil market. In your analysis_text, cite at least one specific "
        "key_event by name.\n\n"
        "Use the submit_trading_recommendation tool to return your structured recommendation."
    )

    timestamp = datetime.now(timezone.utc)

    try:
        # Task 6: Structured output via Anthropic tools (eliminates regex parsing)
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=[RECOMMENDATION_TOOL],
            tool_choice={"type": "tool", "name": "submit_trading_recommendation"},
            messages=[{"role": "user", "content": user_prompt}],
        )

        rec = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_trading_recommendation":
                rec = block.input
                break

        if rec is None:
            logger.error("Opus did not return a tool_use block")
            rec = dict(FALLBACK_REC)
    except Exception:
        logger.exception("Opus synthesize_recommendation failed")
        rec = dict(FALLBACK_REC)

    # Attach context fields
    rec["timestamp"] = timestamp.isoformat()
    rec["haiku_summary"] = haiku_summary
    rec["grok_narrative"] = grok_narrative

    # Ensure required fields have defaults
    rec.setdefault("unified_score", scores.get("unified_score"))
    rec.setdefault("manage_positions", [])

    # Persist to database
    try:
        _store_recommendation(rec, timestamp)
    except Exception:
        logger.exception("Failed to persist AIRecommendation to DB")

    return rec


def _store_recommendation(rec: dict, timestamp: datetime) -> None:
    """Persist an AIRecommendation record to the database.

    Parameters
    ----------
    rec:
        Recommendation dictionary as returned by Opus (or the fallback).
    timestamp:
        UTC datetime for the record.
    """
    risk_factors = rec.get("risk_factors")
    if isinstance(risk_factors, list):
        risk_factors = json.dumps(risk_factors)

    row = AIRecommendation(
        timestamp=timestamp,
        unified_score=rec.get("unified_score"),
        opus_override_score=rec.get("opus_override_score"),
        confidence=rec.get("confidence"),
        action=rec.get("action", "WAIT"),
        analysis_text=rec.get("analysis_text"),
        base_scenario=rec.get("base_scenario"),
        alt_scenario=rec.get("alt_scenario"),
        risk_factors=risk_factors,
        entry_price=rec.get("entry_price"),
        stop_loss=rec.get("stop_loss"),
        take_profit=rec.get("take_profit"),
        haiku_summary=rec.get("haiku_summary"),
        grok_narrative=rec.get("grok_narrative"),
    )

    with SessionLocal() as session:
        session.add(row)
        session.commit()

"""5-minute @marketfeed digest summariser.

Reads SentimentNews rows from the past 5 minutes that came from the
@marketfeed Telegram channel, asks Claude Haiku to write a concise digest of
what happened (with bullet-point key events + aggregate sentiment), and:
  - persists the result to the KnowledgeSummary table (so it becomes part of
    a queryable knowledge base for future analysis)
  - publishes it to the `knowledge.summary` Redis stream so the notifier can
    forward it to Telegram
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import anthropic
from sqlalchemy import select

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.knowledge import KnowledgeSummary
from shared.models.sentiment import SentimentNews
from shared.redis_streams import publish

logger = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_SOURCE_NAME = "telegram_marketfeed"
_WINDOW_MINUTES = 120  # 2 hours (matches scheduler interval)
_STREAM = "knowledge.summary"

_SUMMARY_SYSTEM = (
    "You are a senior oil-market intelligence analyst. You read raw breaking-news "
    "headlines from a Telegram channel and write tight, factual digests for a "
    "trader who needs to know what just happened in the oil market."
)

_SUMMARY_TEMPLATE = """Below are {count} oil-related headlines from @marketfeed in the last {window} minutes (most recent first):

{headlines}

Write a JSON object with EXACTLY these keys (no markdown fences, no extra text):
{{
  "summary": "2-3 sentence digest of what happened, bias-aware (mention bullish/bearish drivers)",
  "key_events": ["3-6 bullet strings, each one specific event with country/entity/number where present"],
  "sentiment_score": <float -1.0 to +1.0, aggregate market impact for Brent crude>,
  "sentiment_label": "bullish" | "bearish" | "neutral"
}}

If no headlines are oil-relevant, return summary="No material oil news in this window." with empty key_events and sentiment_score=0."""


_anthropic_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


def _strip_json(text: str) -> str:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            text = m.group(0)
    return text


def collect_and_store() -> None:
    """Pull last 5 min of @marketfeed messages, summarise, persist, publish."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=_WINDOW_MINUTES)

    with SessionLocal() as session:
        stmt = (
            select(SentimentNews)
            .where(SentimentNews.source == _SOURCE_NAME)
            .where(SentimentNews.timestamp >= cutoff)
            .order_by(SentimentNews.timestamp.desc())
            .limit(40)
        )
        rows = session.scalars(stmt).all()

    if not rows:
        logger.info("No new @marketfeed messages in last %d min — skipping summary", _WINDOW_MINUTES)
        return

    # Skip the Haiku call entirely when the window has <3 headlines. Most
    # such windows are noise — the LLM always comes back with "no material
    # news" and we waste ~$0.003 per call. Cheaper to synthesise a
    # placeholder digest row locally so the downstream consumers still
    # see the expected cadence.
    if len(rows) < 3:
        logger.info(
            "Only %d @marketfeed messages in last %d min — writing placeholder digest, skipping Haiku",
            len(rows), _WINDOW_MINUTES,
        )
        avg_score = sum(float(r.score or 0.0) for r in rows) / len(rows) if rows else 0.0
        placeholder_summary = (
            f"Only {len(rows)} headline{'s' if len(rows) != 1 else ''} this window — no material news flow."
        )
        now = datetime.now(tz=timezone.utc)
        with SessionLocal() as session:
            row = KnowledgeSummary(
                timestamp=now,
                source=_SOURCE_NAME,
                window=f"{_WINDOW_MINUTES}min",
                message_count=len(rows),
                summary=placeholder_summary,
                key_events=json.dumps([str(r.title)[:200] for r in rows])[:5000],
                sentiment_score=avg_score,
                sentiment_label=("bullish" if avg_score > 0.15 else "bearish" if avg_score < -0.15 else "neutral"),
            )
            session.add(row)
            session.commit()
        logger.info("Wrote placeholder digest (%d headlines)", len(rows))
        return

    headlines = "\n".join(
        f"- [{r.timestamp.strftime('%H:%M')} | score {r.score:+.2f} | rel {r.relevance:.2f}] {r.title}"
        for r in rows
    )
    prompt = _SUMMARY_TEMPLATE.format(
        count=len(rows), window=_WINDOW_MINUTES, headlines=headlines
    )

    import time as _time
    from shared.llm_usage import record_anthropic_call, record_failure
    call_start = _time.time()
    try:
        response = _get_client().messages.create(
            model=_HAIKU_MODEL,
            max_tokens=600,
            system=_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        record_anthropic_call(
            call_site="marketfeed.haiku_digest",
            model=_HAIKU_MODEL,
            usage=response.usage,
            duration_ms=(_time.time() - call_start) * 1000,
        )
        raw = response.content[0].text
        data = json.loads(_strip_json(raw))
    except Exception:
        logger.exception("Haiku marketfeed digest failed — raw=%r", locals().get("raw", ""))
        record_failure(
            call_site="marketfeed.haiku_digest",
            model=_HAIKU_MODEL,
            provider="anthropic",
            duration_ms=(_time.time() - call_start) * 1000,
        )
        return

    summary_text = str(data.get("summary", "")).strip()
    key_events = data.get("key_events") or []
    if not isinstance(key_events, list):
        key_events = [str(key_events)]
    sentiment_score = float(data.get("sentiment_score", 0.0))
    sentiment_label = str(data.get("sentiment_label", "neutral")).lower()

    now = datetime.now(tz=timezone.utc)

    with SessionLocal() as session:
        row = KnowledgeSummary(
            timestamp=now,
            source=_SOURCE_NAME,
            window=f"{_WINDOW_MINUTES}min",
            message_count=len(rows),
            summary=summary_text[:5000],
            key_events=json.dumps(key_events)[:5000],
            sentiment_score=sentiment_score,
            sentiment_label=sentiment_label[:16],
        )
        session.add(row)
        session.commit()

    logger.info(
        "@marketfeed digest stored: %d msgs, sentiment=%s (%+.2f)",
        len(rows), sentiment_label, sentiment_score,
    )

    payload = {
        "type": "marketfeed_digest",
        "timestamp": now.isoformat(),
        "source": _SOURCE_NAME,
        "window": f"{_WINDOW_MINUTES}min",
        "message_count": len(rows),
        "summary": summary_text,
        "key_events": key_events,
        "sentiment_score": sentiment_score,
        "sentiment_label": sentiment_label,
    }
    try:
        publish(_STREAM, payload)
        logger.info("Published @marketfeed digest to stream '%s'", _STREAM)
    except Exception:
        logger.exception("Failed to publish marketfeed digest")

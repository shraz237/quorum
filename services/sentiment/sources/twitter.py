"""Twitter/X sentiment collector via Grok."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from openai import OpenAI
from sqlalchemy import desc

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.ohlcv import OHLCV
from shared.models.sentiment import SentimentTwitter
from shared.redis_streams import publish
from shared.schemas.events import SentimentEvent

logger = logging.getLogger(__name__)

_STREAM = "sentiment.twitter"
_GROK_MODEL = "grok-3"
_XAI_BASE_URL = "https://api.x.ai/v1"

_GROK_PROMPT_TEMPLATE = """{price_anchor}Analyze the current sentiment on Twitter/X regarding crude oil prices and the Brent crude oil market.

Based on your real-time access to Twitter/X, provide a JSON response with exactly these keys:
- score: float between -1.0 (very bearish) and 1.0 (very bullish) representing aggregate sentiment
- narrative: string describing the dominant market narrative on Twitter (e.g. "supply cut optimism", "recession demand fears")
- topics: list of strings with key topics or hashtags trending (e.g. ["#OPEC", "#CrudeOil", "supply cuts"])

If you reference price levels in the narrative, use ONLY the FACT above — do not invent prices from your training data.

Respond only with the JSON object and no other text.

Example: {{"score": 0.3, "narrative": "OPEC supply cut optimism driving bullish sentiment", "topics": ["#OPEC", "#CrudeOil", "supply cuts", "#Brent"]}}"""


def _get_current_price() -> float | None:
    """Return the most recent WTI close (Binance CLUSDT)."""
    try:
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
    except Exception:
        logger.exception("Failed to read current price for Grok prompt")
        return None


def parse_grok_response(text: str) -> dict:
    """Parse the JSON response from Grok into a structured dict.

    Returns a dict with keys: score, narrative, topics.
    Falls back to neutral defaults on parse error.
    """
    try:
        # Strip markdown code fences if present
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # Remove first and last fence lines
            stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        data = json.loads(stripped)
        topics = data.get("topics", [])
        if isinstance(topics, list):
            topics_str = ", ".join(str(t) for t in topics)
        else:
            topics_str = str(topics)

        return {
            "score": float(data.get("score", 0.0)),
            "narrative": str(data.get("narrative", "unknown")),
            "topics": topics_str,
        }
    except Exception:
        logger.exception("Failed to parse Grok response: %r", text)
        return {"score": 0.0, "narrative": "parse error", "topics": ""}


def fetch_twitter_sentiment() -> dict:
    """Call Grok to get aggregated Twitter/X sentiment for crude oil."""
    client = OpenAI(api_key=settings.xai_api_key, base_url=_XAI_BASE_URL)

    current_price = _get_current_price()
    price_anchor = (
        f"FACT — current Brent (ICE) price is ${current_price:.2f}. "
        f"Do NOT cite any other price level. Do not invent prices from your training data.\n\n"
        if current_price is not None
        else ""
    )
    prompt = _GROK_PROMPT_TEMPLATE.format(price_anchor=price_anchor)

    try:
        response = client.chat.completions.create(
            model=_GROK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
        )
        raw = response.choices[0].message.content or ""
        logger.debug("Grok raw response: %r", raw)
        return parse_grok_response(raw)
    except Exception:
        logger.exception("Grok API call failed")
        return {"score": 0.0, "narrative": "api error", "topics": ""}


def collect_and_store() -> None:
    """Fetch Twitter sentiment from Grok, persist to DB, and publish event."""
    sentiment = fetch_twitter_sentiment()

    score = sentiment["score"]
    narrative = sentiment["narrative"]
    topics = sentiment["topics"]
    now = datetime.now(tz=timezone.utc)

    with SessionLocal() as session:
        row = SentimentTwitter(
            timestamp=now,
            narrative=narrative,
            score=score,
            key_topics=topics or None,
        )
        session.add(row)
        session.commit()

    logger.info("Stored SentimentTwitter (score=%.3f, narrative=%r)", score, narrative)

    # Derive sentiment label
    if score >= 0.1:
        sentiment_label = "bullish"
    elif score <= -0.1:
        sentiment_label = "bearish"
    else:
        sentiment_label = "neutral"

    event = SentimentEvent(
        timestamp=now,
        source_type="twitter",
        sentiment=sentiment_label,
        score=round(score, 4),
        relevance=1.0,
        summary=narrative,
    )
    publish(_STREAM, event.model_dump())
    logger.info("Published SentimentEvent to stream '%s' (score=%.3f)", _STREAM, score)

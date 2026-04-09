"""Grok agent — retrieves Twitter/X crude oil sentiment narrative."""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta, timezone

from openai import OpenAI
from sqlalchemy import desc

from shared.config import settings
from shared.llm_usage import record_failure, record_openai_compatible_call
from shared.models.base import SessionLocal
from shared.models.ohlcv import OHLCV
from shared.models.sentiment import SentimentTwitter

logger = logging.getLogger(__name__)

MODEL = "grok-3"
FALLBACK = "Unable to retrieve Grok narrative at this time."


def _get_current_price() -> float | None:
    """Return the most recent WTI close (Binance CLUSDT)."""
    try:
        with SessionLocal() as session:
            row = (
                session.query(OHLCV)
                .filter(OHLCV.timeframe == "1min", OHLCV.source == "twelve")
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


def _live_grok_call() -> str:
    """Call the Grok API directly to fetch Twitter/X crude oil narrative."""
    current_price = _get_current_price()
    if current_price is None:
        logger.warning("No current price available — refusing to call LLM (would hallucinate prices)")
        return "Price unavailable — analysis skipped."

    client = OpenAI(
        api_key=settings.xai_api_key,
        base_url="https://api.x.ai/v1",
    )

    price_anchor = (
        f"FACT — current WTI (NYMEX) price is ${current_price:.2f}. "
        f"Do NOT cite any other price level. Do not invent prices from your training data.\n\n"
    )

    prompt = (
        f"{price_anchor}"
        "You have real-time access to Twitter/X. "
        "Describe the current Twitter/X narrative and sentiment around WTI crude oil. "
        "What are traders, analysts, and news accounts saying right now about supply, "
        "demand, geopolitics, and OPEC? "
        "Summarise the dominant themes in 2-3 sentences. Be concise and factual. "
        "If you reference price levels, use only the FACT above."
    )

    call_start = _time.time()
    try:
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        record_openai_compatible_call(
            call_site="twitter_narrative.grok",
            model=MODEL,
            usage=response.usage,
            duration_ms=(_time.time() - call_start) * 1000,
            provider="xai",
        )
        return response.choices[0].message.content.strip()
    except Exception:
        logger.exception("Grok live API call failed")
        record_failure(
            call_site="twitter_narrative.grok",
            model=MODEL,
            provider="xai",
            duration_ms=(_time.time() - call_start) * 1000,
        )
        return FALLBACK


def get_twitter_narrative() -> str:
    """Read latest Grok narrative from sentiment service's DB rows.

    Falls back to live API call only if no fresh row exists (older than 30 min).
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
    try:
        with SessionLocal() as session:
            row = (
                session.query(SentimentTwitter)
                .filter(SentimentTwitter.timestamp >= cutoff)
                .order_by(desc(SentimentTwitter.timestamp))
                .first()
            )
            if row:
                logger.info("Using cached Twitter narrative from %s", row.timestamp)
                return row.narrative
    except Exception:
        logger.exception("Failed to read SentimentTwitter from DB, falling back to live Grok call")

    # Fallback: live Grok call
    return _live_grok_call()

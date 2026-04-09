"""Haiku agent — summarises analysis scores in plain language."""

from __future__ import annotations

import logging

import anthropic
from sqlalchemy import desc

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
FALLBACK = "Unable to generate Haiku summary at this time."


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
        logger.exception("Failed to read current price for Haiku prompt")
        return None


def summarize_scores(scores: dict) -> str:
    """Call claude-haiku to produce a 3-4 sentence outlook summary.

    Parameters
    ----------
    scores:
        Dict containing keys such as technical_score, fundamental_score,
        sentiment_score, shipping_score, unified_score (values may be None).

    Returns
    -------
    str
        A short narrative summary, or a fallback string on error.
    """
    current_price = _get_current_price()
    if current_price is None:
        logger.warning("No current price available — refusing to call LLM (would hallucinate prices)")
        return "Price unavailable — analysis skipped."

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    scores_text = "\n".join(
        f"  {k}: {v}" for k, v in scores.items()
    )
    price_anchor = (
        f"FACT — current WTI (NYMEX) price is ${current_price:.2f}. "
        f"Use ONLY this price level if you reference any number — do not invent prices.\n\n"
    )
    prompt = (
        f"{price_anchor}"
        "You are a WTI crude oil market analyst assistant. "
        "Below are composite analysis scores on a -100..+100 scale "
        "(scale: -100 = extreme bearish, 0 = neutral, +100 = extreme bullish). "
        "Interpret values correctly: e.g. 38.5 means moderately bullish (38/100), "
        "NOT near-neutral.\n\n"
        f"{scores_text}\n\n"
        "Write a concise 3-4 sentence summary covering the technical, fundamental, "
        "and sentiment outlook for WTI crude oil based on these scores. "
        "Be direct and factual."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        logger.exception("Haiku summarize_scores failed")
        return FALLBACK

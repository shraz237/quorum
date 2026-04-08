"""RSS news collector with Claude Haiku sentiment classification (batched)."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

import anthropic
import feedparser
import requests

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.sentiment import SentimentNews
from shared.redis_streams import publish
from shared.schemas.events import SentimentEvent

logger = logging.getLogger(__name__)

FEEDS: list[dict[str, str]] = [
    {
        "name": "oilprice_main",
        "url": "https://oilprice.com/rss/main",
    },
    {
        "name": "oilprice_geopolitics",
        "url": "https://oilprice.com/rss/geopolitics",
    },
    {
        "name": "oilprice_breaking",
        "url": "https://oilprice.com/rss/breaking",
    },
    {
        "name": "rigzone",
        "url": "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
    },
]

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BrentBot/1.0)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

_STREAM = "sentiment.news"
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

_CLASSIFY_SYSTEM = (
    "You are a financial news classifier specialised in crude oil markets. "
    "Respond only with a JSON object and no other text."
)

_CLASSIFY_TEMPLATE = """Classify the following news headline for Brent crude oil market sentiment.

Title: {title}
Source: {source}

Return a JSON object with exactly these keys:
- sentiment: one of "bullish", "bearish", or "neutral"
- score: float between -1.0 (very bearish) and 1.0 (very bullish)
- relevance: float between 0.0 and 1.0 indicating how relevant this headline is to Brent crude oil prices

Example: {{"sentiment": "bullish", "score": 0.6, "relevance": 0.9}}"""

_BATCH_CLASSIFY_TEMPLATE = """You are an oil-market analyst. Classify each headline below for its impact on Brent crude oil price.

Headlines (numbered):
{numbered_headlines}

Return a JSON array with EXACTLY {n} objects, one per headline, in the same order:
[
  {{"i": 0, "sentiment": "bullish"|"bearish"|"neutral", "score": <float -1.0..1.0>, "relevance": <float 0.0..1.0>}},
  ...
]

- score: -1.0 = extremely bearish for Brent, +1.0 = extremely bullish
- relevance: 0.0 = unrelated to oil, 1.0 = directly moves oil market
- Be strict with relevance; only score 0.5+ if the headline directly affects oil supply/demand/risk premium.

Respond with ONLY the JSON array (no markdown fences, no extra text)."""


def _parse_json_array(raw: str) -> list | None:
    """Robust JSON array parser: strips markdown fences, handles trailing commas."""
    text = raw.strip()

    # Strip ```json ... ``` or ``` ... ``` fences
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()

    # If it doesn't start with '[', try to find the array by brace matching
    if not text.startswith("["):
        bracket = re.search(r"\[[\s\S]*\]", text)
        if bracket:
            text = bracket.group(0)
        else:
            return None

    # Remove trailing commas before ] or } (common LLM mistake)
    text = re.sub(r",\s*([\]\}])", r"\1", text)

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    return None


def classify_article(title: str, source: str) -> dict:
    """Call Claude Haiku to classify a single news article headline (fallback).

    Returns a dict with keys: sentiment, score, relevance.
    Falls back to neutral/0/0 on any error.
    """
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = _CLASSIFY_TEMPLATE.format(title=title, source=source)

    try:
        message = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=128,
            system=_CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        # Strip optional ```json ... ``` markdown fences
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if fence:
            raw = fence.group(1).strip()

        # Fall back to grabbing the first {...} object if there is surrounding text
        if not raw.startswith("{"):
            brace = re.search(r"\{[\s\S]*\}", raw)
            if brace:
                raw = brace.group(0)

        data = json.loads(raw)
        return {
            "sentiment": str(data.get("sentiment", "neutral")),
            "score": float(data.get("score", 0.0)),
            "relevance": float(data.get("relevance", 0.0)),
        }
    except Exception:
        logger.exception(
            "Haiku classification failed for title=%r — raw response: %r",
            title,
            locals().get("raw", ""),
        )
        return {"sentiment": "neutral", "score": 0.0, "relevance": 0.0}


def classify_articles_batch(articles: list[dict]) -> list[dict]:
    """Classify all articles in a single Haiku call (batch mode).

    Takes a list of dicts with keys ``title`` and ``source``.
    Returns the same list with ``sentiment``, ``score``, and ``relevance`` populated.

    Falls back to per-article classify_article() if the batch call fails or
    the response array length mismatches.
    """
    if not articles:
        return articles

    numbered_headlines = "\n".join(
        f"{i}. [{a['source']}] {a['title']}" for i, a in enumerate(articles)
    )
    prompt = _BATCH_CLASSIFY_TEMPLATE.format(
        numbered_headlines=numbered_headlines,
        n=len(articles),
    )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    raw = ""
    try:
        message = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        parsed = _parse_json_array(raw)

        if parsed is None or len(parsed) != len(articles):
            logger.warning(
                "Batch classify array length mismatch (got %s, expected %d) — falling back",
                len(parsed) if parsed is not None else "None",
                len(articles),
            )
            raise ValueError("length mismatch")

        for i, article in enumerate(articles):
            # Find the item by index field if present, else use position
            item = next((x for x in parsed if x.get("i") == i), parsed[i] if i < len(parsed) else {})
            article["sentiment"] = str(item.get("sentiment", "neutral")).lower()
            article["score"] = float(item.get("score", 0.0))
            article["relevance"] = float(item.get("relevance", 0.0))

        logger.info("Batch-classified %d RSS articles in 1 Haiku call", len(articles))
        return articles

    except Exception:
        logger.warning(
            "Batch RSS classification failed (raw=%r) — falling back to per-article",
            raw[:200] if raw else "",
        )
        # Fallback: classify one by one
        for article in articles:
            result = classify_article(article["title"], article["source"])
            article["sentiment"] = result["sentiment"]
            article["score"] = result["score"]
            article["relevance"] = result["relevance"]
        return articles


def fetch_and_classify() -> list[dict]:
    """Parse all RSS feeds and classify all entries with a single batched Haiku call.

    Returns a list of dicts with keys:
        title, url, source, sentiment, score, relevance, published_at
    """
    # Collect all entries first (no Haiku calls yet)
    entries: list[dict] = []

    for feed_cfg in FEEDS:
        feed_name = feed_cfg["name"]
        try:
            response = requests.get(feed_cfg["url"], headers=_HTTP_HEADERS, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except Exception as exc:
            logger.warning("Failed to fetch RSS feed %s: %s", feed_name, exc)
            continue

        if feed.bozo and not feed.entries:
            logger.warning("Feed %s parse error: %s", feed_name, feed.bozo_exception)
            continue

        for entry in feed.entries:
            title = entry.get("title", "").strip()
            url = entry.get("link", "")

            if not title:
                continue

            # Parse published date; fall back to now
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published_at = datetime.fromtimestamp(
                    time.mktime(entry.published_parsed), tz=timezone.utc
                )
            else:
                published_at = datetime.now(tz=timezone.utc)

            entries.append(
                {
                    "title": title,
                    "url": url,
                    "source": feed_name,
                    "published_at": published_at,
                    # placeholders — filled by classify_articles_batch
                    "sentiment": "neutral",
                    "score": 0.0,
                    "relevance": 0.0,
                }
            )

    if not entries:
        logger.info("No RSS entries fetched")
        return []

    # Single batched Haiku call for all articles
    results = classify_articles_batch(entries)
    logger.info("Fetched and classified %d articles from RSS feeds", len(results))
    return results


def collect_and_store() -> None:
    """Fetch, classify, filter, persist, and publish RSS sentiment."""
    articles = fetch_and_classify()
    if not articles:
        logger.info("No RSS articles to store")
        return

    # Filter by relevance threshold
    relevant = [a for a in articles if a["relevance"] >= 0.3]
    logger.info("%d/%d articles pass relevance>=0.3 filter", len(relevant), len(articles))

    if not relevant:
        return

    with SessionLocal() as session:
        for art in relevant:
            row = SentimentNews(
                timestamp=art["published_at"],
                source=art["source"],
                title=art["title"],
                url=art["url"],
                sentiment=art["sentiment"],
                score=art["score"],
                relevance=art["relevance"],
            )
            session.add(row)
        session.commit()

    logger.info("Stored %d SentimentNews rows", len(relevant))

    # Compute weighted-average score (weight = relevance)
    total_weight = sum(a["relevance"] for a in relevant)
    weighted_score = sum(a["score"] * a["relevance"] for a in relevant) / total_weight
    avg_relevance = sum(a["relevance"] for a in relevant) / len(relevant)

    # Derive aggregate sentiment label from weighted score
    if weighted_score >= 0.1:
        agg_sentiment = "bullish"
    elif weighted_score <= -0.1:
        agg_sentiment = "bearish"
    else:
        agg_sentiment = "neutral"

    event = SentimentEvent(
        timestamp=datetime.now(tz=timezone.utc),
        source_type="news",
        sentiment=agg_sentiment,
        score=round(weighted_score, 4),
        relevance=round(avg_relevance, 4),
        summary=f"Aggregated from {len(relevant)} articles",
    )
    publish(_STREAM, event.model_dump())
    logger.info("Published SentimentEvent to stream '%s' (score=%.3f)", _STREAM, weighted_score)

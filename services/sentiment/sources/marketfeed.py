"""Telegram @marketfeed channel scraper.

Scrapes the public web preview at https://t.me/s/marketfeed (no auth needed),
filters messages for crude-oil relevance, and uses a single Claude Haiku call
to BOTH classify each relevant message AND produce a rolling 5-minute digest.

@marketfeed posts breaking financial / geopolitical news that moves oil:
OPEC decisions, Iran/Israel/Hormuz events, US inventory surprises, sanctions,
ceasefires, drone strikes on energy infrastructure, etc.

Wave 2 optimisation: 1 Haiku call per scrape cycle instead of N+1.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import requests
import anthropic
from bs4 import BeautifulSoup
from sqlalchemy import select

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.knowledge import KnowledgeSummary
from shared.models.sentiment import SentimentNews
from shared.redis_streams import publish
from shared.schemas.events import SentimentEvent

logger = logging.getLogger(__name__)

_CHANNEL_URL = "https://t.me/s/marketfeed"
_SOURCE_NAME = "telegram_marketfeed"
_STREAM = "sentiment.news"
_KNOWLEDGE_STREAM = "knowledge.summary"

# Keywords that gate which messages we send to Haiku for scoring. Pre-filtering
# saves Anthropic tokens — most @marketfeed posts are not oil-related.
_OIL_KEYWORDS = re.compile(
    r"\b(oil|crude|brent|wti|opec|opec\+|petroleum|barrel|refinery|"
    r"pipeline|tanker|hormuz|strait|saudi|aramco|iran|iraq|libya|venezuela|"
    r"russia|kuwait|uae|emirates|gas\s*field|drone\s+strike|sanction|"
    r"embargo|export|import|inventory|stockpile|spr|cushing|"
    r"price\s*cap|production\s*cut|refinery\s*outage|nord\s*stream|"
    r"red\s*sea|suez|houthi|natural\s*gas|lng)\b",
    re.IGNORECASE,
)

# Per-message fallback prompt (used only when batch call fails)
_CLASSIFY_PROMPT = """You are an oil-market analyst. Classify the following breaking-news headline for its impact on Brent crude oil prices.

HEADLINE:
{title}

Respond with ONLY a JSON object (no markdown, no extra text) with these exact keys:
  "sentiment": "bullish" | "bearish" | "neutral"
  "score":     float in [-1.0, +1.0]   (negative = bearish for oil price)
  "relevance": float in [0.0, 1.0]     (1.0 = directly moves the oil market)
  "reason":    string                  (one short sentence explaining)

Be strict with relevance: only score 0.7+ if the news directly affects oil supply/demand/risk premium."""

# Combined classify + digest prompt (1 Haiku call does both)
_COMBINED_PROMPT = """You are an oil-market analyst. Analyze these {n} breaking-news headlines from Telegram @marketfeed.

Headlines (numbered, last 5 minutes, newest first):
{numbered_headlines}

Return a JSON object with EXACTLY these keys (no markdown, no extra text):
{{
  "items": [
    {{"i": 0, "sentiment": "bullish"|"bearish"|"neutral", "score": <-1..1>, "relevance": <0..1>, "reason": "<short>"}},
    ... (one per headline, in order)
  ],
  "digest": {{
    "summary": "2-3 sentence digest of what just happened in the oil market",
    "key_events": ["3-6 bullet strings, each one specific event with country/entity/number where present"],
    "sentiment_score": <float -1..1, aggregate market impact>,
    "sentiment_label": "bullish"|"bearish"|"neutral"
  }}
}}

If no headlines are oil-relevant, set digest.summary="No material oil news in this window." with empty key_events and sentiment_score=0."""


_anthropic_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _anthropic_client


def _strip_json_object(text: str) -> str:
    """Strip markdown fences; extract first {...} block if needed."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            text = m.group(0)
    # Remove trailing commas before } or ]
    text = re.sub(r",\s*([\]\}])", r"\1", text)
    return text


def fetch_marketfeed_messages() -> list[dict[str, Any]]:
    """Scrape @marketfeed public preview and return parsed messages.

    Each dict has: ``url`` (canonical t.me URL), ``timestamp`` (UTC), ``title``.
    """
    logger.info("Fetching @marketfeed channel preview")
    response = requests.get(
        _CHANNEL_URL,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BrentBot/1.0)"},
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    messages: list[dict[str, Any]] = []

    for wrapper in soup.select(".tgme_widget_message_wrap"):
        msg = wrapper.select_one(".tgme_widget_message")
        if msg is None:
            continue

        post_id = msg.get("data-post")
        if not post_id:
            continue
        url = f"https://t.me/{post_id}"

        text_node = msg.select_one(".tgme_widget_message_text")
        if text_node is None:
            continue
        title = text_node.get_text(separator=" ", strip=True)
        if not title:
            continue

        time_node = msg.select_one("time.time")
        ts: datetime
        if time_node and time_node.get("datetime"):
            try:
                ts = datetime.fromisoformat(time_node["datetime"])
            except ValueError:
                ts = datetime.now(tz=timezone.utc)
        else:
            ts = datetime.now(tz=timezone.utc)

        messages.append({"url": url, "timestamp": ts, "title": title})

    logger.info("Parsed %d messages from @marketfeed", len(messages))
    return messages


def _is_oil_relevant(title: str) -> bool:
    return bool(_OIL_KEYWORDS.search(title))


def classify_message(title: str) -> dict[str, Any] | None:
    """Use Claude Haiku to classify a single headline (fallback only)."""
    try:
        response = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(title=title)}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        # Remove trailing commas
        text = re.sub(r",\s*([\]\}])", r"\1", text)
        data = json.loads(text)
        return {
            "sentiment": str(data.get("sentiment", "neutral")).lower(),
            "score": float(data.get("score", 0.0)),
            "relevance": float(data.get("relevance", 0.0)),
            "reason": str(data.get("reason", "")),
        }
    except Exception:
        logger.exception("Haiku classification failed for: %s", title[:80])
        return None


def classify_messages_batch(messages: list[dict]) -> tuple[list[dict[str, Any] | None], dict | None]:
    """Classify all messages and produce a digest in a SINGLE Haiku call.

    Args:
        messages: list of dicts with at least a ``title`` key.

    Returns:
        (classifications, digest) where:
          - classifications: list of per-message dicts (sentiment/score/relevance/reason),
            same length as messages, None entries mean classification failed.
          - digest: dict with summary/key_events/sentiment_score/sentiment_label,
            or None if the batch call failed entirely.

    On failure, falls back to per-message classify_message() calls (digest=None).
    """
    if not messages:
        return [], None

    numbered_headlines = "\n".join(
        f"{i}. {m['title']}" for i, m in enumerate(messages)
    )
    prompt = _COMBINED_PROMPT.format(
        n=len(messages),
        numbered_headlines=numbered_headlines,
    )

    raw = ""
    try:
        response = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        cleaned = _strip_json_object(raw)
        data = json.loads(cleaned)

        items_raw = data.get("items", [])
        digest_raw = data.get("digest", {})

        if not isinstance(items_raw, list) or len(items_raw) != len(messages):
            logger.warning(
                "Combined batch items length mismatch (got %s, expected %d) — falling back",
                len(items_raw) if isinstance(items_raw, list) else "None",
                len(messages),
            )
            raise ValueError("items length mismatch")

        classifications: list[dict[str, Any] | None] = []
        for i, msg in enumerate(messages):
            # Prefer item with matching "i" field, else fall back to position
            item = next((x for x in items_raw if x.get("i") == i), items_raw[i] if i < len(items_raw) else {})
            classifications.append({
                "sentiment": str(item.get("sentiment", "neutral")).lower(),
                "score": float(item.get("score", 0.0)),
                "relevance": float(item.get("relevance", 0.0)),
                "reason": str(item.get("reason", "")),
            })

        digest: dict | None = None
        if isinstance(digest_raw, dict):
            key_events = digest_raw.get("key_events") or []
            if not isinstance(key_events, list):
                key_events = [str(key_events)]
            digest = {
                "summary": str(digest_raw.get("summary", "")).strip(),
                "key_events": key_events,
                "sentiment_score": float(digest_raw.get("sentiment_score", 0.0)),
                "sentiment_label": str(digest_raw.get("sentiment_label", "neutral")).lower(),
            }

        logger.info(
            "Combined batch: classified %d messages + produced digest in 1 Haiku call",
            len(messages),
        )
        return classifications, digest

    except Exception:
        logger.warning(
            "Combined batch classify failed (raw=%r) — falling back to per-message",
            raw[:300] if raw else "",
        )
        # Fallback: classify one by one, no digest
        classifications = [classify_message(m["title"]) for m in messages]
        return classifications, None


def _store_digest(digest: dict, message_count: int) -> None:
    """Persist a KnowledgeSummary row and publish to knowledge.summary stream."""
    now = datetime.now(tz=timezone.utc)
    try:
        with SessionLocal() as session:
            row = KnowledgeSummary(
                timestamp=now,
                source=_SOURCE_NAME,
                window="5min",
                message_count=message_count,
                summary=digest["summary"][:5000],
                key_events=json.dumps(digest["key_events"])[:5000],
                sentiment_score=digest["sentiment_score"],
                sentiment_label=digest["sentiment_label"][:16],
            )
            session.add(row)
            session.commit()
        logger.info(
            "@marketfeed digest stored: %d msgs, sentiment=%s (%+.2f)",
            message_count, digest["sentiment_label"], digest["sentiment_score"],
        )
    except Exception:
        logger.exception("Failed to store @marketfeed KnowledgeSummary")

    payload = {
        "type": "marketfeed_digest",
        "timestamp": now.isoformat(),
        "source": _SOURCE_NAME,
        "window": "5min",
        "message_count": message_count,
        "summary": digest["summary"],
        "key_events": digest["key_events"],
        "sentiment_score": digest["sentiment_score"],
        "sentiment_label": digest["sentiment_label"],
    }
    try:
        publish(_KNOWLEDGE_STREAM, payload)
        logger.info("Published @marketfeed digest to stream '%s'", _KNOWLEDGE_STREAM)
    except Exception:
        logger.exception("Failed to publish marketfeed digest")


def collect_and_store() -> None:
    """Scrape @marketfeed, score new oil-relevant messages, persist to DB.

    Wave 2: uses a single combined Haiku call to classify all messages AND
    produce a 5-minute digest simultaneously (instead of N+1 calls).
    """
    try:
        messages = fetch_marketfeed_messages()
    except Exception:
        logger.exception("Failed to fetch @marketfeed")
        return

    if not messages:
        logger.warning("@marketfeed returned no messages")
        return

    # Deduplicate against URLs we've already stored
    urls = [m["url"] for m in messages]
    with SessionLocal() as session:
        existing = set(
            session.scalars(
                select(SentimentNews.url).where(SentimentNews.url.in_(urls))
            ).all()
        )

    new_messages = [m for m in messages if m["url"] not in existing]
    logger.info("@marketfeed: %d new messages (skipped %d duplicates)",
                len(new_messages), len(messages) - len(new_messages))

    if not new_messages:
        return

    # Pre-filter by keyword to save Haiku tokens (keep this optimisation)
    relevant = [m for m in new_messages if _is_oil_relevant(m["title"])]
    logger.info("@marketfeed: %d/%d new messages match oil keywords",
                len(relevant), len(new_messages))

    if not relevant:
        return

    # ONE combined Haiku call: classify all + produce digest
    classifications, digest = classify_messages_batch(relevant)

    stored: list[dict[str, Any]] = []
    skipped = 0
    with SessionLocal() as session:
        for msg, cls in zip(relevant, classifications):
            if cls is None or cls["relevance"] < 0.3:
                skipped += 1
                continue

            row = SentimentNews(
                timestamp=msg["timestamp"],
                source=_SOURCE_NAME,
                title=msg["title"][:1000],
                url=msg["url"],
                sentiment=cls["sentiment"][:16],
                score=cls["score"],
                relevance=cls["relevance"],
            )
            session.add(row)
            stored.append({
                "title": msg["title"][:200],
                "url": msg["url"],
                "score": cls["score"],
                "relevance": cls["relevance"],
                "reason": cls.get("reason", ""),
            })
        session.commit()

    logger.info("@marketfeed: stored %d, skipped %d (low-relevance / errors)",
                len(stored), skipped)

    if not stored:
        return

    # Publish a SentimentEvent summarising this batch
    total_weight = sum(s["relevance"] for s in stored)
    avg_score = sum(s["score"] * s["relevance"] for s in stored) / total_weight
    event = SentimentEvent(
        timestamp=datetime.now(tz=timezone.utc),
        source_type="news",
        sentiment="bullish" if avg_score > 0.1 else "bearish" if avg_score < -0.1 else "neutral",
        score=avg_score,
        relevance=1.0,
        summary=f"@marketfeed: {len(stored)} oil-relevant messages, avg score {avg_score:+.2f}",
    )
    publish(_STREAM, event.model_dump())
    logger.info("Published @marketfeed SentimentEvent (avg score %+.2f)", avg_score)

    # Also store + publish the digest (produced in the same Haiku call above)
    if digest is not None:
        _store_digest(digest, len(stored))
    else:
        logger.info("No digest produced (batch fallback was used); marketfeed_summary.py can handle it")

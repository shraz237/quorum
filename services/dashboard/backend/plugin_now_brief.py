"""Now Brief — cached AI synthesis of the entire dashboard state.

Reads every data surface the dashboard shows — account, campaigns,
scores, conviction, Binance metrics, orderbook, whales, volume profile,
recent news — and asks Claude Haiku to write a concise structured
brief: the one thing a trader actually wants to see when they glance
at the screen.

Output is cached for CACHE_TTL_SECONDS so every dashboard refresh
doesn't re-hit the LLM. At 45s cadence the cost is ~$0.10-0.30/day
using Haiku.

Returned shape:
{
  "headline": "one sentence hook",
  "market_state": "2-3 sentences",
  "your_position": "2 sentences",
  "next_action": "1-2 sentence concrete recommendation",
  "watch_for": "1-2 sentences, specific triggers",
  "risk_level": 1..10,
  "risk_reason": "short reason",
  "generated_at": "2026-04-08T23:59:12Z",
  "cache_age_seconds": 12.3
}
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic

from shared.config import settings
from shared.llm_usage import record_anthropic_call, record_failure

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
# 3-minute cache — the market rarely changes meaningfully faster than that
# for a mobile "what's the situation" summary. Previously 45s which was
# aggressively expensive given the 30s frontend poll cadence.
CACHE_TTL_SECONDS = 180

_client: Anthropic | None = None
_cache: dict | None = None
_cache_ts: float = 0.0
_lock = threading.Lock()


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


SYSTEM_PROMPT = """You are a senior oil trading analyst writing a real-time synthesis brief for a WTI crude CFD trader on XTB.

The trader stares at a dashboard with 20+ panels and can't mentally integrate everything. Your job: read the full state below and produce ONE structured JSON output that captures what MATTERS right now.

Rules:
- Be BLUNT and CONCRETE. Cite specific numbers. No fluff, no hedging.
- Prioritise the trader's OPEN POSITION. If they're in trouble, say so. If they're doing fine, say so.
- Risk level 1-10 scale: 1 = totally safe, 10 = imminent margin call / blowup.
- Next action must be CONCRETE and ACTIONABLE, with an explicit trigger. Not "monitor closely".
- "Watch for" must be specific PRICE LEVELS or EVENTS that would change the call.
- Never invent data. Only use what's in the context.
- If data is contradictory, say it explicitly (e.g. "funding extreme crowded short but OI rising — setup for squeeze").

Return ONLY a JSON object (no markdown fences, no preamble):
{
  "headline": "one sentence that captures the dominant story RIGHT NOW (max 15 words)",
  "market_state": "2-3 sentences on price/positioning/momentum with specific numbers",
  "your_position": "2 sentences on the user's open position: PnL, margin level, distance to hard stop",
  "next_action": "concrete recommendation: HOLD/REDUCE/CLOSE/ADD + WHY + WHEN (specific trigger)",
  "watch_for": "1-2 sentences listing specific price levels or events that would change the call",
  "risk_level": <integer 1 to 10>,
  "risk_reason": "short reason for the risk level (max 12 words)"
}"""


def _gather_dashboard_state() -> dict:
    """Collect a compact snapshot of every data surface into one dict."""
    state: dict = {}

    # Account
    try:
        from shared.account_manager import recompute_account_state
        state["account"] = recompute_account_state()
    except Exception as exc:
        state["account"] = {"error": str(exc)}

    # Open campaigns
    try:
        from shared.position_manager import list_open_campaigns
        state["campaigns"] = list_open_campaigns()
    except Exception as exc:
        state["campaigns"] = {"error": str(exc)}

    # Latest scores
    try:
        from shared.models.base import SessionLocal
        from shared.models.signals import AnalysisScore
        from sqlalchemy import desc
        with SessionLocal() as session:
            row = (
                session.query(AnalysisScore)
                .order_by(desc(AnalysisScore.timestamp))
                .first()
            )
            if row:
                state["scores"] = {
                    "technical": row.technical_score,
                    "fundamental": row.fundamental_score,
                    "sentiment": row.sentiment_score,
                    "shipping": row.shipping_score,
                    "unified": row.unified_score,
                    "timestamp": row.timestamp.isoformat(),
                }
    except Exception as exc:
        state["scores"] = {"error": str(exc)}

    # Conviction
    try:
        from plugin_conviction import compute_conviction
        state["conviction"] = compute_conviction()
    except Exception as exc:
        state["conviction"] = {"error": str(exc)}

    # Binance metrics — latest snapshots only, NOT full series
    try:
        from shared.models.base import SessionLocal
        from shared.models.binance_metrics import (
            BinanceFundingRate,
            BinanceOpenInterest,
            BinanceLongShortRatio,
        )
        from sqlalchemy import desc

        with SessionLocal() as session:
            fr = (
                session.query(BinanceFundingRate)
                .order_by(desc(BinanceFundingRate.funding_time))
                .first()
            )
            oi_latest = (
                session.query(BinanceOpenInterest)
                .order_by(desc(BinanceOpenInterest.timestamp))
                .first()
            )
            # OI 24h ago for change computation
            oi_24h_ago = (
                session.query(BinanceOpenInterest)
                .filter(
                    BinanceOpenInterest.timestamp
                    <= datetime.now(tz=timezone.utc) - timedelta(hours=24)
                )
                .order_by(desc(BinanceOpenInterest.timestamp))
                .first()
            )
            top = (
                session.query(BinanceLongShortRatio)
                .filter(BinanceLongShortRatio.ratio_type == "top_position")
                .order_by(desc(BinanceLongShortRatio.timestamp))
                .first()
            )
            glob = (
                session.query(BinanceLongShortRatio)
                .filter(BinanceLongShortRatio.ratio_type == "global_account")
                .order_by(desc(BinanceLongShortRatio.timestamp))
                .first()
            )
            taker = (
                session.query(BinanceLongShortRatio)
                .filter(BinanceLongShortRatio.ratio_type == "taker")
                .order_by(desc(BinanceLongShortRatio.timestamp))
                .first()
            )

        oi_change = None
        if oi_latest and oi_24h_ago and oi_24h_ago.open_interest:
            oi_change = round(
                (oi_latest.open_interest - oi_24h_ago.open_interest)
                / oi_24h_ago.open_interest * 100, 2,
            )

        state["binance"] = {
            "funding_rate_pct": round(fr.funding_rate * 100, 4) if fr else None,
            "open_interest": oi_latest.open_interest if oi_latest else None,
            "open_interest_change_24h_pct": oi_change,
            "top_trader_long_pct": top.long_pct if top else None,
            "global_retail_long_pct": glob.long_pct if glob else None,
            "taker_buysell_ratio": taker.long_short_ratio if taker else None,
        }
    except Exception as exc:
        state["binance"] = {"error": str(exc)}

    # Orderbook snapshot
    try:
        import requests
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/depth",
            params={"symbol": settings.binance_symbol or "CLUSDT", "limit": 50},
            timeout=5,
        )
        r.raise_for_status()
        raw = r.json()
        bids = [(float(p), float(q)) for p, q in raw.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in raw.get("asks", [])]
        bid_vol = sum(q for _, q in bids)
        ask_vol = sum(q for _, q in asks)
        imb = (bid_vol - ask_vol) / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else 0.0
        state["orderbook"] = {
            "mid": (bids[0][0] + asks[0][0]) / 2 if bids and asks else None,
            "imbalance_pct": round(imb * 100, 1),
            "total_bid_vol": round(bid_vol, 1),
            "total_ask_vol": round(ask_vol, 1),
        }
    except Exception as exc:
        state["orderbook"] = {"error": str(exc)}

    # Recent liquidations aggregate (24h)
    try:
        from shared.models.base import SessionLocal
        from shared.models.binance_metrics import BinanceLiquidation
        from sqlalchemy import func
        with SessionLocal() as session:
            since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
            count = (
                session.query(func.count(BinanceLiquidation.id))
                .filter(BinanceLiquidation.timestamp >= since)
                .scalar() or 0
            )
            longs_liq = (
                session.query(func.sum(BinanceLiquidation.quote_qty_usd))
                .filter(
                    BinanceLiquidation.timestamp >= since,
                    BinanceLiquidation.side == "SELL",
                ).scalar() or 0
            )
            shorts_liq = (
                session.query(func.sum(BinanceLiquidation.quote_qty_usd))
                .filter(
                    BinanceLiquidation.timestamp >= since,
                    BinanceLiquidation.side == "BUY",
                ).scalar() or 0
            )
        state["liquidations_24h"] = {
            "count": count,
            "longs_liquidated_usd": round(float(longs_liq), 0),
            "shorts_liquidated_usd": round(float(shorts_liq), 0),
        }
    except Exception as exc:
        state["liquidations_24h"] = {"error": str(exc)}

    # Recent breaking news (last 30 min)
    try:
        from shared.models.base import SessionLocal
        from shared.models.knowledge import KnowledgeSummary
        from sqlalchemy import desc
        with SessionLocal() as session:
            news = (
                session.query(KnowledgeSummary)
                .filter(
                    KnowledgeSummary.timestamp
                    >= datetime.now(tz=timezone.utc) - timedelta(minutes=30)
                )
                .order_by(desc(KnowledgeSummary.timestamp))
                .limit(3)
                .all()
            )
            state["recent_news"] = [
                {
                    "time": n.timestamp.isoformat(),
                    "summary": (n.summary or "")[:200],
                    "sentiment": n.sentiment_label,
                }
                for n in news
            ]
    except Exception as exc:
        state["recent_news"] = {"error": str(exc)}

    return state


def _strip_json(text: str) -> str:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            text = m.group(0)
    return re.sub(r",\s*([\]\}])", r"\1", text)


def compute_now_brief(force: bool = False) -> dict:
    """Return the cached brief, regenerating if expired or forced."""
    global _cache, _cache_ts

    with _lock:
        now = time.time()
        age = now - _cache_ts
        if not force and _cache is not None and age < CACHE_TTL_SECONDS:
            result = dict(_cache)
            result["cache_age_seconds"] = round(age, 1)
            return result

        # Regenerate
        state = _gather_dashboard_state()
        user_prompt = (
            "## Current dashboard state (authoritative — do not invent numbers)\n"
            f"{json.dumps(state, indent=2, default=str)[:14000]}\n\n"
            "Produce the brief now. Return ONLY the JSON object."
        )

        call_start = time.time()
        try:
            response = _get_client().messages.create(
                model=MODEL,
                max_tokens=900,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            record_anthropic_call(
                call_site="now_brief.haiku",
                model=MODEL,
                usage=response.usage,
                duration_ms=(time.time() - call_start) * 1000,
            )
            raw = response.content[0].text if response.content else ""
            cleaned = _strip_json(raw)
            parsed = json.loads(cleaned)
        except Exception as exc:
            logger.exception("Now Brief generation failed")
            record_failure(
                call_site="now_brief.haiku",
                model=MODEL,
                provider="anthropic",
                duration_ms=(time.time() - call_start) * 1000,
            )
            return {
                "error": f"brief generation failed: {exc}",
                "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            }

        parsed["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
        parsed["cache_age_seconds"] = 0.0
        _cache = parsed
        _cache_ts = now
        return dict(parsed)


# ---------------------------------------------------------------------------
# Signal Confluence — pure-logic classifier, no LLM
# ---------------------------------------------------------------------------

def compute_signal_confluence() -> dict:
    """Classify every current signal as BULL / BEAR / NEUTRAL with rationale.

    Returns:
    {
      "bull": [{"signal": "...", "value": "...", "reason": "..."}],
      "bear": [...],
      "neutral": [...],
      "total": N,
      "confluence_score": 0..100,  # how aligned
      "dominant_side": "BULL"|"BEAR"|"MIXED",
      "as_of": "..."
    }
    """
    bull: list[dict] = []
    bear: list[dict] = []
    neutral: list[dict] = []

    def _add(side: str, signal: str, value, reason: str) -> None:
        entry = {"signal": signal, "value": value, "reason": reason}
        if side == "BULL":
            bull.append(entry)
        elif side == "BEAR":
            bear.append(entry)
        else:
            neutral.append(entry)

    state = _gather_dashboard_state()

    # Scores
    scores = state.get("scores") or {}
    for key, threshold, label in [
        ("technical", 10, "Technical"),
        ("fundamental", 10, "Fundamental"),
        ("sentiment", 10, "Sentiment"),
        ("shipping", 10, "Shipping"),
        ("unified", 10, "Unified"),
    ]:
        val = scores.get(key)
        if val is None:
            _add("NEUTRAL", label, "N/A", "no data")
            continue
        if val >= threshold:
            _add("BULL", label, f"{val:.1f}", f"score > +{threshold}")
        elif val <= -threshold:
            _add("BEAR", label, f"{val:.1f}", f"score < -{threshold}")
        else:
            _add("NEUTRAL", label, f"{val:.1f}", "in neutral zone")

    # Conviction direction
    conv = state.get("conviction") or {}
    direction = conv.get("direction")
    score = conv.get("score", 0)
    if direction == "BULL" and score >= 20:
        _add("BULL", "Conviction Meter", f"{score}/100", f"net bull score {conv.get('signed_score')}")
    elif direction == "BEAR" and score >= 20:
        _add("BEAR", "Conviction Meter", f"{score}/100", f"net bear score {conv.get('signed_score')}")
    else:
        _add("NEUTRAL", "Conviction Meter", f"{score}/100", "below signal threshold")

    # Funding rate (contrarian)
    binance = state.get("binance") or {}
    funding = binance.get("funding_rate_pct")
    if funding is not None:
        if funding <= -0.03:
            _add("BULL", "Funding rate", f"{funding}%", "shorts crowded — squeeze risk")
        elif funding >= 0.03:
            _add("BEAR", "Funding rate", f"{funding}%", "longs crowded — top risk")
        else:
            _add("NEUTRAL", "Funding rate", f"{funding}%", "normal range")

    # Open interest change
    oi_change = binance.get("open_interest_change_24h_pct")
    if oi_change is not None:
        if oi_change >= 5:
            _add("NEUTRAL", "Open Interest", f"{oi_change}% 24h",
                 "rising — conviction trade, direction unclear without price context")
        elif oi_change <= -5:
            _add("NEUTRAL", "Open Interest", f"{oi_change}% 24h",
                 "falling — positions unwinding")
        else:
            _add("NEUTRAL", "Open Interest", f"{oi_change}% 24h", "flat")

    # Retail vs smart money (contrarian — retail crowded = contrarian signal)
    top = binance.get("top_trader_long_pct")
    glob = binance.get("global_retail_long_pct")
    if top is not None and glob is not None:
        delta = (glob - top) * 100
        if delta >= 10:
            _add("BEAR", "Retail vs smart money", f"+{delta:.1f}%",
                 "retail more long than smart money")
        elif delta <= -10:
            _add("BULL", "Retail vs smart money", f"{delta:.1f}%",
                 "retail more short than smart money")
        else:
            _add("NEUTRAL", "Retail vs smart money", f"{delta:+.1f}%", "aligned")

    # Taker flow
    taker = binance.get("taker_buysell_ratio")
    if taker is not None:
        if taker >= 1.15:
            _add("BULL", "Taker flow", f"{taker:.2f}", "aggressive buying")
        elif taker <= 0.85:
            _add("BEAR", "Taker flow", f"{taker:.2f}", "aggressive selling")
        else:
            _add("NEUTRAL", "Taker flow", f"{taker:.2f}", "balanced")

    # Orderbook imbalance
    ob = state.get("orderbook") or {}
    imb = ob.get("imbalance_pct")
    if imb is not None:
        if imb >= 20:
            _add("BULL", "Order book imbalance", f"+{imb}%", "bid wall dominating")
        elif imb <= -20:
            _add("BEAR", "Order book imbalance", f"{imb}%", "ask wall dominating")
        else:
            _add("NEUTRAL", "Order book imbalance", f"{imb:+}%", "balanced")

    # Liquidations 24h
    liq = state.get("liquidations_24h") or {}
    longs_liq = liq.get("longs_liquidated_usd") or 0
    shorts_liq = liq.get("shorts_liquidated_usd") or 0
    if longs_liq + shorts_liq > 0:
        if longs_liq > shorts_liq * 2:
            _add("BULL", "Liquidations 24h",
                 f"longs ${longs_liq/1000:.0f}K", "long capitulation — bottom signal")
        elif shorts_liq > longs_liq * 2:
            _add("BEAR", "Liquidations 24h",
                 f"shorts ${shorts_liq/1000:.0f}K", "short squeeze — top signal")
        else:
            _add("NEUTRAL", "Liquidations 24h",
                 f"L${longs_liq/1000:.0f}K S${shorts_liq/1000:.0f}K", "balanced")

    total = len(bull) + len(bear) + len(neutral)
    if total == 0:
        return {
            "bull": [], "bear": [], "neutral": [], "total": 0,
            "confluence_score": 0, "dominant_side": "MIXED",
            "as_of": datetime.now(tz=timezone.utc).isoformat(),
        }

    # Confluence score: dominance of majority side over minority
    directional = len(bull) + len(bear)
    if directional == 0:
        confluence = 0
        dominant = "MIXED"
    else:
        majority = max(len(bull), len(bear))
        minority = min(len(bull), len(bear))
        confluence = round(((majority - minority) / directional) * 100)
        if len(bull) > len(bear) + 1:
            dominant = "BULL"
        elif len(bear) > len(bull) + 1:
            dominant = "BEAR"
        else:
            dominant = "MIXED"

    return {
        "bull": bull,
        "bear": bear,
        "neutral": neutral,
        "total": total,
        "bull_count": len(bull),
        "bear_count": len(bear),
        "neutral_count": len(neutral),
        "confluence_score": confluence,
        "dominant_side": dominant,
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
    }

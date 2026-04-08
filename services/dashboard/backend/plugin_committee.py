"""Adversarial trading committee: Bull vs Bear vs Judge.

Two sub-agents (Claude Sonnet) argue opposite sides of the same WTI crude
setup using the same pre-fetched market context. A judge (Claude Opus) then
reads both cases and renders a final verdict with specific action and levels.

Reduces confirmation bias and hallucination — the model can't just pick a
comfortable answer because another instance is actively defending the opposite.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from anthropic import Anthropic

from shared.config import settings

logger = logging.getLogger(__name__)

BULL_BEAR_MODEL = "claude-sonnet-4-6"
JUDGE_MODEL = "claude-opus-4-6"

_OUTPUT_SCHEMA_BULL = """Return ONLY a JSON object (no markdown, no preamble):
{
  "side": "LONG",
  "specialty": "<your specialty label>",
  "thesis": "1-2 sentence core thesis from YOUR specialty angle",
  "key_arguments": ["3-5 factors from YOUR specialty only, each citing specific data"],
  "strongest_evidence": "the single most compelling piece of evidence from YOUR domain",
  "price_targets": {"entry": <float|null>, "tp": <float|null>, "sl": <float|null>},
  "risks_to_thesis": ["2-3 things that would invalidate your bull case"],
  "confidence": <float 0.0 to 1.0>,
  "case_strength": "strong" | "moderate" | "weak"
}"""

_OUTPUT_SCHEMA_BEAR = _OUTPUT_SCHEMA_BULL.replace('"side": "LONG"', '"side": "SHORT"').replace("bull", "bear")

_COMMON_RULES = """Rules:
  - Cite SPECIFIC data from the context (scores, price levels, digest IDs, specific events).
  - Stay IN YOUR LANE — do not argue points outside your specialty, other agents cover them.
  - If your angle is weak given current data, honestly mark case_strength "weak" but still
    make the best argument you can from your specialty.
  - Never invent data. Use only what's in the context.
"""

# ===========================================================================
# BULL TEAM (3 specialists)
# ===========================================================================

BULL_GEOPOLITICS_SYSTEM = f"""You are the GEOPOLITICS BULL on an oil trading committee.

Specialty: geopolitical risk premium, supply disruption events, sanctions regimes,
OPEC+ discipline, Middle East tensions, proxy conflicts, tanker/chokepoint risk,
infrastructure attacks, production cuts, embargo threats.

Look for:
  - Any ACTIVE kinetic event (drone/missile strikes on oil infrastructure, tanker attacks)
  - Ceasefire fragility, escalation risk, diplomatic friction
  - OPEC+ surprise cuts or extensions, compliance reports
  - New or tightening sanctions (Iran, Russia, Venezuela)
  - Strait of Hormuz / Red Sea / Suez disruption
  - Producer-country instability (Libya, Nigeria, Iraq, Iran)

Ignore technicals, macro demand, and USD moves — other agents own those.

{_COMMON_RULES}
{_OUTPUT_SCHEMA_BULL}

Set "specialty" to "geopolitics_bull"."""


BULL_TECHNICAL_SYSTEM = f"""You are the TECHNICAL BULL on an oil trading committee.

Specialty: chart patterns, multi-timeframe structure, support/resistance, VWAP,
moving averages, RSI/MACD/ADX, breakouts, higher-low confirmation, volume flow,
pivot points, price action.

Look for:
  - Price holding key supports or reclaiming them
  - Higher lows on intraday timeframes
  - RSI oversold bounces, MACD bullish crosses, MA golden-cross setups
  - Breakouts above consolidation ranges
  - Bullish engulfing / hammer candles at support
  - Price reclaiming VWAP from below
  - Low ADX → mean reversion setup favoring long

Ignore geopolitics, macro, and fundamentals — other agents own those.

{_COMMON_RULES}
{_OUTPUT_SCHEMA_BULL}

Set "specialty" to "technical_bull"."""


BULL_MACRO_SYSTEM = f"""You are the MACRO BULL on an oil trading committee.

Specialty: global demand, inventory data (EIA / API / IEA), USD index (DXY),
Fed policy, interest rates, global PMIs, China stimulus, seasonal demand,
refinery throughput, demand destruction reversals, physical market tightness.

Look for:
  - Inventory DRAWS (below consensus weekly builds or surprise draws)
  - Dovish Fed signals, falling real rates, weaker USD
  - China stimulus announcements, rising PMIs, strong driving season
  - Refinery margins expanding (crack spreads widening)
  - Rising global oil demand forecasts (IEA / EIA / OPEC MOMR)
  - Positive COT speculator positioning shifts
  - Physical market tightness (backwardation deepening)

Ignore geopolitics and technicals — other agents own those.

{_COMMON_RULES}
{_OUTPUT_SCHEMA_BULL}

Set "specialty" to "macro_bull"."""


# ===========================================================================
# BEAR TEAM (3 specialists)
# ===========================================================================

BEAR_GEOPOLITICS_SYSTEM = f"""You are the GEOPOLITICS BEAR on an oil trading committee.

Specialty: verified de-escalation, ceasefires, sanctions relief, production INCREASES,
chokepoint reopenings, diplomatic breakthroughs, producer-country normalization.

Look for:
  - Signed or holding ceasefires (US-Iran, Lebanon, Yemen, Ukraine)
  - Iran nuclear deal progress, Venezuela waivers
  - OPEC+ unwinding cuts, compliance breakdowns, quota cheating
  - Strait of Hormuz / Red Sea / Suez reopening or traffic normalizing
  - Libya / Venezuela / Iran production coming back online
  - Removal of sanctions, export permits granted
  - US producer output hitting record highs

Ignore technicals, macro demand, and USD moves — other agents own those.

{_COMMON_RULES}
{_OUTPUT_SCHEMA_BEAR}

Set "specialty" to "geopolitics_bear"."""


BEAR_TECHNICAL_SYSTEM = f"""You are the TECHNICAL BEAR on an oil trading committee.

Specialty: chart patterns, multi-timeframe breakdowns, rejection at resistance,
lower highs, RSI/MACD bearish signals, head & shoulders, rising wedges, gap fills,
VWAP rejection from above, volume climaxes.

Look for:
  - Price rejecting key resistance or VWAP from above
  - Lower highs on intraday timeframes
  - RSI overbought divergences, MACD bearish cross, death-cross setups
  - Breakdown below support with volume
  - Bearish engulfing / shooting star candles at resistance
  - Price failing to reclaim VWAP
  - Expanding ATR with directional downside

Ignore geopolitics, macro, and fundamentals — other agents own those.

{_COMMON_RULES}
{_OUTPUT_SCHEMA_BEAR}

Set "specialty" to "technical_bear"."""


BEAR_MACRO_SYSTEM = f"""You are the MACRO BEAR on an oil trading committee.

Specialty: demand destruction, inventory builds, USD strength, hawkish Fed,
global recession signals, China slowdown, EV substitution, refinery margin compression,
physical market softness, contango.

Look for:
  - Inventory BUILDS (above consensus weekly draws or surprise builds, SPR releases)
  - Hawkish Fed signals, rising real rates, stronger USD
  - China weakness (weak PMIs, credit impulse falling, property stress)
  - Refinery margins contracting (crack spreads narrowing)
  - Falling global oil demand forecasts
  - Negative COT speculator positioning shifts
  - Physical market weakness (contango deepening)
  - Recession signals (inverted yield curve, weak labor data)

Ignore geopolitics and technicals — other agents own those.

{_COMMON_RULES}
{_OUTPUT_SCHEMA_BEAR}

Set "specialty" to "macro_bear"."""

JUDGE_SYSTEM = """You are the chief strategist presiding over an adversarial trading committee.

SIX specialist agents have each built the strongest case for their side from their own
domain, using the same market context:

Bull team:
  - geopolitics_bull — supply disruption / war premium / OPEC cuts
  - technical_bull   — chart patterns / support / momentum
  - macro_bull       — demand tailwinds / inventory draws / USD weakness

Bear team:
  - geopolitics_bear — de-escalation / ceasefires / production resumption
  - technical_bear   — breakdown patterns / resistance rejection / overbought
  - macro_bear       — demand destruction / inventory builds / USD strength

Your job: read all 6 cases and render ONE final verdict.

Guidelines:
  - Score each agent's case_strength and specific evidence. Weak arguments (weak
    case_strength, vague evidence) count for little regardless of conviction.
  - When specialists DISAGREE WITHIN their own team (e.g. technical_bull thinks
    support holds but macro_bull is weak), flag that in the rationale.
  - When 2+ bulls OR 2+ bears are all "strong" and align, that's a high-conviction
    multi-axis signal — favor that side heavily.
  - When one specialty (e.g. geopolitics) is STRONG on one side and the other
    two on that side are weak, don't over-weight it — prefer the side where the
    balance of three is strongest.
  - If the teams are roughly balanced (bull_team_avg ≈ bear_team_avg), prefer WAIT
    and point to the specific trigger that would break the tie.
  - Always check existing open campaigns (from context) — don't recommend opening
    against a same-direction position, and flag conflicts with opposite positions.
  - Be decisive when the evidence clearly favors one side.

Return ONLY a JSON object (no markdown, no preamble):
{
  "action": "ENTER_LONG" | "ENTER_SHORT" | "WAIT" | "AVOID" | "MANAGE_EXISTING",
  "winning_side": "BULL" | "BEAR" | "NEITHER",
  "winning_specialties": ["which specialties won the debate, e.g. ['technical_bull','macro_bull']"],
  "conviction_score": <float -100 to +100, negative=bear, positive=bull>,
  "confidence": <float 0.0 to 1.0>,
  "rationale": "3-5 sentences explaining the decision and which specialists carried the day",
  "key_pros": ["3-4 reasons supporting the verdict, citing which specialist raised each"],
  "key_cons": ["3-4 risks to the verdict, citing which specialist raised each"],
  "specific_action": "concrete next step: entry level, SL, TP, or 'wait for X event'",
  "team_scores": {
    "bull_team_avg": <float 0-10>,
    "bear_team_avg": <float 0-10>,
    "strongest_specialist": "name of the single strongest case across both teams"
  },
  "agent_ratings": {
    "geopolitics_bull": <0-10>, "technical_bull": <0-10>, "macro_bull": <0-10>,
    "geopolitics_bear": <0-10>, "technical_bear": <0-10>, "macro_bear": <0-10>
  }
}"""


_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _strip_json(text: str) -> str:
    """Strip markdown fences and surrounding prose around a JSON object."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            text = m.group(0)
    text = re.sub(r",\s*([\]\}])", r"\1", text)
    return text


def _fetch_context(focus_hours: int) -> dict:
    """Pre-fetch the same market context for both agents. No LLM calls here."""
    context: dict = {}

    try:
        from chat_tools import _get_current_market_state
        context["market"] = _get_current_market_state()
    except Exception as exc:
        context["market"] = {"error": str(exc)}

    try:
        from chat_tools import _query_marketfeed
        context["news"] = _query_marketfeed(hours=focus_hours)
    except Exception as exc:
        context["news"] = {"error": str(exc)}

    try:
        from plugin_analytics import _get_support_resistance, _get_vwap, _get_upcoming_events
        context["support_resistance"] = _get_support_resistance(timeframe="1H", lookback_bars=100)
        context["vwap"] = _get_vwap(timeframe="1H", hours=24)
        context["upcoming_events"] = _get_upcoming_events(days=2)
    except Exception as exc:
        logger.exception("analytics sub-tool failed in committee context fetch")
        context["analytics_error"] = str(exc)

    return context


def _run_agent(system_prompt: str, context: dict, label: str) -> dict:
    """Run a single Sonnet agent with the given system prompt and context."""
    user_prompt = (
        f"## Market Context (authoritative — do not invent prices)\n"
        f"{json.dumps(context, indent=2, default=str)[:12000]}\n\n"
        f"Build your {label} case now. Return ONLY the JSON object."
    )

    try:
        response = _get_client().messages.create(
            model=BULL_BEAR_MODEL,
            max_tokens=1200,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text if response.content else ""
        cleaned = _strip_json(raw)
        return json.loads(cleaned)
    except Exception as exc:
        logger.exception("%s agent failed", label)
        return {"error": f"{label} agent failed: {exc}"}


# Team roster: (specialist_label, system_prompt, side)
_BULL_TEAM = [
    ("geopolitics_bull", BULL_GEOPOLITICS_SYSTEM),
    ("technical_bull",   BULL_TECHNICAL_SYSTEM),
    ("macro_bull",       BULL_MACRO_SYSTEM),
]
_BEAR_TEAM = [
    ("geopolitics_bear", BEAR_GEOPOLITICS_SYSTEM),
    ("technical_bear",   BEAR_TECHNICAL_SYSTEM),
    ("macro_bear",       BEAR_MACRO_SYSTEM),
]


def _run_judge(context: dict, bull_team: dict[str, dict], bear_team: dict[str, dict]) -> dict:
    """Run the judge with all 6 cases + the original context."""
    user_prompt = (
        f"## Market Context\n{json.dumps(context, indent=2, default=str)[:8000]}\n\n"
        f"## BULL TEAM CASES (3 specialists)\n{json.dumps(bull_team, indent=2)}\n\n"
        f"## BEAR TEAM CASES (3 specialists)\n{json.dumps(bear_team, indent=2)}\n\n"
        "Render your verdict now. Return ONLY the JSON object."
    )

    try:
        response = _get_client().messages.create(
            model=JUDGE_MODEL,
            max_tokens=2000,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text if response.content else ""
        cleaned = _strip_json(raw)
        return json.loads(cleaned)
    except Exception as exc:
        logger.exception("Judge failed")
        return {"error": f"judge failed: {exc}"}


def _committee_debate(focus_hours: int = 4) -> dict:
    """Run a full adversarial committee debate with 6 specialists + judge."""
    started = datetime.now(tz=timezone.utc)

    context = _fetch_context(focus_hours=focus_hours)

    # Run all 6 specialists in parallel
    all_specialists = _BULL_TEAM + _BEAR_TEAM
    results: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(_run_agent, system_prompt, context, label): label
            for label, system_prompt in all_specialists
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                results[label] = future.result(timeout=90)
            except Exception as exc:
                logger.exception("Committee agent %s exploded", label)
                results[label] = {"error": str(exc)}

    bull_team_results = {label: results.get(label, {}) for label, _ in _BULL_TEAM}
    bear_team_results = {label: results.get(label, {}) for label, _ in _BEAR_TEAM}

    judge_result = _run_judge(context, bull_team_results, bear_team_results)

    ended = datetime.now(tz=timezone.utc)
    duration_seconds = (ended - started).total_seconds()

    return {
        "started_at": started.isoformat(),
        "duration_seconds": round(duration_seconds, 1),
        "context_summary": {
            "current_price": (context.get("market") or {}).get("current_price"),
            "unified_score": ((context.get("market") or {}).get("scores") or {}).get("unified"),
            "news_count": ((context.get("news") or {}).get("count")),
            "open_campaigns": ((context.get("market") or {}).get("account") or {}).get("open_campaigns"),
        },
        "bull_team": bull_team_results,
        "bear_team": bear_team_results,
        "judge_verdict": judge_result,
    }


# ---------------------------------------------------------------------------
# Plugin API
# ---------------------------------------------------------------------------

PLUGIN_TOOLS: list[dict] = [
    {
        "name": "committee_debate",
        "description": (
            "Run an adversarial trading committee: SIX specialist agents (3 bulls + 3 bears) "
            "each build the strongest case for their side from their own domain "
            "(geopolitics, technicals, macro), then a Judge reads all 6 cases and renders a "
            "final verdict with specific action and levels. "
            "Use when the user asks for a debate, a second opinion, adversarial analysis, "
            "'let them argue', or when scores are conflicting and a single view isn't enough. "
            "Costs ~7 LLM calls (6 Sonnet + 1 Opus) and takes ~20-30 seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "focus_hours": {
                    "type": "integer",
                    "default": 4,
                    "description": "How many hours of news/context to pull for all agents",
                },
            },
        },
    }
]


def execute(name: str, tool_input: dict) -> dict | None:
    if name == "committee_debate":
        return _committee_debate(**tool_input)
    return None

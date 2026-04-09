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
  - Stay IN YOUR LANE for the PRIMARY thesis and key arguments — other specialists
    cover their own domains and the Judge reconciles them.
  - HOWEVER, you may reference out-of-lane data AS CONTEXT when it directly amplifies
    or undermines your thesis. E.g. a technicals bull can note "funding -0.30% adds
    squeeze-risk fuel to this bid defense" without making macro the main argument.
  - If your angle is weak given current data, honestly mark case_strength "weak" but still
    make the best argument you can from your specialty.
  - Never invent data. Use only what's in the context.

Available data in the context dict (use any of these, cite specifically):
  - market: current price, all 5 scores, open campaigns, account state
  - news: recent @marketfeed digests with sentiment
  - support_resistance: key S/R levels from 1H data
  - vwap_24h / vwap_168h: session + weekly VWAP with distance pct
  - pivot_points: classic daily pivot, R1/R2/S1/S2 and current position
  - upcoming_events: 7-day economic calendar (EIA, FOMC, OPEC, IEA)
  - active_watch: any active live-watch monitoring session
  - conviction: composite 0-100 meter with top drivers
  - anomalies.current: currently-firing rare/extreme conditions
  - anomalies.recent_24h: log of anomalies that fired in last 24h
  - binance_metrics: funding rate history, open interest (+ 24h change pct),
    top trader / global retail long pct, retail-vs-smart delta, taker flow,
    liquidations 24h summary with dominant side
  - orderbook: mid, best bid/ask, total depth, imbalance pct, top 5 levels
  - whale_trades: 24h aggregated >= $10k trades (buy/sell/delta USD, dominant side)
  - volume_profile: POC (point of control), value area (70% volume range)
  - cvd: Cumulative Volume Delta + divergence detection
  - cross_assets: DXY / SPX / Gold / BTC / VIX levels + 24h change + correlation
  - scenarios: PnL/equity/margin at price offsets, key levels (breakeven, stop-out)
  - monte_carlo_24h: GBM simulation — probability of margin call / -50% hard stop
  - trade_journal.stats: user's own win rate, profit factor, avg win/loss
  - trade_journal.recent_trades: last 10 closed campaigns with outcomes
  - pattern_match: top-N historically similar moments + forward return distribution
  - signal_performance: per-feature bucket stats (does high unified score actually
    predict forward returns?)
  - smart_alerts: user-configured confluence alerts with current match status
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
  - SAME-DOMAIN NEUTRALIZATION: If bull and bear specialists in the SAME domain
    (e.g. technical_bull vs technical_bear) have confidence within 0.15 of each
    other AND look at the same price action/data, treat that domain as NEUTRAL
    in your scoring — they are framing the same facts differently, not providing
    independent evidence. Explicitly call this out in the rationale: "technicals
    neutralize — bull and bear interpret the same bounce as buying/dead-cat".
  - AGENT FAILURES: If an agent has status="agent_failed", DO NOT count it in the
    team average and explicitly note the failure in rationale. Reduce the
    effective team size (e.g. if bull macro failed, bull team is 2 agents).
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
  "rationale": "3-5 sentences explaining the decision and which specialists carried the day. MUST explicitly note any same-domain neutralization and any agent failures.",
  "key_pros": ["3-4 reasons supporting the verdict, citing which specialist raised each"],
  "key_cons": ["3-4 risks to the verdict, citing which specialist raised each"],
  "specific_action": "concrete next step in plain text: entry level, SL, TP, or 'wait for X event'",
  "trade_levels": {
    "entry": <float|null — specific entry level in USD, or null if action is WAIT/AVOID>,
    "stop_loss": <float|null>,
    "take_profit": <float|null>,
    "side": "LONG" | "SHORT" | null
  },
  "neutralized_domains": ["list of domains where bull/bear cancelled out, e.g. ['technical']"],
  "failed_agents": ["list of agent labels that had status='agent_failed'"],
  "team_scores": {
    "bull_team_avg": <float 0-10, computed over NON-FAILED bull agents only>,
    "bear_team_avg": <float 0-10, computed over NON-FAILED bear agents only>,
    "strongest_specialist": "name of the single strongest case across both teams"
  },
  "agent_ratings": {
    "geopolitics_bull": <0-10>, "technical_bull": <0-10>, "macro_bull": <0-10>,
    "geopolitics_bear": <0-10>, "technical_bear": <0-10>, "macro_bear": <0-10>
  }
}

IMPORTANT: trade_levels MUST be filled when action is ENTER_LONG or ENTER_SHORT.
For WAIT/AVOID/MANAGE_EXISTING, set entry/sl/tp to null. R:R will be computed
deterministically downstream — do NOT mention R:R ratios in your rationale text."""


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
    """Pre-fetch EVERY data surface the dashboard has into one dict.

    The committee specialists get access to the full situational picture —
    scores, news, Binance derivatives, microstructure, cross-assets, flow,
    risk scenarios, history, anomalies — so each can cite specific data
    from outside its "lane" when the evidence is unambiguous (e.g. a
    technicals bull referencing funding-extreme as context for why dips
    are getting bought). Failures in sub-fetches become error strings
    so one broken source can't crash the whole debate.
    """
    context: dict = {}

    # ---- Market state & news (existing) ----
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

    # ---- Technical context: S/R, VWAP, pivots, events ----
    try:
        from plugin_analytics import (
            _get_support_resistance,
            _get_vwap,
            _get_upcoming_events,
            _get_pivot_points,
        )
        context["support_resistance"] = _get_support_resistance(timeframe="1H", lookback_bars=100)
        context["vwap_24h"] = _get_vwap(timeframe="1H", hours=24)
        context["vwap_168h"] = _get_vwap(timeframe="1H", hours=168)
        context["pivot_points"] = _get_pivot_points()
        context["upcoming_events"] = _get_upcoming_events(days=7)
    except Exception as exc:
        logger.exception("analytics sub-tool failed")
        context["analytics_error"] = str(exc)

    # ---- Live watch session ----
    try:
        from plugin_live_watch import _get_active_watch
        context["active_watch"] = _get_active_watch()
    except Exception as exc:
        context["active_watch"] = {"error": str(exc)}

    # ---- Conviction meter + top drivers ----
    try:
        from plugin_conviction import compute_conviction
        context["conviction"] = compute_conviction()
    except Exception as exc:
        context["conviction"] = {"error": str(exc)}

    # ---- Anomaly radar (currently active + recent history) ----
    try:
        from plugin_anomalies import detect_anomalies, get_anomaly_history
        context["anomalies"] = {
            "current": detect_anomalies(),
            "recent_24h": get_anomaly_history(hours=24, limit=30),
        }
    except Exception as exc:
        context["anomalies"] = {"error": str(exc)}

    # ---- Binance derivatives metrics (funding, OI, L/S, liquidations) ----
    try:
        from shared.models.base import SessionLocal
        from shared.models.binance_metrics import (
            BinanceFundingRate,
            BinanceOpenInterest,
            BinanceLongShortRatio,
            BinanceLiquidation,
        )
        from sqlalchemy import desc, func
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        now = _dt.now(tz=_tz.utc)
        with SessionLocal() as session:
            fr = (
                session.query(BinanceFundingRate)
                .order_by(desc(BinanceFundingRate.funding_time))
                .limit(5).all()
            )
            oi_latest = (
                session.query(BinanceOpenInterest)
                .order_by(desc(BinanceOpenInterest.timestamp))
                .first()
            )
            oi_24h_ago = (
                session.query(BinanceOpenInterest)
                .filter(BinanceOpenInterest.timestamp <= now - _td(hours=24))
                .order_by(desc(BinanceOpenInterest.timestamp))
                .first()
            )
            oi_change_pct = None
            if oi_latest and oi_24h_ago and oi_24h_ago.open_interest:
                oi_change_pct = round(
                    (oi_latest.open_interest - oi_24h_ago.open_interest)
                    / oi_24h_ago.open_interest * 100, 2,
                )

            def _latest(rt):
                return (
                    session.query(BinanceLongShortRatio)
                    .filter(BinanceLongShortRatio.ratio_type == rt)
                    .order_by(desc(BinanceLongShortRatio.timestamp))
                    .first()
                )
            top = _latest("top_position")
            glob = _latest("global_account")
            taker = _latest("taker")

            liq_24h = now - _td(hours=24)
            longs_liq = session.query(func.sum(BinanceLiquidation.quote_qty_usd)).filter(
                BinanceLiquidation.timestamp >= liq_24h,
                BinanceLiquidation.side == "SELL",
            ).scalar() or 0
            shorts_liq = session.query(func.sum(BinanceLiquidation.quote_qty_usd)).filter(
                BinanceLiquidation.timestamp >= liq_24h,
                BinanceLiquidation.side == "BUY",
            ).scalar() or 0

        context["binance_metrics"] = {
            "funding_rates_last_5": [
                {"time": f.funding_time.isoformat(), "rate_pct": round(f.funding_rate * 100, 4)}
                for f in fr
            ],
            "open_interest": oi_latest.open_interest if oi_latest else None,
            "open_interest_change_24h_pct": oi_change_pct,
            "top_trader_long_pct": top.long_pct if top else None,
            "global_retail_long_pct": glob.long_pct if glob else None,
            "retail_vs_smart_delta_pct": (
                round((glob.long_pct - top.long_pct) * 100, 2)
                if top and glob and top.long_pct is not None and glob.long_pct is not None
                else None
            ),
            "taker_buysell_ratio": taker.long_short_ratio if taker else None,
            "liquidations_24h": {
                "longs_liquidated_usd": round(float(longs_liq), 0),
                "shorts_liquidated_usd": round(float(shorts_liq), 0),
                "dominant_side": (
                    "longs" if longs_liq > shorts_liq * 1.5
                    else "shorts" if shorts_liq > longs_liq * 1.5
                    else "balanced"
                ),
            },
        }
    except Exception as exc:
        logger.exception("binance metrics fetch failed")
        context["binance_metrics"] = {"error": str(exc)}

    # ---- Market microstructure: orderbook, whales, volume profile ----
    try:
        import requests
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/depth",
            params={"symbol": settings.binance_symbol or "CLUSDT", "limit": 100},
            timeout=5,
        )
        raw = r.json()
        bids = [(float(p), float(q)) for p, q in raw.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in raw.get("asks", [])]
        bid_vol = sum(q for _, q in bids)
        ask_vol = sum(q for _, q in asks)
        mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else None
        context["orderbook"] = {
            "mid": mid,
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
            "total_bid_volume": round(bid_vol, 1),
            "total_ask_volume": round(ask_vol, 1),
            "imbalance_pct": round(
                (bid_vol - ask_vol) / (bid_vol + ask_vol) * 100, 2,
            ) if (bid_vol + ask_vol) > 0 else 0.0,
            "top_5_bids": [{"price": p, "qty": q} for p, q in bids[:5]],
            "top_5_asks": [{"price": p, "qty": q} for p, q in asks[:5]],
        }
    except Exception as exc:
        context["orderbook"] = {"error": str(exc)}

    try:
        import requests
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/aggTrades",
            params={"symbol": settings.binance_symbol or "CLUSDT", "limit": 1000},
            timeout=8,
        )
        raw = r.json()
        buy_usd = 0.0
        sell_usd = 0.0
        whale_count = 0
        for row in raw:
            try:
                quote = float(row["p"]) * float(row["q"])
                if quote < 10_000:
                    continue
                whale_count += 1
                if row.get("m"):
                    sell_usd += quote
                else:
                    buy_usd += quote
            except (KeyError, ValueError, TypeError):
                continue
        context["whale_trades"] = {
            "threshold_usd": 10_000,
            "count": whale_count,
            "buy_volume_usd": round(buy_usd, 0),
            "sell_volume_usd": round(sell_usd, 0),
            "delta_usd": round(buy_usd - sell_usd, 0),
            "dominant_side": (
                "BUY" if buy_usd > sell_usd * 1.2
                else "SELL" if sell_usd > buy_usd * 1.2
                else "BALANCED"
            ),
        }
    except Exception as exc:
        context["whale_trades"] = {"error": str(exc)}

    # ---- Volume profile (POC / VAH / VAL) — inline compute from OHLCV ----
    try:
        from shared.models.base import SessionLocal
        from shared.models.ohlcv import OHLCV
        from sqlalchemy import desc
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        since = _dt.now(tz=_tz.utc) - _td(hours=24)
        with SessionLocal() as session:
            bars = (
                session.query(OHLCV)
                .filter(
                    OHLCV.source == "yahoo",
                    OHLCV.timeframe == "5min",
                    OHLCV.timestamp >= since,
                )
                .order_by(OHLCV.timestamp.asc())
                .all()
            )
        if bars:
            typical = [(b.high + b.low + b.close) / 3 for b in bars]
            vols = [b.volume or 0.0 for b in bars]
            p_min, p_max = min(typical), max(typical)
            if p_max > p_min:
                n_buckets = 20
                step = (p_max - p_min) / n_buckets
                hist = [0.0] * n_buckets
                for tp, v in zip(typical, vols):
                    idx = min(n_buckets - 1, int((tp - p_min) / step))
                    hist[idx] += v
                poc_idx = max(range(n_buckets), key=lambda i: hist[i])
                poc_price = p_min + step * (poc_idx + 0.5)
                total = sum(hist)
                target = total * 0.70
                lo = hi = poc_idx
                accum = hist[poc_idx]
                while accum < target and (lo > 0 or hi < n_buckets - 1):
                    left = hist[lo - 1] if lo > 0 else -1
                    right = hist[hi + 1] if hi < n_buckets - 1 else -1
                    if left >= right and lo > 0:
                        lo -= 1; accum += hist[lo]
                    elif hi < n_buckets - 1:
                        hi += 1; accum += hist[hi]
                    else:
                        break
                context["volume_profile"] = {
                    "poc_price": round(poc_price, 3),
                    "value_area_low": round(p_min + step * lo, 3),
                    "value_area_high": round(p_min + step * (hi + 1), 3),
                    "total_volume": round(total, 0),
                    "price_min": round(p_min, 3),
                    "price_max": round(p_max, 3),
                }
    except Exception as exc:
        context["volume_profile_error"] = str(exc)

    # ---- CVD (Cumulative Volume Delta) + divergence ----
    try:
        from plugin_cross_cvd import cvd_series, cross_asset_snapshot
        cvd = cvd_series(minutes=120)
        # Drop the full series to keep context small; keep key numbers
        context["cvd"] = {
            "symbol": cvd.get("symbol"),
            "window_minutes": cvd.get("window_minutes"),
            "current_cvd": cvd.get("current_cvd"),
            "current_price": cvd.get("current_price"),
            "divergence": cvd.get("divergence"),
        }
        context["cross_assets"] = cross_asset_snapshot(hours=24)
    except Exception as exc:
        context["cross_cvd_error"] = str(exc)

    # ---- Scenario calculator + Monte Carlo risk probabilities ----
    try:
        from plugin_risk_tools import compute_scenarios, simulate_margin_call
        context["scenarios"] = compute_scenarios()
        context["monte_carlo_24h"] = simulate_margin_call(horizon_hours=24, n_paths=1500)
    except Exception as exc:
        context["risk_tools_error"] = str(exc)

    # ---- Trade journal stats (user's own historical performance) ----
    try:
        from plugin_trade_journal import get_journal
        journal = get_journal(limit=20)
        context["trade_journal"] = {
            "stats": journal.get("stats"),
            "recent_trades": [
                {
                    "id": e["id"],
                    "side": e["side"],
                    "status": e["status"],
                    "closed_at": e.get("closed_at"),
                    "realized_pnl": e.get("realized_pnl"),
                    "pnl_pct_of_entry_margin": e.get("pnl_pct_of_entry_margin"),
                    "duration_minutes": e.get("duration_minutes"),
                }
                for e in (journal.get("entries") or [])[:10]
            ],
        }
    except Exception as exc:
        context["trade_journal"] = {"error": str(exc)}

    # ---- Pattern match (forward-return distribution for similar moments) ----
    try:
        from plugin_learning import find_similar_moments, compute_signal_performance
        context["pattern_match"] = find_similar_moments(top_n=5)
        context["signal_performance"] = compute_signal_performance()
    except Exception as exc:
        context["learning_error"] = str(exc)

    # ---- Active smart alerts (user-configured triggers) ----
    try:
        from plugin_smart_alerts import list_smart_alerts
        context["smart_alerts"] = [
            {
                "id": a["id"],
                "message": a["message"],
                "status": a["status"],
                "matches_now": a.get("matches_now"),
                "trace": a.get("trace"),
            }
            for a in list_smart_alerts(status="active")
        ]
    except Exception as exc:
        context["smart_alerts_error"] = str(exc)

    return context


def _compute_risk_reward(trade_levels: dict | None) -> dict | None:
    """Compute R:R ratio deterministically from entry/SL/TP.

    Returns None if any level is missing or if the geometry is nonsensical
    (e.g. SL on the wrong side of entry for the given direction).
    """
    if not trade_levels:
        return None
    entry = trade_levels.get("entry")
    sl = trade_levels.get("stop_loss")
    tp = trade_levels.get("take_profit")
    side = (trade_levels.get("side") or "").upper()
    if entry is None or sl is None or tp is None or side not in ("LONG", "SHORT"):
        return None
    try:
        entry = float(entry); sl = float(sl); tp = float(tp)
    except (TypeError, ValueError):
        return None

    if side == "LONG":
        risk = entry - sl
        reward = tp - entry
        geometry_ok = sl < entry < tp
    else:  # SHORT
        risk = sl - entry
        reward = entry - tp
        geometry_ok = tp < entry < sl

    if not geometry_ok or risk <= 0 or reward <= 0:
        return {
            "side": side,
            "entry": entry,
            "stop_loss": sl,
            "take_profit": tp,
            "risk_usd": round(risk, 2),
            "reward_usd": round(reward, 2),
            "rr_ratio": None,
            "geometry_error": "SL/TP on wrong side of entry for declared direction",
        }

    rr = reward / risk
    return {
        "side": side,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "risk_usd": round(risk, 2),
        "reward_usd": round(reward, 2),
        "rr_ratio": round(rr, 2),
        "rr_text": f"1:{rr:.2f}",
    }


_REQUIRED_AGENT_FIELDS = (
    "side",
    "thesis",
    "key_arguments",
    "confidence",
    "case_strength",
)


def _validate_agent_output(parsed: dict, label: str, raw: str) -> dict:
    """Ensure an agent's JSON response has the required fields.

    Returns the parsed dict augmented with a "status" field. If any required
    field is missing or malformed, returns an explicit failure marker so
    downstream rendering can flag it instead of silently showing dashes.
    """
    missing = [f for f in _REQUIRED_AGENT_FIELDS if f not in parsed or parsed[f] in (None, "", [])]
    if missing:
        return {
            "status": "agent_failed",
            "error": f"missing_fields: {', '.join(missing)}",
            "raw_excerpt": raw[:400],
            "specialty": label,
        }

    # Normalise confidence to float in [0, 1]
    try:
        conf = float(parsed.get("confidence", 0.0))
        parsed["confidence"] = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        return {
            "status": "agent_failed",
            "error": "confidence is not a number",
            "raw_excerpt": raw[:400],
            "specialty": label,
        }

    # Normalise case_strength
    if parsed.get("case_strength") not in ("strong", "moderate", "weak"):
        return {
            "status": "agent_failed",
            "error": f"case_strength invalid: {parsed.get('case_strength')}",
            "raw_excerpt": raw[:400],
            "specialty": label,
        }

    parsed["status"] = "ok"
    return parsed


def _run_agent(system_prompt: str, context: dict, label: str) -> dict:
    """Run a single Sonnet agent with the given system prompt and context."""
    # Context now contains a much richer snapshot — up to ~25kb of JSON. Most
    # of it compresses well (repeated keys, floats). We cap at 30k chars and
    # let the model see everything.
    user_prompt = (
        f"## Full Dashboard Context (authoritative — do not invent numbers)\n"
        f"{json.dumps(context, indent=2, default=str)[:30000]}\n\n"
        f"Build your {label} case now. Return ONLY the JSON object."
    )

    raw = ""
    try:
        response = _get_client().messages.create(
            model=BULL_BEAR_MODEL,
            max_tokens=1600,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text if response.content else ""
        cleaned = _strip_json(raw)
        parsed = json.loads(cleaned)
        return _validate_agent_output(parsed, label, raw)
    except json.JSONDecodeError as exc:
        logger.warning("%s agent returned unparseable JSON: %s", label, exc)
        return {
            "status": "agent_failed",
            "error": f"json_decode: {exc}",
            "raw_excerpt": raw[:400],
            "specialty": label,
        }
    except Exception as exc:
        logger.exception("%s agent failed", label)
        return {
            "status": "agent_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "raw_excerpt": raw[:400],
            "specialty": label,
        }


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
        f"## Full Dashboard Context\n{json.dumps(context, indent=2, default=str)[:20000]}\n\n"
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

    # Count successful agents per team so downstream renderers can show
    # "4/6 agents reported" etc instead of silently hiding failures.
    failed_agents = [
        label for label, r in {**bull_team_results, **bear_team_results}.items()
        if r.get("status") == "agent_failed"
    ]

    judge_result = _run_judge(context, bull_team_results, bear_team_results)

    # Deterministically compute R:R from judge's trade_levels — the LLM
    # sometimes hallucinates the math (e.g. claims 2.5:1 when it's actually
    # 3.1:1). This overwrites any R:R the judge might have baked into text.
    risk_reward = None
    if isinstance(judge_result, dict):
        risk_reward = _compute_risk_reward(judge_result.get("trade_levels"))

    ended = datetime.now(tz=timezone.utc)
    duration_seconds = (ended - started).total_seconds()

    active_watch = context.get("active_watch") or {}
    return {
        "started_at": started.isoformat(),
        "duration_seconds": round(duration_seconds, 1),
        "context_summary": {
            "current_price": (context.get("market") or {}).get("current_price"),
            "unified_score": ((context.get("market") or {}).get("scores") or {}).get("unified"),
            "news_count": ((context.get("news") or {}).get("count")),
            "open_campaigns": ((context.get("market") or {}).get("account") or {}).get("open_campaigns"),
            "active_watch_session_id": (active_watch.get("session") or {}).get("session_id") if active_watch.get("active") else None,
        },
        "failed_agents": failed_agents,
        "agents_reporting": f"{6 - len(failed_agents)}/6",
        "risk_reward": risk_reward,
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

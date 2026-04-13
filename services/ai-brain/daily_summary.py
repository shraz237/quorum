"""Daily end-of-day summary — fires at 22:00 UTC (midnight Poland).

Sends a comprehensive Telegram report covering the full trading day:
  - P/L for both personas (main + scalper)
  - Every trade opened/closed today with reasoning
  - Win rate, best/worst trade
  - AI cost for the day
  - Open positions going into overnight
  - Key news that moved the market
  - Opus's overnight outlook

Uses Sonnet to generate the narrative so the summary reads like a
human analyst's daily wrap, not just a table of numbers.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic

from shared.config import settings
from shared.account_manager import recompute_account_state
from shared.models.base import SessionLocal
from shared.models.campaigns import Campaign
from shared.models.knowledge import KnowledgeSummary
from shared.models.llm_usage import LlmUsage
from shared.redis_streams import publish
from shared.llm_usage import record_anthropic_call

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
STREAM_POSITION = "position.event"
SUMMARY_HOUR_UTC = 22  # midnight Poland (CEST)


def _get_todays_campaigns(persona: str) -> list[dict]:
    today_start = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as session:
        rows = (
            session.query(Campaign)
            .filter(
                Campaign.persona == persona,
                Campaign.opened_at >= today_start,
            )
            .order_by(Campaign.opened_at)
            .all()
        )
        result = []
        for c in rows:
            entry_snap = c.entry_snapshot or {}
            result.append({
                "id": c.id,
                "side": c.side,
                "status": c.status,
                "opened_at": c.opened_at.isoformat() if c.opened_at else None,
                "closed_at": c.closed_at.isoformat() if c.closed_at else None,
                "realized_pnl": round(float(c.realized_pnl or 0), 2),
                "entry_price": entry_snap.get("price"),
                "entry_reason": (entry_snap.get("ai_recommendation") or {}).get("analysis_text", "")[:500],
                "notes": (c.notes or "")[:300],
            })
        return result


def _get_todays_llm_cost() -> float:
    today_start = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        with SessionLocal() as session:
            rows = (
                session.query(LlmUsage)
                .filter(LlmUsage.ts >= today_start)
                .all()
            )
            return round(sum(r.estimated_cost_usd or 0 for r in rows), 2)
    except Exception:
        return 0.0


def _get_todays_news() -> list[dict]:
    today_start = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as session:
        rows = (
            session.query(KnowledgeSummary)
            .filter(KnowledgeSummary.timestamp >= today_start)
            .order_by(KnowledgeSummary.timestamp.desc())
            .limit(5)
            .all()
        )
        return [
            {
                "ts": r.timestamp.isoformat(),
                "summary": (r.summary or "")[:300],
                "sentiment": r.sentiment_label,
            }
            for r in rows
        ]


def _get_open_positions() -> list[dict]:
    with SessionLocal() as session:
        rows = (
            session.query(Campaign)
            .filter(Campaign.status == "open")
            .all()
        )
        return [
            {
                "id": c.id,
                "persona": c.persona,
                "side": c.side,
                "take_profit": c.take_profit,
                "stop_loss": c.stop_loss,
            }
            for r in rows
            for c in [r]
        ]


def _generate_narrative(context: dict) -> str:
    """Ask Sonnet to write the daily wrap narrative."""
    client = Anthropic(api_key=settings.anthropic_api_key)

    system = (
        "You are a WTI crude oil trading desk analyst writing the end-of-day summary. "
        "Be direct, specific, cite price levels and P/L numbers. Cover:\n"
        "1. Overall day performance (P/L, win rate)\n"
        "2. Best and worst trade with brief reasoning why\n"
        "3. Key market events that drove price action\n"
        "4. Open positions going into overnight — risk assessment\n"
        "5. One-line outlook for tomorrow\n\n"
        "Keep it under 1500 chars. Always reply in English. "
        "Format for Telegram (no tables, no ### headers, use bullet points)."
    )

    user_prompt = f"## End of Day Data\n{json.dumps(context, indent=2, default=str)}\n\nWrite the daily wrap."

    t0 = time.time()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=800,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        record_anthropic_call(
            call_site="daily_summary.sonnet",
            model=MODEL,
            usage=response.usage,
            duration_ms=(time.time() - t0) * 1000,
        )
        return response.content[0].text.strip()
    except Exception:
        logger.exception("Daily summary narrative generation failed")
        return ""


def _publish_daily_summary() -> None:
    main_acc = recompute_account_state("main")
    scalper_acc = recompute_account_state("scalper")
    main_trades = _get_todays_campaigns("main")
    scalper_trades = _get_todays_campaigns("scalper")
    llm_cost = _get_todays_llm_cost()
    news = _get_todays_news()
    open_pos = _get_open_positions()

    main_realized = sum(t["realized_pnl"] for t in main_trades if t["status"] != "open")
    scalper_realized = sum(t["realized_pnl"] for t in scalper_trades if t["status"] != "open")
    main_wins = sum(1 for t in main_trades if t["status"] != "open" and t["realized_pnl"] > 0)
    scalper_wins = sum(1 for t in scalper_trades if t["status"] != "open" and t["realized_pnl"] > 0)
    main_closed = sum(1 for t in main_trades if t["status"] != "open")
    scalper_closed = sum(1 for t in scalper_trades if t["status"] != "open")

    context = {
        "date": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
        "main": {
            "equity": main_acc.get("equity"),
            "drawdown_pct": main_acc.get("account_drawdown_pct"),
            "realized_today": main_realized,
            "trades_closed": main_closed,
            "wins": main_wins,
            "unrealized": main_acc.get("unrealised_pnl"),
            "trades": main_trades,
        },
        "scalper": {
            "equity": scalper_acc.get("equity"),
            "drawdown_pct": scalper_acc.get("account_drawdown_pct"),
            "realized_today": scalper_realized,
            "trades_closed": scalper_closed,
            "wins": scalper_wins,
            "unrealized": scalper_acc.get("unrealised_pnl"),
            "trades": scalper_trades,
        },
        "llm_cost_today": llm_cost,
        "open_positions": open_pos,
        "key_news": news,
    }

    # Generate AI narrative
    narrative = _generate_narrative(context)

    # Build the message
    main_pnl = main_realized + (main_acc.get("unrealised_pnl") or 0)
    scalper_pnl = scalper_realized + (scalper_acc.get("unrealised_pnl") or 0)
    total_pnl = main_pnl + scalper_pnl

    header = (
        f"*Daily Summary — {context['date']}*\n\n"
        f"*Main:* eq ${main_acc.get('equity', 0):,.0f} | "
        f"realized {'+' if main_realized >= 0 else ''}${main_realized:,.0f} | "
        f"{main_closed} trades ({main_wins}W) | "
        f"dd {main_acc.get('account_drawdown_pct', 0):+.1f}%\n"
        f"*Scalper:* eq ${scalper_acc.get('equity', 0):,.0f} | "
        f"realized {'+' if scalper_realized >= 0 else ''}${scalper_realized:,.0f} | "
        f"{scalper_closed} trades ({scalper_wins}W) | "
        f"dd {scalper_acc.get('account_drawdown_pct', 0):+.1f}%\n"
        f"*Combined:* {'+' if total_pnl >= 0 else ''}${total_pnl:,.0f} | "
        f"AI cost: ${llm_cost:.2f}\n"
        f"Open overnight: {len(open_pos)} position(s)"
    )

    full_message = header
    if narrative:
        full_message += f"\n\n{narrative}"

    logger.info("Daily summary:\n%s", full_message)

    try:
        publish(STREAM_POSITION, {
            "type": "heartbeat_action",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "campaign_id": 0,
            "action": "daily_summary",
            "side": "",
            "reason": full_message,
        })
    except Exception:
        logger.exception("Failed to publish daily summary")


def run_daily_summary_loop() -> None:
    """Background loop — fires once per day at SUMMARY_HOUR_UTC."""
    logger.info("Daily summary worker started (fires at %02d:00 UTC)", SUMMARY_HOUR_UTC)
    time.sleep(90)

    last_fired_date = None

    while True:
        now = datetime.now(tz=timezone.utc)
        today = now.date()

        if now.hour >= SUMMARY_HOUR_UTC and last_fired_date != today:
            # Skip weekends
            if now.weekday() not in (5, 6):
                try:
                    _publish_daily_summary()
                    last_fired_date = today
                except Exception:
                    logger.exception("Daily summary failed")
            else:
                last_fired_date = today  # don't retry on weekends

        time.sleep(60)

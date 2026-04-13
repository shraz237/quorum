"""Main Trader Brain — what is the main persona thinking right now?

Shows the current state of the main trader's decision pipeline in one
glanceable snapshot, similar to how Scalp Brain works for the scalper.

Aggregates:
  - Latest AI recommendation (Opus verdict + confidence + reasoning)
  - Entry gate status (would it open a campaign right now? why/why not?)
  - Range bias (where in the 30-day range)
  - Latest scores (unified, technical, sentiment, fundamental, shipping)
  - Open campaign status (if any)
  - Last heartbeat decision + reasoning
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from shared.models.base import SessionLocal
from shared.models.campaigns import Campaign
from shared.models.heartbeat_runs import HeartbeatRun
from shared.models.signals import AIRecommendation, AnalysisScore
from shared.position_manager import get_current_price, list_open_campaigns
from shared.account_manager import recompute_account_state

logger = logging.getLogger(__name__)

_CACHE: dict | None = None
_CACHE_TS: float = 0.0
_CACHE_TTL = 15  # 15s cache
_LOCK = threading.Lock()


def _get_latest_recommendation() -> dict | None:
    try:
        with SessionLocal() as session:
            rec = (
                session.query(AIRecommendation)
                .order_by(AIRecommendation.timestamp.desc())
                .first()
            )
            if rec is None:
                return None
            return {
                "action": rec.action,
                "confidence": rec.confidence,
                "unified_score": rec.unified_score,
                "opus_override_score": rec.opus_override_score,
                "entry_price": rec.entry_price,
                "stop_loss": rec.stop_loss,
                "take_profit": rec.take_profit,
                "analysis_text": (rec.analysis_text or "")[:1500],
                "base_scenario": (rec.base_scenario or "")[:500],
                "alt_scenario": (rec.alt_scenario or "")[:500],
                "risk_factors": rec.risk_factors,
                "timestamp": rec.timestamp.isoformat() if rec.timestamp else None,
            }
    except Exception:
        logger.exception("main_brain: recommendation fetch failed")
        return None


def _get_latest_scores() -> dict | None:
    try:
        with SessionLocal() as session:
            row = (
                session.query(AnalysisScore)
                .order_by(AnalysisScore.timestamp.desc())
                .first()
            )
            if row is None:
                return None
            return {
                "unified": row.unified_score,
                "technical": row.technical_score,
                "fundamental": row.fundamental_score,
                "sentiment": row.sentiment_score,
                "shipping": row.shipping_score,
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            }
    except Exception:
        logger.exception("main_brain: scores fetch failed")
        return None


def _get_entry_gates(action: str | None, scores: dict | None) -> list[dict]:
    """Evaluate every entry gate and return their status."""
    gates = []

    # Gate 0: Market hours
    try:
        from shared.market_hours import is_market_open
        market_open = is_market_open()
        gates.append({
            "name": "Market hours",
            "ok": market_open,
            "detail": "open" if market_open else "CLOSED (weekend)",
        })
    except Exception:
        gates.append({"name": "Market hours", "ok": True, "detail": "check failed"})

    # Gate 1: Range bias
    try:
        from shared.range_bias import compute_range_bias
        rb = compute_range_bias()
        if rb.get("error"):
            gates.append({"name": "Range bias", "ok": True, "detail": rb["error"]})
        else:
            side = "LONG" if action in ("BUY", "LONG") else "SHORT" if action in ("SELL", "SHORT") else None
            blocked = False
            if side == "LONG" and rb.get("should_refuse_long"):
                blocked = True
            if side == "SHORT" and rb.get("should_refuse_short"):
                blocked = True
            gates.append({
                "name": "Range bias",
                "ok": not blocked,
                "detail": f"{rb['position_pct']:.0f}% of range · bias={rb['bias']}" + (" · BLOCKED" if blocked else ""),
                "position_pct": rb.get("position_pct"),
                "bias": rb.get("bias"),
                "range_high": rb.get("range_high"),
                "range_low": rb.get("range_low"),
            })
    except Exception:
        gates.append({"name": "Range bias", "ok": True, "detail": "check failed"})

    # Gate 2: Technical score
    tech = (scores or {}).get("technical")
    if tech is not None:
        min_tech = 5.0
        if action in ("BUY", "LONG"):
            ok = tech >= min_tech
            gates.append({
                "name": "Tech score",
                "ok": ok,
                "detail": f"tech={tech:.1f}" + (f" · BLOCKED (< {min_tech})" if not ok else " · OK for LONG"),
            })
        elif action in ("SELL", "SHORT"):
            ok = tech <= -min_tech
            gates.append({
                "name": "Tech score",
                "ok": ok,
                "detail": f"tech={tech:.1f}" + (f" · BLOCKED (> {-min_tech})" if not ok else " · OK for SHORT"),
            })
        else:
            gates.append({"name": "Tech score", "ok": True, "detail": f"tech={tech:.1f} · no side to check"})
    else:
        gates.append({"name": "Tech score", "ok": True, "detail": "no score available"})

    # Gate 3: Loss cooldown
    try:
        with SessionLocal() as session:
            last_loss = (
                session.query(Campaign)
                .filter(Campaign.persona == "main", Campaign.status != "open", Campaign.realized_pnl < 0)
                .order_by(Campaign.closed_at.desc())
                .first()
            )
            if last_loss and last_loss.closed_at:
                age_min = (datetime.now(tz=timezone.utc) - last_loss.closed_at).total_seconds() / 60
                ok = age_min >= 30
                gates.append({
                    "name": "Loss cooldown",
                    "ok": ok,
                    "detail": f"last loss {age_min:.0f} min ago" + (" · BLOCKED (< 30 min)" if not ok else " · OK"),
                })
            else:
                gates.append({"name": "Loss cooldown", "ok": True, "detail": "no recent losses"})
    except Exception:
        gates.append({"name": "Loss cooldown", "ok": True, "detail": "check failed"})

    # Gate 4: Data staleness
    price = get_current_price()
    gates.append({
        "name": "Price data",
        "ok": price is not None,
        "detail": f"${price:.3f}" if price is not None else "STALE — no fresh price",
    })

    return gates


def _get_last_heartbeat() -> dict | None:
    try:
        with SessionLocal() as session:
            row = (
                session.query(HeartbeatRun)
                .filter(HeartbeatRun.campaign_id.isnot(None))
                .order_by(HeartbeatRun.ran_at.desc())
                .first()
            )
            if row is None:
                return None
            return {
                "decision": row.decision,
                "reason": (row.reason or "")[:500],
                "campaign_id": row.campaign_id,
                "ran_at": row.ran_at.isoformat() if row.ran_at else None,
                "executed": row.executed,
            }
    except Exception:
        return None


def _compute_main_brain() -> dict:
    current_price = get_current_price()
    recommendation = _get_latest_recommendation()
    scores = _get_latest_scores()
    action = (recommendation or {}).get("action")
    confidence = (recommendation or {}).get("confidence")

    # What would the main trader do right now?
    gates = _get_entry_gates(action, scores)
    gates_passed = sum(1 for g in gates if g["ok"])
    gates_total = len(gates)
    all_gates_pass = gates_passed == gates_total

    # Open campaign
    open_camps = list_open_campaigns(persona="main")
    has_open = len(open_camps) > 0

    # Determine the "brain verdict"
    if has_open:
        verdict = "MANAGING"
        verdict_detail = f"Managing {len(open_camps)} open campaign(s)"
    elif action in ("BUY", "SELL") and all_gates_pass:
        verdict = action
        verdict_detail = f"Would open {action} ({confidence:.0%} confidence) — all gates pass"
    elif action in ("BUY", "SELL") and not all_gates_pass:
        failed = [g["name"] for g in gates if not g["ok"]]
        verdict = "BLOCKED"
        verdict_detail = f"Wants to {action} but blocked by: {', '.join(failed)}"
    elif action == "HOLD":
        verdict = "HOLD"
        verdict_detail = "Holding — no strong signal"
    else:
        verdict = "WAIT"
        verdict_detail = "Waiting for a signal"

    # Account state
    account = recompute_account_state("main")

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "current_price": current_price,
        "verdict": verdict,
        "verdict_detail": verdict_detail,
        "recommendation": recommendation,
        "scores": scores,
        "gates": gates,
        "gates_passed": gates_passed,
        "gates_total": gates_total,
        "has_open_campaign": has_open,
        "open_campaigns_count": len(open_camps),
        "last_heartbeat": _get_last_heartbeat(),
        "account": {
            "equity": account.get("equity"),
            "drawdown_pct": account.get("account_drawdown_pct"),
            "free_margin": account.get("free_margin"),
        },
    }


def get_main_brain() -> dict:
    global _CACHE, _CACHE_TS
    now = time.time()
    with _LOCK:
        if _CACHE is not None and (now - _CACHE_TS) < _CACHE_TTL:
            return {**_CACHE, "cache_age_seconds": round(now - _CACHE_TS, 1)}
    result = _compute_main_brain()
    with _LOCK:
        _CACHE = result
        _CACHE_TS = now
    return {**result, "cache_age_seconds": 0.0}

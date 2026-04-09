"""Thesis helpers — shared across ai-brain, dashboard, and chat.

This module contains the pure logic for:
  - Creating theses (with validation)
  - Evaluating whether a pending thesis should trigger NOW
  - Resolving the outcome of a triggered thesis

Background workers in ai-brain call eval_trigger() and eval_resolution()
on a schedule. Dashboard endpoints and chat tools call create_thesis()
and cancel_thesis(). All three layers use this same module so the
evaluation logic is shared.

Design principle: side-effect-free helpers where possible. The only
side effects are DB writes (creating rows, updating status). Redis +
Telegram publishing lives in the ai-brain workers, not here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.models.base import SessionLocal
from shared.models.knowledge import KnowledgeSummary
from shared.models.ohlcv import OHLCV
from shared.models.signals import AnalysisScore
from shared.models.theses import Thesis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


VALID_TRIGGER_TYPES = {
    "price_cross_above",
    "price_cross_below",
    "score_above",
    "score_below",
    "time_elapsed",
    "news_keyword",
    "scalp_brain_state",
    "manual",
}

VALID_ACTIONS = {"LONG", "SHORT", "CLOSE_EXISTING", "WATCH", "NONE"}
VALID_DOMAINS = {"campaign", "scalp"}
VALID_OUTCOME_MODES = {"fixed_window", "tp_or_sl_first"}


def create_thesis(
    *,
    created_by: str,
    title: str,
    thesis_text: str,
    trigger_type: str,
    trigger_params: dict,
    domain: str = "campaign",
    reasoning: str | None = None,
    context_snapshot: dict | None = None,
    planned_action: str = "WATCH",
    planned_entry: float | None = None,
    planned_stop_loss: float | None = None,
    planned_take_profit: float | None = None,
    planned_size_margin: float | None = None,
    outcome_mode: str = "tp_or_sl_first",
    resolution_window_minutes: int = 240,
    expires_at: datetime | None = None,
) -> int | None:
    """Insert a new thesis row. Returns the new id or None on validation error.

    Called from: chat create_thesis tool, dashboard POST /api/theses form,
    heartbeat.py propose_theses path, plugin_scalp_brain auto-propose.
    """
    if trigger_type not in VALID_TRIGGER_TYPES:
        logger.warning("create_thesis: invalid trigger_type %r", trigger_type)
        return None
    if planned_action not in VALID_ACTIONS:
        logger.warning("create_thesis: invalid planned_action %r", planned_action)
        return None
    if domain not in VALID_DOMAINS:
        logger.warning("create_thesis: invalid domain %r", domain)
        return None
    if outcome_mode not in VALID_OUTCOME_MODES:
        logger.warning("create_thesis: invalid outcome_mode %r", outcome_mode)
        return None
    if not isinstance(trigger_params, dict):
        logger.warning("create_thesis: trigger_params must be a dict")
        return None

    try:
        with SessionLocal() as session:
            row = Thesis(
                created_at=datetime.now(tz=timezone.utc),
                created_by=created_by[:32],
                domain=domain,
                title=title[:200],
                thesis_text=thesis_text,
                reasoning=reasoning,
                context_snapshot=context_snapshot,
                trigger_type=trigger_type,
                trigger_params=trigger_params,
                expires_at=expires_at,
                planned_action=planned_action,
                planned_entry=planned_entry,
                planned_stop_loss=planned_stop_loss,
                planned_take_profit=planned_take_profit,
                planned_size_margin=planned_size_margin,
                outcome_mode=outcome_mode,
                resolution_window_minutes=resolution_window_minutes,
                status="pending",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            logger.info(
                "Created thesis #%s domain=%s trigger=%s by=%s title=%r",
                row.id, domain, trigger_type, created_by, title[:60],
            )
            return int(row.id)
    except Exception:
        logger.exception("create_thesis DB insert failed")
        return None


def cancel_thesis(thesis_id: int, reason: str = "user_cancelled") -> bool:
    try:
        with SessionLocal() as session:
            row = session.query(Thesis).filter(Thesis.id == thesis_id).first()
            if row is None or row.status != "pending":
                return False
            row.status = "cancelled"
            row.outcome_notes = reason[:500]
            session.commit()
            return True
    except Exception:
        logger.exception("cancel_thesis failed for #%s", thesis_id)
        return False


# ---------------------------------------------------------------------------
# Trigger evaluation — called by ai-brain theses_watcher every ~30s
# ---------------------------------------------------------------------------


def _get_current_price() -> float | None:
    try:
        with SessionLocal() as session:
            row = (
                session.query(OHLCV)
                .filter(OHLCV.timeframe == "1min", OHLCV.source == "twelve")
                .order_by(OHLCV.timestamp.desc())
                .first()
            )
            return float(row.close) if row else None
    except Exception:
        logger.exception("theses: failed to read current price")
        return None


def _get_latest_unified_score() -> float | None:
    try:
        with SessionLocal() as session:
            row = (
                session.query(AnalysisScore)
                .order_by(AnalysisScore.timestamp.desc())
                .first()
            )
            return float(row.unified_score) if row and row.unified_score is not None else None
    except Exception:
        logger.exception("theses: failed to read unified score")
        return None


def _check_news_keyword(keywords: list[str], since_minutes: int = 30) -> dict | None:
    """Return a matching news summary if any recent headline contains any keyword."""
    if not keywords:
        return None
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=since_minutes)
    needles = [k.lower() for k in keywords if k]
    try:
        with SessionLocal() as session:
            rows = (
                session.query(KnowledgeSummary)
                .filter(KnowledgeSummary.timestamp >= cutoff)
                .order_by(KnowledgeSummary.timestamp.desc())
                .limit(20)
                .all()
            )
            for r in rows:
                hay = (r.summary or "").lower() + " " + (r.key_events or "").lower()
                for needle in needles:
                    if needle in hay:
                        return {
                            "ts": r.timestamp.isoformat(),
                            "summary": (r.summary or "")[:400],
                            "matched_keyword": needle,
                        }
    except Exception:
        logger.exception("theses: news keyword check failed")
    return None


def _get_scalp_brain_verdict() -> str | None:
    """Read the scalp brain's current verdict from its in-memory cache.

    Imported lazily so this helper doesn't pull dashboard plugins when
    called from ai-brain. Returns None on any failure.
    """
    try:
        # Only the dashboard service has plugin_scalp_brain in its PYTHONPATH.
        # ai-brain theses_watcher can't call it directly — it has to hit
        # the /api/scalp-brain HTTP endpoint if it wants to check. For
        # now we leave scalp_brain_state triggers unimplemented in the
        # ai-brain watcher (returns None = never triggers) and the
        # scalp brain can self-trigger on its own state changes.
        import plugin_scalp_brain  # noqa: F401
        data = plugin_scalp_brain.get_scalp_brain()
        return data.get("verdict") if isinstance(data, dict) else None
    except Exception:
        return None


def evaluate_trigger(thesis: Thesis) -> tuple[bool, dict]:
    """Return (fired, snapshot) for a single pending thesis.

    snapshot is a small dict recording what we saw at evaluation time —
    stored on the row if fired so the outcome worker can compare against
    it later.
    """
    if thesis.status != "pending":
        return False, {}

    # Check expiry first — an expired thesis is not a trigger
    if thesis.expires_at is not None and datetime.now(tz=timezone.utc) >= thesis.expires_at:
        return False, {"expired": True}

    params = thesis.trigger_params or {}
    tt = thesis.trigger_type

    if tt in ("price_cross_above", "price_cross_below"):
        target = params.get("price")
        if target is None:
            return False, {"error": "missing price"}
        current_price = _get_current_price()
        if current_price is None:
            return False, {"error": "no current price"}
        fired = (
            current_price >= float(target) if tt == "price_cross_above"
            else current_price <= float(target)
        )
        return fired, {
            "current_price": current_price,
            "target_price": float(target),
        }

    if tt in ("score_above", "score_below"):
        target = params.get("score")
        score_key = params.get("score_key", "unified")  # future: technical, sentiment, etc.
        if target is None:
            return False, {"error": "missing score"}
        # For v1 we only support the unified score
        value = _get_latest_unified_score() if score_key == "unified" else None
        if value is None:
            return False, {"error": "no score"}
        fired = (
            value >= float(target) if tt == "score_above"
            else value <= float(target)
        )
        return fired, {"current_score": value, "target_score": float(target), "score_key": score_key}

    if tt == "time_elapsed":
        minutes = int(params.get("minutes", 0))
        if minutes <= 0:
            return False, {"error": "missing minutes"}
        elapsed = (datetime.now(tz=timezone.utc) - thesis.created_at).total_seconds() / 60.0
        fired = elapsed >= minutes
        return fired, {"elapsed_minutes": round(elapsed, 1), "threshold_minutes": minutes}

    if tt == "news_keyword":
        keywords = params.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        match = _check_news_keyword(keywords)
        if match is None:
            return False, {"checked_keywords": keywords}
        return True, {"checked_keywords": keywords, "match": match}

    if tt == "scalp_brain_state":
        target_state = params.get("state")  # e.g. "LONG" or "SHORT"
        if target_state is None:
            return False, {"error": "missing state"}
        current = _get_scalp_brain_verdict()
        if current is None:
            return False, {"note": "scalp brain unavailable"}
        fired = current == target_state
        return fired, {"current_state": current, "target_state": target_state}

    if tt == "manual":
        # Manual triggers are never fired automatically — user fires them
        # via the dashboard or chat. We still return False here.
        return False, {"note": "manual trigger — user-fired only"}

    return False, {"error": f"unknown trigger_type {tt}"}


def mark_triggered(thesis_id: int, triggered_snapshot: dict) -> bool:
    """Transition a pending thesis to triggered state."""
    try:
        with SessionLocal() as session:
            row = session.query(Thesis).filter(Thesis.id == thesis_id).first()
            if row is None or row.status != "pending":
                return False
            row.status = "triggered"
            row.triggered_at = datetime.now(tz=timezone.utc)
            row.triggered_price = _get_current_price()
            row.triggered_snapshot = triggered_snapshot
            session.commit()
            return True
    except Exception:
        logger.exception("mark_triggered failed for #%s", thesis_id)
        return False


def mark_expired(thesis_id: int) -> bool:
    try:
        with SessionLocal() as session:
            row = session.query(Thesis).filter(Thesis.id == thesis_id).first()
            if row is None or row.status != "pending":
                return False
            row.status = "expired"
            row.resolved_at = datetime.now(tz=timezone.utc)
            session.commit()
            return True
    except Exception:
        logger.exception("mark_expired failed for #%s", thesis_id)
        return False


# ---------------------------------------------------------------------------
# Outcome resolution — called by ai-brain theses_resolver every ~5 min
# ---------------------------------------------------------------------------


def _price_window_since(since: datetime) -> tuple[float, float] | None:
    """Return (max_high, min_low) of 1-min WTI bars since `since`."""
    try:
        with SessionLocal() as session:
            rows = (
                session.query(OHLCV)
                .filter(
                    OHLCV.timeframe == "1min",
                    OHLCV.source == "twelve",
                    OHLCV.timestamp >= since,
                )
                .all()
            )
            if not rows:
                return None
            return max(r.high for r in rows), min(r.low for r in rows)
    except Exception:
        logger.exception("theses: price window read failed")
        return None


def evaluate_resolution(thesis: Thesis) -> tuple[bool, dict] | None:
    """For a triggered thesis, determine whether its outcome can be resolved
    and return the outcome payload. Returns None if not ready yet.
    """
    if thesis.status != "triggered" or thesis.triggered_at is None:
        return None

    now = datetime.now(tz=timezone.utc)
    mode = thesis.outcome_mode or "tp_or_sl_first"

    # Common: what did price do since triggered_at?
    window = _price_window_since(thesis.triggered_at)
    current_price = _get_current_price()
    if window is None or current_price is None:
        return None  # no data yet

    max_high, min_low = window
    trigger_price = thesis.triggered_price or current_price

    # Max favorable / adverse excursion relative to the planned direction
    if thesis.planned_action == "LONG":
        max_favorable = max_high - trigger_price
        max_adverse = trigger_price - min_low
    elif thesis.planned_action == "SHORT":
        max_favorable = trigger_price - min_low
        max_adverse = max_high - trigger_price
    else:
        max_favorable = None
        max_adverse = None

    if mode == "tp_or_sl_first":
        # Resolved when the window shows price has touched TP or SL,
        # whichever came first. We can't tell WHICH came first from the
        # aggregate max/min — so we conservatively resolve only when
        # ONE of TP/SL has been touched but not both. If both touched,
        # we mark partial and note the ambiguity.
        tp = thesis.planned_take_profit
        sl = thesis.planned_stop_loss
        if tp is None or sl is None:
            # Without both levels we fall back to the fixed-window mode
            pass
        else:
            if thesis.planned_action == "LONG":
                tp_hit = max_high >= tp
                sl_hit = min_low <= sl
            elif thesis.planned_action == "SHORT":
                tp_hit = min_low <= tp
                sl_hit = max_high >= sl
            else:
                tp_hit = sl_hit = False

            if tp_hit and not sl_hit:
                hypo_pnl = _hypothetical_pnl(thesis, trigger_price, tp)
                return True, {
                    "outcome": "correct",
                    "notes": "TP hit before SL",
                    "outcome_price": current_price,
                    "max_favorable_excursion": max_favorable,
                    "max_adverse_excursion": max_adverse,
                    "hypothetical_pnl_usd": hypo_pnl,
                }
            if sl_hit and not tp_hit:
                hypo_pnl = _hypothetical_pnl(thesis, trigger_price, sl)
                return True, {
                    "outcome": "wrong",
                    "notes": "SL hit before TP",
                    "outcome_price": current_price,
                    "max_favorable_excursion": max_favorable,
                    "max_adverse_excursion": max_adverse,
                    "hypothetical_pnl_usd": hypo_pnl,
                }
            if tp_hit and sl_hit:
                return True, {
                    "outcome": "partial",
                    "notes": "Both TP and SL touched in the window — ambiguous which came first",
                    "outcome_price": current_price,
                    "max_favorable_excursion": max_favorable,
                    "max_adverse_excursion": max_adverse,
                    "hypothetical_pnl_usd": None,
                }
            # Neither hit yet — fall through to window-expiry check

    # fixed_window or fall-through from tp_or_sl_first
    window_end = thesis.triggered_at + timedelta(minutes=thesis.resolution_window_minutes or 240)
    if now < window_end:
        return None  # still waiting

    # Window elapsed with no TP/SL hit — mark unresolved with the current price
    hypo_pnl = _hypothetical_pnl(thesis, trigger_price, current_price)
    return True, {
        "outcome": "unresolved" if mode == "tp_or_sl_first" else "correct" if hypo_pnl and hypo_pnl > 0 else "wrong",
        "notes": f"{thesis.resolution_window_minutes}-min window elapsed; neither TP nor SL hit",
        "outcome_price": current_price,
        "max_favorable_excursion": max_favorable,
        "max_adverse_excursion": max_adverse,
        "hypothetical_pnl_usd": hypo_pnl,
    }


def _hypothetical_pnl(thesis: Thesis, entry_price: float, exit_price: float) -> float | None:
    """Compute the hypothetical P/L in USD if the plan had been executed
    at entry_price and closed at exit_price.

    Uses the planned_size_margin * leverage * 10 (WTI CFD: 1 lot = 100 bbl,
    10x leverage, so 1 USD margin controls 1 USD of barrel exposure after
    leverage — we compute barrels directly from margin).
    """
    if thesis.planned_action not in ("LONG", "SHORT"):
        return None
    margin = thesis.planned_size_margin or 3000.0  # sensible default
    leverage = 10  # matches account_manager.DEFAULT_LEVERAGE
    if entry_price <= 0:
        return None
    # Barrels exposed = (margin * leverage) / entry_price
    barrels = (margin * leverage) / entry_price
    if thesis.planned_action == "LONG":
        return round((exit_price - entry_price) * barrels, 2)
    else:
        return round((entry_price - exit_price) * barrels, 2)


def mark_resolved(thesis_id: int, payload: dict) -> bool:
    try:
        with SessionLocal() as session:
            row = session.query(Thesis).filter(Thesis.id == thesis_id).first()
            if row is None or row.status != "triggered":
                return False
            row.status = "resolved"
            row.resolved_at = datetime.now(tz=timezone.utc)
            row.outcome = payload.get("outcome")
            row.outcome_notes = (payload.get("notes") or "")[:2000]
            row.outcome_price = payload.get("outcome_price")
            row.outcome_hypothetical_pnl_usd = payload.get("hypothetical_pnl_usd")
            row.outcome_max_favorable_excursion = payload.get("max_favorable_excursion")
            row.outcome_max_adverse_excursion = payload.get("max_adverse_excursion")
            session.commit()
            return True
    except Exception:
        logger.exception("mark_resolved failed for #%s", thesis_id)
        return False


# ---------------------------------------------------------------------------
# Read helpers — used by dashboard plugin
# ---------------------------------------------------------------------------


def list_theses(
    domain: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    with SessionLocal() as session:
        q = session.query(Thesis).order_by(Thesis.created_at.desc())
        if domain is not None:
            q = q.filter(Thesis.domain == domain)
        if status is not None:
            q = q.filter(Thesis.status == status)
        q = q.limit(limit)
        rows = q.all()
        return [_thesis_to_dict(r) for r in rows]


def _thesis_to_dict(row: Thesis) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": row.created_by,
        "domain": row.domain,
        "title": row.title,
        "thesis_text": row.thesis_text,
        "reasoning": row.reasoning,
        "context_snapshot": row.context_snapshot,
        "trigger_type": row.trigger_type,
        "trigger_params": row.trigger_params,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "planned_action": row.planned_action,
        "planned_entry": row.planned_entry,
        "planned_stop_loss": row.planned_stop_loss,
        "planned_take_profit": row.planned_take_profit,
        "planned_size_margin": row.planned_size_margin,
        "outcome_mode": row.outcome_mode,
        "resolution_window_minutes": row.resolution_window_minutes,
        "status": row.status,
        "triggered_at": row.triggered_at.isoformat() if row.triggered_at else None,
        "triggered_price": row.triggered_price,
        "triggered_snapshot": row.triggered_snapshot,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "outcome": row.outcome,
        "outcome_notes": row.outcome_notes,
        "outcome_price": row.outcome_price,
        "outcome_hypothetical_pnl_usd": row.outcome_hypothetical_pnl_usd,
        "outcome_max_favorable_excursion": row.outcome_max_favorable_excursion,
        "outcome_max_adverse_excursion": row.outcome_max_adverse_excursion,
    }


def domain_stats(domain: str, days: int = 30) -> dict:
    """Rollup for the dashboard panel."""
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    with SessionLocal() as session:
        rows = (
            session.query(Thesis)
            .filter(Thesis.domain == domain, Thesis.created_at >= since)
            .all()
        )
        total = len(rows)
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        resolved = [r for r in rows if r.status == "resolved"]
        correct = sum(1 for r in resolved if r.outcome == "correct")
        wrong = sum(1 for r in resolved if r.outcome == "wrong")
        partial = sum(1 for r in resolved if r.outcome == "partial")
        unresolved = sum(1 for r in resolved if r.outcome == "unresolved")
        hit_rate = (correct / (correct + wrong)) if (correct + wrong) > 0 else None
        hypothetical_pnl = sum(
            (r.outcome_hypothetical_pnl_usd or 0.0) for r in resolved
        )
        return {
            "domain": domain,
            "days": days,
            "total_created": total,
            "by_status": by_status,
            "resolved": {
                "correct": correct,
                "wrong": wrong,
                "partial": partial,
                "unresolved": unresolved,
                "hit_rate": round(hit_rate, 3) if hit_rate is not None else None,
                "hypothetical_pnl_usd": round(hypothetical_pnl, 2),
            },
        }

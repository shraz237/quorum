"""Lightweight trade journal snapshot builder — usable from any service.

Captures a compact dict of the current market + account state for storage
in the campaigns.entry_snapshot / exit_snapshot JSONB columns. Unlike the
richer dashboard-side plugin_trade_journal.capture_snapshot (which can pull
from plugin_conviction, plugin_anomalies, etc.), this version uses ONLY
shared DB models so it can be called from:

  - dashboard chat_tools and API endpoints (manual open/close)
  - ai-brain auto-opens (no access to dashboard plugins)
  - position_manager itself (always-on hook)

The dashboard can still layer its richer snapshot on top via
plugin_trade_journal, but this guarantees that AT LEAST a basic snapshot
is attached to every campaign regardless of which service opened it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc

from shared.models.base import SessionLocal

logger = logging.getLogger(__name__)


def _safe_latest(session, model, order_col, extra_filter=None):
    try:
        q = session.query(model)
        if extra_filter is not None:
            q = q.filter(extra_filter)
        return q.order_by(desc(order_col)).first()
    except Exception:
        logger.exception("trade_snapshot: query for %s failed", model.__name__)
        return None


def build_snapshot(reason: str | None = None) -> dict:
    """Return a compact JSON-serialisable snapshot of current state."""
    from shared.models.ohlcv import OHLCV
    from shared.models.signals import AnalysisScore
    from shared.models.binance_metrics import (
        BinanceFundingRate,
        BinanceOpenInterest,
        BinanceLongShortRatio,
    )

    snap: dict = {
        "captured_at": datetime.now(tz=timezone.utc).isoformat(),
        "reason": reason,
    }

    # Current price
    try:
        from shared.position_manager import get_current_price
        snap["price"] = get_current_price()
    except Exception as exc:
        snap["price_error"] = str(exc)

    # Account state
    try:
        from shared.account_manager import recompute_account_state
        acc = recompute_account_state()
        snap["account"] = {
            "equity": acc.get("equity"),
            "cash": acc.get("cash"),
            "margin_used": acc.get("margin_used"),
            "margin_level_pct": acc.get("margin_level_pct"),
            "drawdown_pct": acc.get("account_drawdown_pct"),
            "realized_pnl_total": acc.get("realized_pnl_total"),
        }
    except Exception as exc:
        snap["account_error"] = str(exc)

    with SessionLocal() as session:
        scores = _safe_latest(session, AnalysisScore, AnalysisScore.timestamp)
        if scores:
            snap["scores"] = {
                "technical": scores.technical_score,
                "fundamental": scores.fundamental_score,
                "sentiment": scores.sentiment_score,
                "shipping": scores.shipping_score,
                "unified": scores.unified_score,
            }

        fr = _safe_latest(session, BinanceFundingRate, BinanceFundingRate.funding_time)
        oi_latest = _safe_latest(session, BinanceOpenInterest, BinanceOpenInterest.timestamp)

        # 24h OI change
        oi_change_pct = None
        if oi_latest:
            try:
                cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
                oi_24h = (
                    session.query(BinanceOpenInterest)
                    .filter(BinanceOpenInterest.timestamp <= cutoff)
                    .order_by(desc(BinanceOpenInterest.timestamp))
                    .first()
                )
                if oi_24h and oi_24h.open_interest:
                    oi_change_pct = round(
                        (oi_latest.open_interest - oi_24h.open_interest)
                        / oi_24h.open_interest * 100, 2,
                    )
            except Exception:
                logger.exception("trade_snapshot: OI 24h change failed")

        def _latest_ratio(rt):
            return (
                session.query(BinanceLongShortRatio)
                .filter(BinanceLongShortRatio.ratio_type == rt)
                .order_by(desc(BinanceLongShortRatio.timestamp))
                .first()
            )

        try:
            top = _latest_ratio("top_position")
            glob = _latest_ratio("global_account")
            taker = _latest_ratio("taker")
        except Exception:
            top = glob = taker = None

        snap["binance"] = {
            "funding_rate_pct": round(fr.funding_rate * 100, 4) if fr else None,
            "open_interest": oi_latest.open_interest if oi_latest else None,
            "open_interest_change_24h_pct": oi_change_pct,
            "top_trader_long_pct": (top.long_pct if top and top.long_pct is not None else None),
            "global_retail_long_pct": (glob.long_pct if glob and glob.long_pct is not None else None),
            "taker_buysell_ratio": taker.long_short_ratio if taker else None,
        }

    # --- REASONING LAYER ---
    # Captures WHY the trade was opened/closed so the user can review
    # the bot's thinking on every trade for learning purposes.

    # Latest AI recommendation (Opus reasoning + levels)
    try:
        from shared.models.signals import AIRecommendation
        with SessionLocal() as session:
            rec = _safe_latest(session, AIRecommendation, AIRecommendation.timestamp)
            if rec:
                snap["ai_recommendation"] = {
                    "action": rec.action,
                    "confidence": rec.confidence,
                    "unified_score": rec.unified_score,
                    "opus_override_score": rec.opus_override_score,
                    "analysis_text": (rec.analysis_text or "")[:2000],
                    "base_scenario": (rec.base_scenario or "")[:500],
                    "alt_scenario": (rec.alt_scenario or "")[:500],
                    "risk_factors": rec.risk_factors,
                    "entry_price": rec.entry_price,
                    "stop_loss": rec.stop_loss,
                    "take_profit": rec.take_profit,
                    "timestamp": rec.timestamp.isoformat() if rec.timestamp else None,
                }
    except Exception:
        logger.exception("trade_snapshot: AI recommendation capture failed")

    # Latest heartbeat decision + reasoning (what Opus was thinking)
    try:
        from shared.models.heartbeat_runs import HeartbeatRun
        with SessionLocal() as session:
            hb = _safe_latest(session, HeartbeatRun, HeartbeatRun.ran_at)
            if hb:
                snap["last_heartbeat"] = {
                    "decision": hb.decision,
                    "reason": (hb.reason or "")[:1000],
                    "ran_at": hb.ran_at.isoformat() if hb.ran_at else None,
                    "campaign_id": hb.campaign_id,
                    "executed": hb.executed,
                }
    except Exception:
        logger.exception("trade_snapshot: heartbeat capture failed")

    # Recent marketfeed headlines (last 3 digests — what the news was)
    try:
        from shared.models.knowledge import KnowledgeSummary
        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
        with SessionLocal() as session:
            news_rows = (
                session.query(KnowledgeSummary)
                .filter(KnowledgeSummary.timestamp >= cutoff)
                .order_by(desc(KnowledgeSummary.timestamp))
                .limit(3)
                .all()
            )
            if news_rows:
                snap["recent_news"] = [
                    {
                        "ts": r.timestamp.isoformat(),
                        "summary": (r.summary or "")[:400],
                        "sentiment": r.sentiment_label,
                        "score": r.sentiment_score,
                    }
                    for r in news_rows
                ]
    except Exception:
        logger.exception("trade_snapshot: news capture failed")

    # Pending theses at the time of the trade
    try:
        from shared.models.theses import Thesis
        with SessionLocal() as session:
            pending = (
                session.query(Thesis)
                .filter(Thesis.status == "pending")
                .filter(~Thesis.created_by.like("smoke%"))
                .order_by(desc(Thesis.created_at))
                .limit(10)
                .all()
            )
            if pending:
                snap["pending_theses"] = [
                    {
                        "id": t.id,
                        "title": t.title,
                        "trigger_type": t.trigger_type,
                        "trigger_params": t.trigger_params,
                        "planned_action": t.planned_action,
                    }
                    for t in pending
                ]
    except Exception:
        logger.exception("trade_snapshot: theses capture failed")

    # Friction config at the time of trade (so journal entries show the
    # spread/slippage/swap assumptions that were in effect)
    try:
        from shared.trading_friction import friction_summary
        snap["friction_config"] = friction_summary()
    except Exception:
        logger.exception("trade_snapshot: friction capture failed")

    return snap


def attach_entry_snapshot(
    campaign_id: int,
    reason: str | None = None,
    extra: dict | None = None,
) -> None:
    """Build and store an entry snapshot on a newly-opened campaign.

    `extra` can carry caller-specific context (e.g. the score event that
    triggered the open, or the chat message that asked for it). It's
    merged into the snapshot so the trade journal shows the full story.
    """
    from shared.models.campaigns import Campaign
    snap = build_snapshot(reason=reason or "campaign_open")
    if extra and isinstance(extra, dict):
        snap["entry_context"] = extra
    try:
        with SessionLocal() as session:
            row = session.query(Campaign).filter(Campaign.id == campaign_id).first()
            if row is None:
                return
            row.entry_snapshot = snap
            session.commit()
    except Exception:
        logger.exception("attach_entry_snapshot failed for campaign %s", campaign_id)


def attach_exit_snapshot(
    campaign_id: int,
    reason: str | None = None,
    extra: dict | None = None,
) -> None:
    """Build and store an exit snapshot on a closed campaign.

    `extra` can carry close-specific context (friction costs breakdown,
    the heartbeat reason that triggered the close, etc.). Merged into
    the snapshot for full journal audit trail.
    """
    from shared.models.campaigns import Campaign
    snap = build_snapshot(reason=reason or "campaign_close")
    if extra and isinstance(extra, dict):
        snap["exit_context"] = extra

    # Also compute max favorable/adverse excursion over the campaign's life
    try:
        with SessionLocal() as session:
            row = session.query(Campaign).filter(Campaign.id == campaign_id).first()
            if row is not None and row.opened_at is not None:
                from shared.models.ohlcv import OHLCV
                bars = (
                    session.query(OHLCV)
                    .filter(
                        OHLCV.source == "twelve",
                        OHLCV.timeframe == "1min",
                        OHLCV.timestamp >= row.opened_at,
                    )
                    .all()
                )
                if bars:
                    max_high = max(b.high for b in bars)
                    min_low = min(b.low for b in bars)
                    entry_snap = row.entry_snapshot or {}
                    entry_price = entry_snap.get("price")
                    if entry_price is not None:
                        if row.side == "LONG":
                            snap["max_favorable_excursion_usd"] = round(max_high - entry_price, 3)
                            snap["max_adverse_excursion_usd"] = round(entry_price - min_low, 3)
                        elif row.side == "SHORT":
                            snap["max_favorable_excursion_usd"] = round(entry_price - min_low, 3)
                            snap["max_adverse_excursion_usd"] = round(max_high - entry_price, 3)
    except Exception:
        logger.exception("attach_exit_snapshot: MFE/MAE computation failed")

    try:
        with SessionLocal() as session:
            row = session.query(Campaign).filter(Campaign.id == campaign_id).first()
            if row is None:
                return
            row.exit_snapshot = snap
            session.commit()
    except Exception:
        logger.exception("attach_exit_snapshot failed for campaign %s", campaign_id)

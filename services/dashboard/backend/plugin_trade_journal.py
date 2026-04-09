"""Trade Journal — automatic campaign context capture + performance stats.

Two responsibilities:

1. capture_snapshot() — builds a compact dict of current dashboard state
   (scores, conviction, funding, OI, positioning, orderbook, CVD, whales,
   recent news). Called from campaign open/close hooks. Stored in the
   campaigns.entry_snapshot / exit_snapshot JSONB columns.

2. get_journal() / get_stats() — reads closed campaigns, computes running
   performance metrics (win rate, profit factor, avg win/loss, Sharpe,
   max drawdown) and returns per-trade entries with their snapshots.

The journal is the feedback loop the trader has been missing. Every
closed campaign goes in, stats accumulate, and over time the trader can
ask: "what's my win rate when conviction > 50 vs < 50?" — and get a
data-driven answer instead of gut feel.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from sqlalchemy import desc

from shared.models.base import SessionLocal
from shared.models.campaigns import Campaign
from shared.models.positions import Position

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def capture_snapshot(reason: str | None = None) -> dict:
    """Return a compact dict of the current dashboard state.

    Pulls minimal data from each surface to keep the snapshot small enough
    to store many of them cheaply. Safe to call from anywhere — failures
    in sub-fetches become keys with error strings rather than raising.
    """
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
        }
    except Exception as exc:
        snap["account_error"] = str(exc)

    # Latest analysis scores
    try:
        from shared.models.signals import AnalysisScore
        with SessionLocal() as session:
            row = (
                session.query(AnalysisScore)
                .order_by(desc(AnalysisScore.timestamp))
                .first()
            )
            if row:
                snap["scores"] = {
                    "technical": row.technical_score,
                    "fundamental": row.fundamental_score,
                    "sentiment": row.sentiment_score,
                    "shipping": row.shipping_score,
                    "unified": row.unified_score,
                }
    except Exception as exc:
        snap["scores_error"] = str(exc)

    # Conviction meter
    try:
        from plugin_conviction import compute_conviction
        conv = compute_conviction()
        snap["conviction"] = {
            "score": conv.get("score"),
            "direction": conv.get("direction"),
            "label": conv.get("label"),
            "signed_score": conv.get("signed_score"),
        }
    except Exception as exc:
        snap["conviction_error"] = str(exc)

    # Binance metrics
    try:
        from shared.models.binance_metrics import (
            BinanceFundingRate,
            BinanceOpenInterest,
            BinanceLongShortRatio,
        )
        with SessionLocal() as session:
            fr = (
                session.query(BinanceFundingRate)
                .order_by(desc(BinanceFundingRate.funding_time))
                .first()
            )
            oi = (
                session.query(BinanceOpenInterest)
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
            snap["binance"] = {
                "funding_rate_pct": round(fr.funding_rate * 100, 4) if fr else None,
                "open_interest": oi.open_interest if oi else None,
                "top_trader_long_pct": top.long_pct if top else None,
                "global_retail_long_pct": glob.long_pct if glob else None,
            }
    except Exception as exc:
        snap["binance_error"] = str(exc)

    # Active anomalies count
    try:
        from plugin_anomalies import detect_anomalies
        anomalies = detect_anomalies()
        snap["active_anomalies"] = [
            {"category": a["category"], "severity": a["severity"], "direction": a["direction"]}
            for a in anomalies
        ]
    except Exception as exc:
        snap["anomalies_error"] = str(exc)

    return snap


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

def _compute_stats(campaigns: list[Campaign]) -> dict:
    """Compute aggregate performance metrics from closed campaigns."""
    closed = [c for c in campaigns if c.status.startswith("closed")]
    if not closed:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "total_pnl": 0.0,
            "avg_win": None,
            "avg_loss": None,
            "profit_factor": None,
            "largest_win": 0.0,
            "largest_loss": 0.0,
            "avg_duration_minutes": None,
            "by_close_reason": {},
        }

    wins: list[float] = []
    losses: list[float] = []
    durations: list[float] = []
    by_reason: dict[str, int] = {}
    total_pnl = 0.0

    for c in closed:
        pnl = c.realized_pnl or 0.0
        total_pnl += pnl
        if pnl >= 0:
            wins.append(pnl)
        else:
            losses.append(pnl)

        if c.opened_at and c.closed_at:
            durations.append((c.closed_at - c.opened_at).total_seconds() / 60.0)

        by_reason[c.status] = by_reason.get(c.status, 0) + 1

    n = len(closed)
    win_rate = (len(wins) / n) * 100 if n else None
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    total_win = sum(wins)
    total_loss = abs(sum(losses))
    profit_factor = (total_win / total_loss) if total_loss > 0 else (None if total_win == 0 else math.inf)

    largest_win = max(wins, default=0.0)
    largest_loss = min(losses, default=0.0)
    avg_duration = (sum(durations) / len(durations)) if durations else None

    # Sharpe-like: mean PnL / stdev PnL. Not annualised.
    pnls = wins + losses
    if len(pnls) >= 2:
        mean = sum(pnls) / len(pnls)
        var = sum((x - mean) ** 2 for x in pnls) / (len(pnls) - 1)
        stdev = math.sqrt(var) if var > 0 else 0.0
        sharpe_like = round(mean / stdev, 3) if stdev > 0 else None
    else:
        sharpe_like = None

    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2) if win_rate is not None else None,
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
        "profit_factor": round(profit_factor, 3) if profit_factor not in (None, math.inf) else profit_factor,
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
        "avg_duration_minutes": round(avg_duration, 1) if avg_duration is not None else None,
        "sharpe_like": sharpe_like,
        "by_close_reason": by_reason,
    }


def get_journal(limit: int = 100, include_open: bool = False) -> dict:
    """Return recent campaigns with snapshots + aggregate stats."""
    with SessionLocal() as session:
        query = session.query(Campaign).order_by(desc(Campaign.opened_at))
        if not include_open:
            query = query.filter(Campaign.status != "open")
        campaigns = query.limit(limit).all()

    entries: list[dict] = []
    for c in campaigns:
        duration = None
        if c.opened_at and c.closed_at:
            duration = (c.closed_at - c.opened_at).total_seconds() / 60.0

        # Compute simple PnL % vs starting margin (rough reference)
        entry_margin = None
        if c.entry_snapshot and isinstance(c.entry_snapshot, dict):
            acc = c.entry_snapshot.get("account") or {}
            entry_margin = acc.get("margin_used")

        pnl_pct = None
        if c.realized_pnl is not None and entry_margin and entry_margin > 0:
            pnl_pct = round((c.realized_pnl / entry_margin) * 100, 2)

        entries.append({
            "id": c.id,
            "side": c.side,
            "status": c.status,
            "opened_at": c.opened_at.isoformat() if c.opened_at else None,
            "closed_at": c.closed_at.isoformat() if c.closed_at else None,
            "duration_minutes": round(duration, 1) if duration is not None else None,
            "realized_pnl": c.realized_pnl,
            "pnl_pct_of_entry_margin": pnl_pct,
            "notes": c.notes,
            "entry_snapshot": c.entry_snapshot,
            "exit_snapshot": c.exit_snapshot,
        })

    stats = _compute_stats(campaigns)
    return {"entries": entries, "stats": stats}


def attach_entry_snapshot(campaign_id: int, reason: str | None = None) -> None:
    """Capture and persist an entry snapshot on a newly-opened campaign."""
    snap = capture_snapshot(reason=reason or "campaign_open")
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
    """Capture and persist an exit snapshot on a closed campaign."""
    snap = capture_snapshot(reason=reason or "campaign_close")
    if extra:
        snap.update(extra)
    try:
        with SessionLocal() as session:
            row = session.query(Campaign).filter(Campaign.id == campaign_id).first()
            if row is None:
                return
            row.exit_snapshot = snap
            session.commit()
    except Exception:
        logger.exception("attach_exit_snapshot failed for campaign %s", campaign_id)

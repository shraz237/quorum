"""Position lifecycle helpers shared across services.

Used by ai-brain (to open positions from new signals & check TP/SL),
notifier (to format position alerts), and dashboard (to display state).

Wave 4B: Integrates with Campaign and Account models.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from shared.models.base import SessionLocal
from shared.models.campaigns import Campaign
from shared.models.ohlcv import OHLCV
from shared.models.positions import Position
from shared.sizing import (
    DCA_LAYERS_MARGIN,
    DCA_DRAWDOWN_TRIGGER_PCT,
    HARD_STOP_DRAWDOWN_PCT,
    lots_from_margin,
    next_layer_margin,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def get_current_price() -> float | None:
    """Return the most recent WTI close (Yahoo CL=F)."""
    with SessionLocal() as session:
        row = (
            session.query(OHLCV)
            .filter(OHLCV.timeframe == "1min", OHLCV.source == "yahoo")
            .order_by(OHLCV.timestamp.desc())
            .first()
        )
        if row is None:
            row = (
                session.query(OHLCV)
                .filter(OHLCV.timeframe == "1min")
                .order_by(OHLCV.timestamp.desc())
                .first()
            )
        return float(row.close) if row else None


def get_current_bar() -> tuple[float, float, float] | None:
    """Return (high, low, close) of the latest Yahoo 1-min bar."""
    with SessionLocal() as session:
        row = (
            session.query(OHLCV)
            .filter(OHLCV.timeframe == "1min", OHLCV.source == "yahoo")
            .order_by(OHLCV.timestamp.desc())
            .first()
        )
        return (float(row.high), float(row.low), float(row.close)) if row else None


# ---------------------------------------------------------------------------
# Low-level position helpers
# ---------------------------------------------------------------------------

def list_open_positions() -> list[dict]:
    """Return a list of currently-open positions enriched with live P/L."""
    price = get_current_price()
    with SessionLocal() as session:
        stmt = select(Position).where(Position.status == "open").order_by(Position.opened_at)
        rows = session.scalars(stmt).all()

        result: list[dict] = []
        for p in rows:
            unrealised = None
            if price is not None and p.lots is not None:
                if p.side == "LONG":
                    unrealised = (price - p.entry_price) * p.lots * 100
                elif p.side == "SHORT":
                    unrealised = (p.entry_price - price) * p.lots * 100
            elif price is not None:
                # Legacy position without lots — simple point move
                if p.side == "LONG":
                    unrealised = price - p.entry_price
                elif p.side == "SHORT":
                    unrealised = p.entry_price - price

            result.append(
                {
                    "id": p.id,
                    "side": p.side,
                    "status": p.status,
                    "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                    "entry_price": p.entry_price,
                    "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
                    "current_price": price,
                    "unrealised_pnl": round(unrealised, 4) if unrealised is not None else None,
                    "unrealised_pct": (
                        round((unrealised / (p.entry_price * (p.lots or 1) * 100)) * 100, 2)
                        if unrealised is not None and p.lots
                        else None
                    ),
                    "recommendation_id": p.recommendation_id,
                    "campaign_id": p.campaign_id,
                    "lots": p.lots,
                    "margin_used": p.margin_used,
                    "layer_index": p.layer_index,
                }
            )
    return result


def open_position(
    side: str,
    entry_price: float,
    stop_loss: float | None,
    take_profit: float | None,
    recommendation_id: int | None = None,
    notes: str | None = None,
    campaign_id: int | None = None,
    lots: float | None = None,
    margin_used: float | None = None,
    nominal_value: float | None = None,
    layer_index: int | None = None,
) -> int | None:
    """Insert a new open Position and return its id.

    If margin_used is provided, also calls account_manager.apply_position_open().
    """
    from shared.account_manager import apply_position_open

    side_norm = side.upper()
    if side_norm not in ("LONG", "SHORT"):
        return None

    with SessionLocal() as session:
        row = Position(
            opened_at=datetime.now(tz=timezone.utc),
            side=side_norm,
            status="open",
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            recommendation_id=recommendation_id,
            notes=notes,
            campaign_id=campaign_id,
            lots=lots,
            margin_used=margin_used,
            nominal_value=nominal_value,
            layer_index=layer_index,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        logger.info(
            "Opened position #%s %s @ %.2f lots=%s margin=%s layer=%s campaign=%s",
            row.id, side_norm, entry_price, lots, margin_used, layer_index, campaign_id,
        )

    # Reserve margin in the account (outside the position session to avoid deadlock)
    if margin_used is not None:
        try:
            apply_position_open(margin_used)
        except Exception:
            logger.exception("apply_position_open failed for position #%s", row.id)

    return row.id


def close_position(
    position_id: int,
    close_price: float,
    status: str,
    notes: str | None = None,
) -> dict | None:
    """Close a position and return the closed-row snapshot.

    PnL is computed in USD = (close - entry) * lots * 100, signed by side.
    Falls back to simple point move for legacy positions without lots.
    Also calls account_manager.apply_position_close().
    """
    from shared.account_manager import apply_position_close

    with SessionLocal() as session:
        row = session.execute(
            select(Position)
            .where(Position.id == position_id, Position.status == "open")
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
        if row is None:
            return None  # already closed by someone else

        lots = row.lots
        margin = row.margin_used or 0.0

        if lots is not None:
            # Full dollar P&L
            if row.side == "LONG":
                pnl = (close_price - row.entry_price) * lots * 100
            else:
                pnl = (row.entry_price - close_price) * lots * 100
        else:
            # Legacy fallback — simple point move (not dollar-accurate)
            if row.side == "LONG":
                pnl = close_price - row.entry_price
            else:
                pnl = row.entry_price - close_price

        row.status = status
        row.close_price = close_price
        row.closed_at = datetime.now(tz=timezone.utc)
        row.realised_pnl = pnl
        if notes:
            row.notes = (row.notes + "\n" if row.notes else "") + notes
        session.commit()

        logger.info(
            "Closed position #%s %s status=%s pnl=%+.2f margin=%.2f",
            row.id, row.side, status, pnl, margin,
        )
        snap = {
            "id": row.id,
            "side": row.side,
            "status": status,
            "entry_price": row.entry_price,
            "close_price": close_price,
            "realised_pnl": pnl,
            "lots": lots,
            "margin_used": margin,
            "layer_index": row.layer_index,
            "campaign_id": row.campaign_id,
        }

    # Return margin + pnl to the account
    try:
        apply_position_close(margin, pnl)
    except Exception:
        logger.exception("apply_position_close failed for position #%s", position_id)

    return snap


# ---------------------------------------------------------------------------
# Campaign helpers
# ---------------------------------------------------------------------------

def _position_to_layer_dict(p: Position, current_price: float | None) -> dict:
    """Convert a Position ORM row to the layer sub-dict used in campaign responses."""
    return {
        "id": p.id,
        "layer_index": p.layer_index,
        "entry_price": p.entry_price,
        "lots": p.lots,
        "margin_used": p.margin_used,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
    }


def compute_campaign_state(campaign_id: int, current_price: float | None = None) -> dict | None:
    """Return the full campaign dict matching the API contract shape.

    Computes avg_entry, total_lots, total_margin, unrealised_pnl from positions.
    """
    if current_price is None:
        current_price = get_current_price()

    with SessionLocal() as session:
        campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
        if campaign is None:
            return None

        open_pos = (
            session.query(Position)
            .filter(Position.campaign_id == campaign_id, Position.status == "open")
            .order_by(Position.opened_at)
            .all()
        )

        total_lots = sum(p.lots or 0.0 for p in open_pos)
        total_margin = sum(p.margin_used or 0.0 for p in open_pos)
        total_nominal = sum(p.nominal_value or 0.0 for p in open_pos)
        layers_used = len(open_pos)

        # Weighted average entry price
        if total_lots > 0:
            avg_entry = sum((p.lots or 0.0) * p.entry_price for p in open_pos) / total_lots
        else:
            avg_entry = None

        # Unrealised PnL
        unrealised_pnl = 0.0
        if current_price is not None and total_lots > 0:
            if campaign.side == "LONG":
                unrealised_pnl = (current_price - avg_entry) * total_lots * 100
            else:
                unrealised_pnl = (avg_entry - current_price) * total_lots * 100

        # Unrealised PnL as % of total margin
        if total_margin > 0:
            unrealised_pnl_pct = round((unrealised_pnl / total_margin) * 100, 2)
        else:
            unrealised_pnl_pct = 0.0

        next_margin = next_layer_margin(layers_used)

        positions_data = [_position_to_layer_dict(p, current_price) for p in open_pos]

        return {
            "id": campaign.id,
            "side": campaign.side,
            "status": campaign.status,
            "opened_at": campaign.opened_at.isoformat() if campaign.opened_at else None,
            "closed_at": campaign.closed_at.isoformat() if campaign.closed_at else None,
            "avg_entry_price": round(avg_entry, 5) if avg_entry is not None else None,
            "total_lots": round(total_lots, 5),
            "total_margin": round(total_margin, 2),
            "total_nominal": round(total_nominal, 2),
            "layers_used": layers_used,
            "max_layers": len(DCA_LAYERS_MARGIN),
            "next_layer_margin": next_margin,
            "current_price": current_price,
            "unrealised_pnl": round(unrealised_pnl, 2),
            "unrealised_pnl_pct": unrealised_pnl_pct,
            "max_loss_pct": campaign.max_loss_pct,
            "realized_pnl": campaign.realized_pnl,
            "notes": campaign.notes,
            "positions": positions_data,
        }


def list_open_campaigns() -> list[dict]:
    """Return all open campaigns with computed fields."""
    current_price = get_current_price()

    with SessionLocal() as session:
        campaigns = (
            session.query(Campaign)
            .filter(Campaign.status == "open")
            .order_by(Campaign.opened_at)
            .all()
        )
        ids = [c.id for c in campaigns]

    return [compute_campaign_state(cid, current_price) for cid in ids if cid is not None]


def list_campaigns(status: str | None = None, limit: int = 50) -> list[dict]:
    """Return campaigns filtered by status (or all if status is None)."""
    current_price = get_current_price()

    with SessionLocal() as session:
        q = session.query(Campaign).order_by(Campaign.opened_at.desc())
        if status and status != "all":
            if status == "open":
                q = q.filter(Campaign.status == "open")
            elif status == "closed":
                q = q.filter(Campaign.status != "open")
        q = q.limit(limit)
        campaigns = q.all()
        ids = [c.id for c in campaigns]

    return [compute_campaign_state(cid, current_price) for cid in ids if cid is not None]


def open_new_campaign(side: str, current_price: float) -> int:
    """Create a Campaign row and open the first DCA layer (layer 0).

    Returns the campaign id.
    """
    side_norm = side.upper()
    if side_norm not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid side: {side}")

    margin = DCA_LAYERS_MARGIN[0]
    lots = lots_from_margin(margin, current_price)
    nom = lots * 100 * current_price

    now = datetime.now(tz=timezone.utc)

    with SessionLocal() as session:
        campaign = Campaign(
            opened_at=now,
            side=side_norm,
            status="open",
            max_loss_pct=HARD_STOP_DRAWDOWN_PCT,
        )
        session.add(campaign)
        session.flush()  # get campaign.id before committing
        campaign_id = campaign.id
        session.commit()

    # Open the first position
    open_position(
        side=side_norm,
        entry_price=current_price,
        stop_loss=None,
        take_profit=None,
        campaign_id=campaign_id,
        lots=lots,
        margin_used=margin,
        nominal_value=nom,
        layer_index=0,
    )

    logger.info(
        "Opened campaign #%s %s @ %.2f (layer 0, lots=%.4f, margin=%.0f)",
        campaign_id, side_norm, current_price, lots, margin,
    )
    return campaign_id


def add_dca_layer(campaign_id: int, current_price: float) -> int | None:
    """Open the next DCA layer position in the campaign.

    Returns the new position id, or None if all layers are exhausted.
    """
    with SessionLocal() as session:
        campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
        if campaign is None or campaign.status != "open":
            return None

        campaign_side = campaign.side  # read while session is open
        layers_used = (
            session.query(Position)
            .filter(Position.campaign_id == campaign_id, Position.status == "open")
            .count()
        )

    margin = next_layer_margin(layers_used)
    if margin is None:
        logger.info("Campaign #%s: all DCA layers exhausted", campaign_id)
        return None

    lots = lots_from_margin(margin, current_price)
    nom = lots * 100 * current_price

    pos_id = open_position(
        side=campaign_side,
        entry_price=current_price,
        stop_loss=None,
        take_profit=None,
        campaign_id=campaign_id,
        lots=lots,
        margin_used=margin,
        nominal_value=nom,
        layer_index=layers_used,
    )

    logger.info(
        "Campaign #%s: added DCA layer %d @ %.2f (lots=%.4f, margin=%.0f)",
        campaign_id, layers_used, current_price, lots, margin,
    )
    return pos_id


def close_campaign(campaign_id: int, status: str, notes: str | None = None) -> dict | None:
    """Close all open positions in the campaign and mark it closed.

    Returns the aggregate snapshot matching the campaign API shape.
    """
    current_price = get_current_price()
    if current_price is None:
        logger.error("close_campaign: no current price available")
        return None

    with SessionLocal() as session:
        campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
        if campaign is None:
            return None

        open_pos = (
            session.query(Position)
            .filter(Position.campaign_id == campaign_id, Position.status == "open")
            .all()
        )
        pos_ids = [p.id for p in open_pos]

    total_pnl = 0.0
    closed_snaps = []
    for pos_id in pos_ids:
        snap = close_position(
            pos_id,
            close_price=current_price,
            status=status,
            notes=notes or f"Campaign closed: {status}",
        )
        if snap:
            total_pnl += snap.get("realised_pnl") or 0.0
            closed_snaps.append(snap)

    # Mark the campaign closed
    with SessionLocal() as session:
        campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
        if campaign:
            campaign.status = status
            campaign.closed_at = datetime.now(tz=timezone.utc)
            campaign.realized_pnl = total_pnl
            if notes:
                campaign.notes = (campaign.notes + "\n" if campaign.notes else "") + notes
            session.commit()

    logger.info(
        "Closed campaign #%s status=%s total_pnl=%+.2f positions=%d",
        campaign_id, status, total_pnl, len(closed_snaps),
    )

    # Return the campaign state snapshot
    result = compute_campaign_state(campaign_id, current_price)
    return result


# ---------------------------------------------------------------------------
# TP/SL / hard-stop check
# ---------------------------------------------------------------------------

def check_tp_sl_hits() -> list[dict]:
    """Scan open campaigns and enforce the hard-stop drawdown rule and campaign-level TP.

    For each open campaign:
      - If campaign.take_profit is set and price crosses it → close_campaign(status='closed_tp')
      - Compute unrealised PnL % vs total margin
      - If pnl_pct <= -max_loss_pct → close_campaign(status='closed_hard_stop')

    Returns the list of newly-closed campaign snapshots.
    """
    bar = get_current_bar()
    current_price = get_current_price()
    if current_price is None:
        return []

    high = bar[0] if bar else current_price
    low = bar[1] if bar else current_price

    closed: list[dict] = []

    with SessionLocal() as session:
        open_camps = (
            session.query(Campaign)
            .filter(Campaign.status == "open")
            .all()
        )
        camp_rows = [(c.id, c.side, c.take_profit, c.max_loss_pct) for c in open_camps]

    for camp_id, side, camp_tp, camp_max_loss in camp_rows:
        # 1) Campaign-level take-profit check
        if camp_tp is not None:
            tp_hit = (side == "LONG" and high >= camp_tp) or (
                side == "SHORT" and low <= camp_tp
            )
            if tp_hit:
                logger.info(
                    "Campaign #%s TP triggered: take_profit=%.2f (high=%.2f low=%.2f)",
                    camp_id, camp_tp, high, low,
                )
                snap = close_campaign(
                    camp_id,
                    status="closed_tp",
                    notes=f"Campaign TP hit: price reached {camp_tp:.2f}",
                )
                if snap:
                    closed.append(snap)
                continue  # already closed, skip hard-stop check

        # 2) Hard-stop drawdown check
        camp = compute_campaign_state(camp_id, current_price)
        if camp is None:
            continue
        pnl_pct = camp.get("unrealised_pnl_pct") or 0.0
        threshold = camp_max_loss if camp_max_loss else HARD_STOP_DRAWDOWN_PCT
        if pnl_pct <= -threshold:
            logger.warning(
                "Campaign #%s hard stop triggered: pnl_pct=%.2f%% threshold=%.2f%%",
                camp_id, pnl_pct, threshold,
            )
            snap = close_campaign(
                camp_id,
                status="closed_hard_stop",
                notes=f"Hard stop: drawdown {pnl_pct:.2f}% exceeded -{threshold}%",
            )
            if snap:
                closed.append(snap)

    return closed


# ---------------------------------------------------------------------------
# Campaign management helpers (used by plugin_campaign_mgmt)
# ---------------------------------------------------------------------------

def partial_close_campaign(
    campaign_id: int,
    pct: float,
    current_price: float,
    reason: str,
) -> dict:
    """Close a fraction of a campaign's open layers (oldest first).

    Closes whole layers until cumulative closed_lots >= target_lots_to_close.
    Returns {closed_count, closed_lots, realized_pnl, remaining_lots}.
    """
    if not (0 < pct <= 100):
        return {"error": "pct must be between 0 and 100 (exclusive lower)"}

    with SessionLocal() as session:
        campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
        if campaign is None:
            return {"error": f"campaign {campaign_id} not found"}
        if campaign.status != "open":
            return {"error": f"campaign {campaign_id} is not open (status={campaign.status})"}

        open_pos = (
            session.query(Position)
            .filter(Position.campaign_id == campaign_id, Position.status == "open")
            .order_by(Position.opened_at)
            .all()
        )
        if not open_pos:
            return {"error": f"campaign {campaign_id} has no open positions"}

        total_open_lots = sum(p.lots or 0.0 for p in open_pos)
        target_lots = total_open_lots * pct / 100.0
        pos_ids_to_close: list[int] = []
        accumulated = 0.0

        for p in open_pos:
            if accumulated >= target_lots:
                break
            pos_ids_to_close.append(p.id)
            accumulated += p.lots or 0.0

    # Close the selected positions outside the session
    closed_count = 0
    closed_lots = 0.0
    realized_pnl = 0.0

    for pos_id in pos_ids_to_close:
        snap = close_position(
            pos_id,
            close_price=current_price,
            status="closed_manual",
            notes=f"Partial close ({pct:.1f}%): {reason}",
        )
        if snap:
            closed_count += 1
            closed_lots += snap.get("lots") or 0.0
            realized_pnl += snap.get("realised_pnl") or 0.0

    remaining_lots = total_open_lots - closed_lots

    logger.info(
        "partial_close_campaign #%s: closed %d layers, lots=%.4f, pnl=%.2f, remaining=%.4f",
        campaign_id, closed_count, closed_lots, realized_pnl, remaining_lots,
    )
    return {
        "campaign_id": campaign_id,
        "closed_count": closed_count,
        "closed_lots": round(closed_lots, 5),
        "realized_pnl": round(realized_pnl, 2),
        "remaining_lots": round(remaining_lots, 5),
        "close_price": current_price,
        "reason": reason,
    }


def update_campaign_limits(campaign_id: int, max_loss_pct: float) -> dict:
    """Update the max_loss_pct hard-stop threshold for an open campaign."""
    if not (1 <= max_loss_pct <= 90):
        return {"error": "max_loss_pct must be between 1 and 90"}

    with SessionLocal() as session:
        campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
        if campaign is None:
            return {"error": f"campaign {campaign_id} not found"}
        if campaign.status != "open":
            return {"error": f"campaign {campaign_id} is not open (status={campaign.status})"}

        old_val = campaign.max_loss_pct
        campaign.max_loss_pct = max_loss_pct
        session.commit()

    logger.info(
        "update_campaign_limits #%s: max_loss_pct %.1f → %.1f",
        campaign_id, old_val, max_loss_pct,
    )
    return {
        "campaign_id": campaign_id,
        "old_max_loss_pct": old_val,
        "new_max_loss_pct": max_loss_pct,
        "updated": True,
    }

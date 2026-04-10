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

PRICE_SOURCE: str = "twelve"  # single canonical price source (Twelve Data WTI/USD)


def get_current_price() -> float | None:
    """Return the most recent 1-min WTI close from Twelve Data."""
    with SessionLocal() as session:
        row = (
            session.query(OHLCV)
            .filter(OHLCV.timeframe == "1min", OHLCV.source == PRICE_SOURCE)
            .order_by(OHLCV.timestamp.desc())
            .first()
        )
        return float(row.close) if row else None


def get_current_bar() -> tuple[float, float, float] | None:
    """Return (high, low, close) of the newest 1-min WTI bar."""
    with SessionLocal() as session:
        row = (
            session.query(OHLCV)
            .filter(OHLCV.timeframe == "1min", OHLCV.source == PRICE_SOURCE)
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

    Applies realistic trading friction (spread + slippage) to the entry
    price so the paper book reflects what a real broker fill would look
    like. The mid_price (the "screen price") is stored in notes for
    audit; the Position row gets the effective (worse) entry.

    If margin_used is provided, also calls account_manager.apply_position_open().
    """
    from shared.account_manager import apply_position_open
    from shared.trading_friction import apply_entry_friction, compute_commission

    side_norm = side.upper()
    if side_norm not in ("LONG", "SHORT"):
        return None

    # Apply spread + slippage to degrade the entry
    effective_entry, friction_detail = apply_entry_friction(side_norm, entry_price)

    # Commission on open side
    open_commission, comm_detail = compute_commission(lots or 0.0)

    friction_note = (
        f"[friction] mid=${entry_price:.3f} → entry=${effective_entry:.3f} "
        f"(spread=${friction_detail['spread_half']:.3f}, slip=${friction_detail['slippage']:.4f})"
    )
    if open_commission > 0:
        friction_note += f" comm=${open_commission:.2f}"
    full_notes = f"{notes}\n{friction_note}" if notes else friction_note

    with SessionLocal() as session:
        row = Position(
            opened_at=datetime.now(tz=timezone.utc),
            side=side_norm,
            status="open",
            entry_price=effective_entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            recommendation_id=recommendation_id,
            notes=full_notes,
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
            "Opened position #%s %s @ %.3f (mid %.3f) lots=%s margin=%s layer=%s campaign=%s",
            row.id, side_norm, effective_entry, entry_price, lots, margin_used, layer_index, campaign_id,
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

    Applies realistic trading friction:
      - Spread + slippage on the close price (adverse fill)
      - Swap fees based on number of nights held
      - Currency conversion costs
      - Commission on the close side

    PnL = gross price move × lots × 100 − total friction costs.
    Also calls account_manager.apply_position_close().
    """
    from shared.account_manager import apply_position_close
    from shared.trading_friction import (
        apply_exit_friction,
        compute_commission,
        compute_holding_costs,
    )

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

        # Apply spread + slippage to degrade the close price
        effective_close, exit_friction = apply_exit_friction(row.side, close_price)

        # Gross P&L at the effective close
        if lots is not None:
            if row.side == "LONG":
                gross_pnl = (effective_close - row.entry_price) * lots * 100
            else:
                gross_pnl = (row.entry_price - effective_close) * lots * 100
        else:
            if row.side == "LONG":
                gross_pnl = effective_close - row.entry_price
            else:
                gross_pnl = row.entry_price - effective_close

        # Holding costs (swap + currency conversion)
        holding_cost = 0.0
        holding_detail: dict = {}
        if lots is not None and row.opened_at is not None:
            closed_at = datetime.now(tz=timezone.utc)
            holding_cost, holding_detail = compute_holding_costs(
                side=row.side,
                lots=lots,
                margin_used=margin,
                opened_at=row.opened_at,
                closed_at=closed_at,
            )

        # Commission on close side
        close_commission, comm_detail = compute_commission(lots or 0.0)

        # Net P&L = gross − all friction costs
        total_friction = holding_cost + close_commission
        net_pnl = gross_pnl - total_friction

        # Build friction note for the trade journal
        friction_note = (
            f"[friction] mid=${close_price:.3f} → close=${effective_close:.3f} "
            f"(spread=${exit_friction['spread_half']:.3f}, slip=${exit_friction['slippage']:.4f})"
        )
        if holding_cost > 0:
            friction_note += (
                f" | swap={holding_detail.get('nights_held', 0)} nights × "
                f"${holding_detail.get('swap_per_lot_per_night', 0)}/lot = ${holding_detail.get('swap_cost_usd', 0):.2f}"
            )
        if holding_detail.get("conversion_cost_usd", 0) > 0:
            friction_note += f" | fx=${holding_detail['conversion_cost_usd']:.2f}"
        if close_commission > 0:
            friction_note += f" | comm=${close_commission:.2f}"
        friction_note += f" | gross=${gross_pnl:+.2f} net=${net_pnl:+.2f} friction_total=${total_friction:.2f}"

        full_notes = notes or ""
        full_notes = f"{full_notes}\n{friction_note}" if full_notes else friction_note

        row.status = status
        row.close_price = effective_close
        row.closed_at = datetime.now(tz=timezone.utc)
        row.realised_pnl = net_pnl
        if full_notes:
            row.notes = (row.notes + "\n" if row.notes else "") + full_notes
        session.commit()

        logger.info(
            "Closed position #%s %s status=%s gross=%+.2f net=%+.2f friction=%.2f (swap=%.2f fx=%.2f comm=%.2f)",
            row.id, row.side, status, gross_pnl, net_pnl,
            total_friction, holding_detail.get("swap_cost_usd", 0),
            holding_detail.get("conversion_cost_usd", 0), close_commission,
        )
        snap = {
            "id": row.id,
            "side": row.side,
            "status": status,
            "entry_price": row.entry_price,
            "close_price": effective_close,
            "mid_close_price": close_price,
            "realised_pnl": net_pnl,
            "gross_pnl": gross_pnl,
            "friction_costs": {
                "spread_slippage": exit_friction,
                "holding": holding_detail,
                "commission": comm_detail,
                "total_friction_usd": round(total_friction, 2),
            },
            "lots": lots,
            "margin_used": margin,
            "layer_index": row.layer_index,
            "campaign_id": row.campaign_id,
        }

    # Return margin + NET pnl to the account
    try:
        apply_position_close(margin, net_pnl)
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

        # Unrealised PnL — includes estimated exit friction (spread + slippage)
        # so the displayed number reflects what you'd actually get if you
        # closed RIGHT NOW. Without this, the dashboard looks rosier than
        # reality because it assumes a mid-price exit.
        unrealised_pnl = 0.0
        if current_price is not None and total_lots > 0:
            from shared.trading_friction import SPREAD_HALF_USD
            # Estimate the effective close price (mid - half_spread for LONG,
            # mid + half_spread for SHORT) — we skip slippage in the estimate
            # since it's random and we want a stable display number.
            if campaign.side == "LONG":
                est_close = current_price - SPREAD_HALF_USD
                unrealised_pnl = (est_close - avg_entry) * total_lots * 100
            else:
                est_close = current_price + SPREAD_HALF_USD
                unrealised_pnl = (avg_entry - est_close) * total_lots * 100

        # Unrealised PnL as % of total margin
        if total_margin > 0:
            unrealised_pnl_pct = round((unrealised_pnl / total_margin) * 100, 2)
        else:
            unrealised_pnl_pct = 0.0

        campaign_multiplier = float(campaign.size_multiplier or 1.0)
        next_margin = next_layer_margin(layers_used, multiplier=campaign_multiplier)

        positions_data = [_position_to_layer_dict(p, current_price) for p in open_pos]

        # DCA preview: simulate adding the next layer at several price points.
        # Shows the user what their new avg / total exposure / breakeven
        # would be if they DCA'd here, or if they waited for a -1%, -2%,
        # -3% move first. Only meaningful when there IS a next layer available.
        dca_preview: list[dict] = []
        if (
            next_margin is not None
            and current_price is not None
            and avg_entry is not None
            and total_lots > 0
        ):
            # For a LONG we DCA on dips (negative offsets); for a SHORT on rallies
            # (positive offsets). Show a symmetric grid relative to trend.
            offsets = (
                [0.0, -0.005, -0.01, -0.02, -0.03]
                if campaign.side == "LONG"
                else [0.0, 0.005, 0.01, 0.02, 0.03]
            )
            for offset in offsets:
                trigger_price = current_price * (1.0 + offset)
                new_lots_at_layer = next_margin * 10 / (trigger_price * 100)  # 10x leverage, lot = 100 bbl
                new_total_lots = total_lots + new_lots_at_layer
                new_avg = (
                    (avg_entry * total_lots) + (trigger_price * new_lots_at_layer)
                ) / new_total_lots
                new_total_margin = total_margin + next_margin
                # Breakeven price = the new avg itself (ignoring fees)
                dca_preview.append({
                    "offset_pct": round(offset * 100, 2),
                    "trigger_price": round(trigger_price, 3),
                    "added_lots": round(new_lots_at_layer, 4),
                    "added_margin": round(next_margin, 2),
                    "new_total_lots": round(new_total_lots, 4),
                    "new_avg_entry": round(new_avg, 3),
                    "new_total_margin": round(new_total_margin, 2),
                    "new_breakeven": round(new_avg, 3),
                })

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
            "take_profit": campaign.take_profit,
            "stop_loss": campaign.stop_loss,
            "size_multiplier": round(campaign_multiplier, 3),
            "sizing_info": campaign.sizing_info,
            "current_price": current_price,
            "unrealised_pnl": round(unrealised_pnl, 2),
            "unrealised_pnl_pct": unrealised_pnl_pct,
            "max_loss_pct": campaign.max_loss_pct,
            "realized_pnl": campaign.realized_pnl,
            "notes": campaign.notes,
            "positions": positions_data,
            "dca_preview": dca_preview,
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


def _validate_tp_sl(
    side: str,
    entry: float,
    take_profit: float | None,
    stop_loss: float | None,
) -> tuple[float | None, float | None]:
    """Return (tp, sl) after sanity checks. Drops any level that's on the
    wrong side of the entry (e.g. LONG with TP below entry) instead of
    raising, so a partially-broken Opus output doesn't block the open."""
    tp, sl = take_profit, stop_loss
    if side == "LONG":
        if tp is not None and tp <= entry:
            logger.warning("rejecting LONG TP %.2f <= entry %.2f", tp, entry)
            tp = None
        if sl is not None and sl >= entry:
            logger.warning("rejecting LONG SL %.2f >= entry %.2f", sl, entry)
            sl = None
    elif side == "SHORT":
        if tp is not None and tp >= entry:
            logger.warning("rejecting SHORT TP %.2f >= entry %.2f", tp, entry)
            tp = None
        if sl is not None and sl <= entry:
            logger.warning("rejecting SHORT SL %.2f <= entry %.2f", sl, entry)
            sl = None
    return tp, sl


def update_campaign_levels(
    campaign_id: int,
    take_profit: float | None = None,
    stop_loss: float | None = None,
) -> dict | None:
    """Update an open campaign's take_profit and/or stop_loss in place.

    Used by the heartbeat Opus manager to tighten SL as profits build,
    or to pull TP closer when the thesis is losing steam. Validates the
    new levels against the CURRENT price (not the original entry) since
    the campaign is already in flight.

    Rules:
      - Campaign must be open.
      - For LONG: TP must be > current_price, SL must be < current_price.
      - For SHORT: TP must be < current_price, SL must be > current_price.
      - At least one of take_profit / stop_loss must be provided.
      - None means "leave unchanged" — passing explicit None for both is a no-op.
      - Invalid levels are REJECTED (return None) rather than silently dropped.
        The caller (heartbeat) needs to know the update failed so it can log
        the Opus hallucination.

    Returns a dict with the updated levels on success, None on validation failure
    or if the campaign doesn't exist / is already closed.
    """
    if take_profit is None and stop_loss is None:
        logger.warning("update_campaign_levels: both TP and SL are None, nothing to do")
        return None

    current_price = get_current_price()
    if current_price is None:
        logger.error("update_campaign_levels: no current price available")
        return None

    with SessionLocal() as session:
        campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
        if campaign is None:
            logger.warning("update_campaign_levels: campaign #%s not found", campaign_id)
            return None
        if campaign.status != "open":
            logger.warning(
                "update_campaign_levels: campaign #%s is %s, cannot update",
                campaign_id, campaign.status,
            )
            return None

        side = campaign.side

        # Validate new levels against CURRENT price
        if take_profit is not None:
            if side == "LONG" and take_profit <= current_price:
                logger.warning(
                    "update_campaign_levels: rejecting LONG TP %.3f <= current %.3f",
                    take_profit, current_price,
                )
                return None
            if side == "SHORT" and take_profit >= current_price:
                logger.warning(
                    "update_campaign_levels: rejecting SHORT TP %.3f >= current %.3f",
                    take_profit, current_price,
                )
                return None

        if stop_loss is not None:
            if side == "LONG" and stop_loss >= current_price:
                logger.warning(
                    "update_campaign_levels: rejecting LONG SL %.3f >= current %.3f",
                    stop_loss, current_price,
                )
                return None
            if side == "SHORT" and stop_loss <= current_price:
                logger.warning(
                    "update_campaign_levels: rejecting SHORT SL %.3f <= current %.3f",
                    stop_loss, current_price,
                )
                return None

        old_tp = campaign.take_profit
        old_sl = campaign.stop_loss
        if take_profit is not None:
            campaign.take_profit = take_profit
        if stop_loss is not None:
            campaign.stop_loss = stop_loss
        session.commit()

        result = {
            "campaign_id": campaign_id,
            "side": side,
            "current_price": current_price,
            "old_take_profit": old_tp,
            "new_take_profit": campaign.take_profit,
            "old_stop_loss": old_sl,
            "new_stop_loss": campaign.stop_loss,
        }

    logger.info(
        "Updated campaign #%s levels: TP %s -> %s, SL %s -> %s (price %.3f)",
        campaign_id,
        old_tp, result["new_take_profit"],
        old_sl, result["new_stop_loss"],
        current_price,
    )
    return result


def open_new_campaign(
    side: str,
    current_price: float,
    llm_confidence: float | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
) -> int | None:
    """Create a Campaign row and open the first DCA layer (layer 0).

    Size is determined dynamically:
      - Base Layer-0 margin from DCA_LAYERS_MARGIN_BASE[0]
      - Multiplied by compute_size_multiplier() based on current state
        (unified score, LLM confidence, funding, drawdown, volatility)
      - Capped by the 80%-equity safety net via apply_equity_cap()

    TP/SL from the caller (typically the LLM strategist) are validated
    against the entry price and persisted on the Campaign row so that
    check_tp_sl_hits() can auto-close the campaign when price crosses
    either level. Invalid levels (wrong side of entry) are silently
    dropped rather than blocking the open.

    Returns the campaign id, or None if the equity cap zeroed out the size.
    """
    from shared.dynamic_sizing import (
        apply_equity_cap,
        compute_size_multiplier,
    )
    from shared.sizing import DCA_LAYERS_MARGIN_BASE

    side_norm = side.upper()
    if side_norm not in ("LONG", "SHORT"):
        raise ValueError(f"Invalid side: {side}")

    # Sanitise TP/SL against the entry price
    take_profit, stop_loss = _validate_tp_sl(
        side_norm, current_price, take_profit, stop_loss,
    )

    # Compute multiplier + sizing info from current market state
    from shared.dynamic_sizing import _gather_sizing_state
    state = _gather_sizing_state(side=side_norm)
    multiplier, sizing_info = compute_size_multiplier(
        state=state,
        llm_confidence=llm_confidence,
    )

    base_margin = DCA_LAYERS_MARGIN_BASE[0]
    raw_margin = base_margin * multiplier

    # Equity safety net
    equity = state.get("equity") or 0
    already_locked = state.get("margin_used") or 0
    margin = apply_equity_cap(raw_margin, equity, already_locked)
    if margin <= 0:
        logger.warning(
            "open_new_campaign refused: equity cap zeroed out margin "
            "(equity=%.0f already_locked=%.0f requested=%.0f)",
            equity, already_locked, raw_margin,
        )
        return None

    if margin < raw_margin:
        sizing_info.setdefault("reasons", []).append(
            f"equity cap: requested ${raw_margin:.0f} → ${margin:.0f} "
            f"(max 80% of ${equity:.0f} equity)"
        )
        logger.warning(
            "open_new_campaign: equity cap reduced margin %.0f -> %.0f",
            raw_margin, margin,
        )

    lots = lots_from_margin(margin, current_price)
    nom = lots * 100 * current_price

    now = datetime.now(tz=timezone.utc)

    with SessionLocal() as session:
        campaign = Campaign(
            opened_at=now,
            side=side_norm,
            status="open",
            max_loss_pct=HARD_STOP_DRAWDOWN_PCT,
            take_profit=take_profit,
            stop_loss=stop_loss,
            size_multiplier=multiplier,
            sizing_info=sizing_info,
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
        "Opened campaign #%s %s @ %.2f (layer 0, lots=%.4f, margin=%.0f, "
        "multiplier=%.2fx base=%.0f, tp=%s, sl=%s)",
        campaign_id, side_norm, current_price, lots, margin,
        multiplier, base_margin,
        f"${take_profit:.2f}" if take_profit else "none",
        f"${stop_loss:.2f}" if stop_loss else "none",
    )

    # Capture entry snapshot for the trade journal (best-effort).
    try:
        from shared.trade_snapshot import attach_entry_snapshot
        attach_entry_snapshot(campaign_id, reason="campaign_open")
    except Exception:
        logger.exception("entry snapshot failed for campaign #%s", campaign_id)

    return campaign_id


def add_dca_layer(campaign_id: int, current_price: float) -> int | None:
    """Open the next DCA layer position in the campaign.

    Uses the campaign's stored size_multiplier so the proportional size
    stays consistent across layers. Equity cap is re-checked on every
    layer so a DCA that would exceed 80% total exposure is refused.
    """
    from shared.dynamic_sizing import apply_equity_cap
    from shared.account_manager import recompute_account_state

    with SessionLocal() as session:
        campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
        if campaign is None or campaign.status != "open":
            return None

        campaign_side = campaign.side  # read while session is open
        campaign_multiplier = float(campaign.size_multiplier or 1.0)
        layers_used = (
            session.query(Position)
            .filter(Position.campaign_id == campaign_id, Position.status == "open")
            .count()
        )

    raw_margin = next_layer_margin(layers_used, multiplier=campaign_multiplier)
    if raw_margin is None:
        logger.info("Campaign #%s: all DCA layers exhausted", campaign_id)
        return None

    # Equity safety net — refuse if this layer would push us over 80%
    acc = recompute_account_state()
    equity = acc.get("equity") or 0
    already_locked = acc.get("margin_used") or 0
    margin = apply_equity_cap(raw_margin, equity, already_locked)
    if margin <= 0:
        logger.warning(
            "add_dca_layer #%s refused: equity cap hit "
            "(equity=%.0f already_locked=%.0f requested=%.0f)",
            campaign_id, equity, already_locked, raw_margin,
        )
        return None
    if margin < raw_margin:
        logger.warning(
            "add_dca_layer #%s: equity cap reduced margin %.0f -> %.0f",
            campaign_id, raw_margin, margin,
        )

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

    # Capture exit snapshot for the trade journal. Best-effort — a broken
    # snapshot must not break the close.
    try:
        from shared.trade_snapshot import attach_exit_snapshot
        attach_exit_snapshot(campaign_id, reason=f"{status}:{notes or ''}"[:120])
    except Exception:
        logger.exception("exit snapshot failed for campaign #%s", campaign_id)

    # Return the campaign state snapshot
    result = compute_campaign_state(campaign_id, current_price)
    return result


# ---------------------------------------------------------------------------
# TP/SL / hard-stop check
# ---------------------------------------------------------------------------

def check_tp_sl_hits() -> list[dict]:
    """Scan open campaigns and enforce TP / SL / hard-stop rules.

    For each open campaign:
      - If campaign.take_profit is set and bar high/low crosses it →
        close_campaign(status='closed_tp')
      - If campaign.stop_loss is set and bar high/low crosses it →
        close_campaign(status='closed_sl')
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
        camp_rows = [
            (c.id, c.side, c.take_profit, c.stop_loss, c.max_loss_pct)
            for c in open_camps
        ]

    for camp_id, side, camp_tp, camp_sl, camp_max_loss in camp_rows:
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
                continue  # already closed, skip other checks

        # 2) Campaign-level stop-loss check
        if camp_sl is not None:
            sl_hit = (side == "LONG" and low <= camp_sl) or (
                side == "SHORT" and high >= camp_sl
            )
            if sl_hit:
                logger.warning(
                    "Campaign #%s SL triggered: stop_loss=%.2f (high=%.2f low=%.2f)",
                    camp_id, camp_sl, high, low,
                )
                snap = close_campaign(
                    camp_id,
                    status="closed_sl",
                    notes=f"Campaign SL hit: price reached {camp_sl:.2f}",
                )
                if snap:
                    closed.append(snap)
                continue

        # 3) Hard-stop drawdown check
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

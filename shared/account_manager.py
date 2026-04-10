"""Account manager — per-persona account rows + state recomputation.

Two personas share this module:
  main    — conservative DCA campaigns, managed by Opus Heartbeat
  scalper — fast scalp trades, managed by Scalp Brain auto-executor

Each persona has its own Account row ($50k starting balance each),
separate margin accounting, separate P/L tracking. All mutations use
SELECT FOR UPDATE to prevent race conditions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from shared.models.base import SessionLocal
from shared.models.account import Account
from shared.models.campaigns import Campaign
from shared.models.positions import Position

logger = logging.getLogger(__name__)

STARTING_BALANCE = 50_000.0  # per persona (was 100k total, now split 50/50)
DEFAULT_LEVERAGE = 10


def get_or_create_account(persona: str = "main") -> Account:
    """Return the Account row for `persona`, creating with defaults if missing."""
    with SessionLocal() as session:
        row = session.query(Account).filter(Account.persona == persona).first()
        if row is None:
            now = datetime.now(tz=timezone.utc)
            row = Account(
                persona=persona,
                starting_balance=STARTING_BALANCE,
                cash=STARTING_BALANCE,
                realized_pnl_total=0.0,
                leverage=DEFAULT_LEVERAGE,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            logger.info("Created new %s account (starting_balance=%.2f)", persona, STARTING_BALANCE)
        return row


def recompute_account_state(persona: str = "main") -> dict:
    """Compute and return the full account state for a specific persona.

    Filters positions by campaign persona — only positions belonging to
    campaigns owned by this persona count toward its margin + P/L.
    """
    from shared.position_manager import get_current_price
    from shared.trading_friction import SPREAD_HALF_USD

    current_price = get_current_price()

    with SessionLocal() as session:
        account = session.query(Account).filter(Account.persona == persona).first()
        if account is None:
            account = _create_account_in_session(session, persona)

        cash = account.cash
        realized_pnl_total = account.realized_pnl_total
        starting_balance = account.starting_balance
        leverage = account.leverage

        # Only gather positions from campaigns owned by this persona
        persona_campaign_ids = set(
            r[0] for r in session.query(Campaign.id)
            .filter(Campaign.persona == persona)
            .all()
        )

        open_positions = (
            session.query(Position)
            .filter(Position.status == "open")
            .all()
        )

        total_margin_used = 0.0
        total_unrealised = 0.0
        open_campaign_ids: set[int] = set()

        for p in open_positions:
            # Skip positions that don't belong to this persona
            if p.campaign_id is not None and p.campaign_id not in persona_campaign_ids:
                continue

            margin = p.margin_used or 0.0
            total_margin_used += margin

            if p.campaign_id is not None:
                open_campaign_ids.add(p.campaign_id)

            # Compute unrealised PnL with estimated exit spread
            if current_price is not None and p.lots is not None:
                lots = p.lots
                if p.side == "LONG":
                    est_close = current_price - SPREAD_HALF_USD
                    pnl = (est_close - p.entry_price) * lots * 100
                else:  # SHORT
                    est_close = current_price + SPREAD_HALF_USD
                    pnl = (p.entry_price - est_close) * lots * 100
                total_unrealised += pnl

        equity = cash + total_unrealised

        if total_margin_used > 0:
            margin_level_pct = round((equity / total_margin_used) * 100, 2)
        else:
            margin_level_pct = None

        free_margin = equity - total_margin_used

        if starting_balance > 0:
            account_drawdown_pct = round(
                ((equity - starting_balance) / starting_balance) * 100, 2
            )
        else:
            account_drawdown_pct = 0.0

        return {
            "persona": persona,
            "starting_balance": starting_balance,
            "cash": round(cash, 2),
            "equity": round(equity, 2),
            "margin_used": round(total_margin_used, 2),
            "free_margin": round(free_margin, 2),
            "margin_level_pct": margin_level_pct,
            "realized_pnl_total": round(realized_pnl_total, 2),
            "unrealised_pnl": round(total_unrealised, 2),
            "account_drawdown_pct": account_drawdown_pct,
            "account_hard_stop_pct": -50.0,
            "open_campaigns": len(open_campaign_ids),
            "leverage": leverage,
        }


def apply_position_open(margin_used: float, persona: str = "main") -> None:
    """Update the timestamp when a position opens (margin is derived, not booked)."""
    with SessionLocal() as session:
        account = (
            session.execute(
                select(Account)
                .where(Account.persona == persona)
                .with_for_update()
            ).scalar_one_or_none()
        )
        if account is None:
            account = _create_account_in_session(session, persona)
        account.updated_at = datetime.now(tz=timezone.utc)
        session.commit()


def apply_position_close(margin_used: float, realized_pnl: float, persona: str = "main") -> None:
    """Book realized PnL against the persona's cash."""
    with SessionLocal() as session:
        account = (
            session.execute(
                select(Account)
                .where(Account.persona == persona)
                .with_for_update()
            ).scalar_one_or_none()
        )
        if account is None:
            account = _create_account_in_session(session, persona)
        account.cash += realized_pnl
        account.realized_pnl_total += realized_pnl
        account.updated_at = datetime.now(tz=timezone.utc)
        session.commit()
        logger.info(
            "apply_position_close [%s]: cash=%.2f (+%.2f pnl)",
            persona, account.cash, realized_pnl,
        )


def _create_account_in_session(session, persona: str = "main") -> Account:
    """Create and flush account row within an existing session."""
    now = datetime.now(tz=timezone.utc)
    account = Account(
        persona=persona,
        starting_balance=STARTING_BALANCE,
        cash=STARTING_BALANCE,
        realized_pnl_total=0.0,
        leverage=DEFAULT_LEVERAGE,
        created_at=now,
        updated_at=now,
    )
    session.add(account)
    session.flush()
    return account

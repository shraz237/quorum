"""Scalper auto-executor — opens/closes mini-campaigns on the scalper persona.

When the Scalp Brain hits a NOW verdict with all gatekeepers passing,
this module auto-opens a small campaign on the scalper's separate $50k
account. When the verdict flips away from NOW, it auto-closes.

The scalper persona is completely independent from the main persona:
  - Own account balance ($50k starting)
  - Own campaigns (persona='scalper')
  - Own P/L tracking
  - Own trade journal entries

Rate limits:
  - Max 1 open scalper campaign at a time (no stacking)
  - 5-minute cooldown between trades (no jitter-driven churn)
  - Only executes on NOW verdicts (not LEAN)

Sizing: small fixed margin ($1000 per scalp) — the scalper is learning,
not swinging for the fences. As its hit rate proves out, you can increase
SCALPER_MARGIN_USD via env var.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from shared.models.base import SessionLocal
from shared.models.campaigns import Campaign
from shared.position_manager import (
    close_campaign,
    get_current_price,
    open_new_campaign,
)
from shared.redis_streams import get_redis, publish

logger = logging.getLogger(__name__)

# Sizing — small so the scalper can learn without blowing the account
SCALPER_MARGIN_USD = float(os.environ.get("SCALPER_MARGIN_USD", "1000"))

# Rate limit
SCALPER_COOLDOWN_SECONDS = 5 * 60  # 5 min between trades
REDIS_KEY_LAST_SCALP_TRADE = "scalper:last_trade_ts"

STREAM_POSITION = "position.event"


def _get_open_scalper_campaign() -> dict | None:
    """Return the open scalper campaign, or None if flat."""
    with SessionLocal() as session:
        camp = (
            session.query(Campaign)
            .filter(Campaign.persona == "scalper", Campaign.status == "open")
            .first()
        )
        if camp is None:
            return None
        return {
            "id": camp.id,
            "side": camp.side,
            "take_profit": camp.take_profit,
            "stop_loss": camp.stop_loss,
        }


def _cooldown_active() -> bool:
    try:
        r = get_redis()
        raw = r.get(REDIS_KEY_LAST_SCALP_TRADE)
        if raw is None:
            return False
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return (time.time() - float(raw)) < SCALPER_COOLDOWN_SECONDS
    except Exception:
        return False


def _mark_trade() -> None:
    try:
        get_redis().set(REDIS_KEY_LAST_SCALP_TRADE, str(time.time()))
    except Exception:
        logger.exception("Failed to mark scalper trade timestamp")


def maybe_execute(scalp_brain_result: dict) -> dict | None:
    """Called after every Scalp Brain compute. Opens or closes scalper
    campaigns based on the verdict. Returns a summary dict if something
    happened, None if no action.

    Call site: plugin_scalp_brain.get_scalp_brain() → here.
    """
    verdict = scalp_brain_result.get("verdict")
    intended_side = scalp_brain_result.get("intended_side")
    trade_levels = scalp_brain_result.get("trade_levels") or {}
    gates_passed = scalp_brain_result.get("gates_passed", 0)
    gates_total = scalp_brain_result.get("gates_total", 4)

    open_camp = _get_open_scalper_campaign()

    # --- CLOSE PATH: verdict flipped away from the open side ---
    if open_camp is not None:
        camp_side = open_camp["side"]
        should_close = False
        close_reason = ""

        if verdict in ("WAIT", "LEAN_LONG", "LEAN_SHORT"):
            should_close = True
            close_reason = f"scalp verdict flipped to {verdict} (was {camp_side})"
        elif verdict == "LONG" and camp_side == "SHORT":
            should_close = True
            close_reason = f"scalp verdict reversed: now LONG, was SHORT"
        elif verdict == "SHORT" and camp_side == "LONG":
            should_close = True
            close_reason = f"scalp verdict reversed: now SHORT, was LONG"

        if should_close:
            snap = close_campaign(
                open_camp["id"],
                status="closed_strategy",
                notes=f"scalper auto-close: {close_reason}",
            )
            if snap is not None:
                logger.info(
                    "Scalper auto-closed campaign #%s: %s (pnl=%s)",
                    open_camp["id"], close_reason, snap.get("realized_pnl"),
                )
                _publish_scalper_event(
                    "scalper_closed",
                    open_camp["id"],
                    camp_side,
                    close_reason,
                    snap,
                )
                _mark_trade()
                return {"action": "closed", "campaign_id": open_camp["id"], "reason": close_reason}

        # Still aligned — nothing to do
        return None

    # --- OPEN PATH: no open scalper campaign + NOW verdict ---
    if verdict not in ("LONG", "SHORT"):
        return None  # only NOW verdicts trigger opens

    if intended_side not in ("LONG", "SHORT"):
        return None

    if gates_passed < gates_total:
        return None  # not all gatekeepers passing

    if _cooldown_active():
        logger.info("Scalper: NOW verdict but cooldown active, skipping")
        return None

    current_price = get_current_price()
    if current_price is None:
        return None

    tp = trade_levels.get("take_profit_1")
    sl = trade_levels.get("stop_loss")

    try:
        campaign_id = open_new_campaign(
            side=intended_side,
            current_price=current_price,
            take_profit=tp,
            stop_loss=sl,
            persona="scalper",
        )
    except Exception:
        logger.exception("Scalper auto-open failed")
        return None

    if campaign_id is None:
        logger.warning("Scalper auto-open returned None (equity cap or validation)")
        return None

    logger.info(
        "Scalper auto-opened campaign #%s %s @ %.3f (TP=%.3f SL=%.3f margin=$%.0f)",
        campaign_id, intended_side, current_price,
        tp or 0, sl or 0, SCALPER_MARGIN_USD,
    )
    _publish_scalper_event(
        "scalper_opened",
        campaign_id,
        intended_side,
        f"Scalp Brain NOW verdict: {intended_side} @ ${current_price:.3f}",
        {"entry_price": current_price, "take_profit": tp, "stop_loss": sl},
    )
    _mark_trade()

    # Arm heartbeat hot window so the TP/SL check runs at 30s cadence
    try:
        from shared.heartbeat_hot import arm_hot_window
        arm_hot_window(reason=f"scalper opened #{campaign_id}")
    except Exception:
        logger.exception("Failed to arm hot window for scalper")

    return {"action": "opened", "campaign_id": campaign_id, "side": intended_side}


def _publish_scalper_event(event_type: str, campaign_id: int, side: str, reason: str, extra: dict | None = None) -> None:
    try:
        payload = {
            "type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "campaign_id": campaign_id,
            "side": side,
            "persona": "scalper",
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        publish(STREAM_POSITION, payload)
    except Exception:
        logger.exception("Failed to publish %s event", event_type)

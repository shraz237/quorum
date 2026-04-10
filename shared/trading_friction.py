"""Trading friction — realistic costs that degrade paper-book P/L.

The bot's internal book was running on "easy mode" — opening and closing
at exact mid-prices with zero cost. Real WTI CFD trading on XTB (or any
broker) involves:

  1. SPREAD — bid/ask gap. Entry is slightly worse than mid-price.
     We model this as half-spread applied adversely on BOTH open and
     close. So the round-trip spread cost is SPREAD_USD × 2.

  2. SLIPPAGE — market orders don't fill at the screen price.
     Small random adverse fill offset on every open and close.

  3. COMMISSION — XTB is commission-free on CFDs (spread covers it),
     but this field exists for brokers that charge explicit per-lot fees.

  4. SWAP (overnight fee) — charged per lot per night the position is
     held past the broker's rollover time (typically 00:00 broker time).
     Computed retroactively at close based on number of nights held.

  5. CURRENCY CONVERSION — XTB PLN accounts pay ~0.5% on USD margin.
     Applied once at open (margin deposit conversion) and once at
     close (P/L repatriation). We model this as a flat % of margin.

All values are configurable via environment variables with sensible
defaults for XTB OIL.WTI CFD. The friction is applied inside
position_manager.py at the open_position / close_position call sites
so every consumer (ai-brain, heartbeat, dashboard) gets realistic P/L
without any caller-side changes.

The friction details are attached to the position's `notes` field so
the trade journal can show exactly how much friction degraded the P/L.
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

# Half-spread in USD. Applied adversely on BOTH open and close.
# XTB OIL.WTI typical spread is $0.03-0.05 during liquid hours,
# $0.08-0.15 during Asian session. We use a conservative average.
SPREAD_HALF_USD = float(os.environ.get("FRICTION_SPREAD_HALF_USD", "0.04"))

# Slippage in USD. Random uniform [0, SLIPPAGE_MAX_USD] applied adversely.
# Set to 0 to disable. Typical for market orders during normal hours: $0.01-0.03.
SLIPPAGE_MAX_USD = float(os.environ.get("FRICTION_SLIPPAGE_MAX_USD", "0.03"))

# Commission per lot per side (open or close). XTB = 0 for CFDs.
COMMISSION_PER_LOT = float(os.environ.get("FRICTION_COMMISSION_PER_LOT", "0.0"))

# Swap (overnight fee) per lot per night. Approximate XTB OIL.WTI swap.
# Negative = cost (always a cost for both long and short in practice).
# Applied retroactively at close based on number of nights held.
SWAP_PER_LOT_PER_NIGHT = float(os.environ.get("FRICTION_SWAP_PER_LOT_PER_NIGHT", "0.12"))

# Currency conversion % applied to margin on open and P/L on close.
# XTB PLN accounts pay ~0.5%. Set to 0 if account is USD-denominated.
CURRENCY_CONVERSION_PCT = float(os.environ.get("FRICTION_CURRENCY_CONVERSION_PCT", "0.5"))


# ---------------------------------------------------------------------------
# Public API — called from position_manager.py
# ---------------------------------------------------------------------------


def apply_entry_friction(side: str, mid_price: float) -> tuple[float, dict]:
    """Return the effective entry price after spread + slippage.

    For a LONG, entry is worse (higher) than mid. For a SHORT, entry is
    worse (lower) than mid. The returned detail dict contains each
    component for audit/display.

    Returns (effective_entry, friction_detail).
    """
    spread = SPREAD_HALF_USD
    slippage = random.uniform(0, SLIPPAGE_MAX_USD) if SLIPPAGE_MAX_USD > 0 else 0.0
    slippage = round(slippage, 4)

    if side == "LONG":
        effective = mid_price + spread + slippage
    else:  # SHORT
        effective = mid_price - spread - slippage

    detail = {
        "mid_price": round(mid_price, 5),
        "spread_half": round(spread, 4),
        "slippage": slippage,
        "effective_entry": round(effective, 5),
        "direction": "adverse",
        "side": side,
    }
    logger.info(
        "Entry friction %s: mid=$%.3f → effective=$%.3f (spread=$%.3f, slip=$%.4f)",
        side, mid_price, effective, spread, slippage,
    )
    return round(effective, 5), detail


def apply_exit_friction(side: str, mid_price: float) -> tuple[float, dict]:
    """Return the effective close price after spread + slippage.

    For closing a LONG, the close is worse (lower). For closing a SHORT,
    the close is worse (higher). Mirror of apply_entry_friction.
    """
    spread = SPREAD_HALF_USD
    slippage = random.uniform(0, SLIPPAGE_MAX_USD) if SLIPPAGE_MAX_USD > 0 else 0.0
    slippage = round(slippage, 4)

    if side == "LONG":
        effective = mid_price - spread - slippage
    else:  # SHORT
        effective = mid_price + spread + slippage

    detail = {
        "mid_price": round(mid_price, 5),
        "spread_half": round(spread, 4),
        "slippage": slippage,
        "effective_close": round(effective, 5),
        "direction": "adverse",
        "side": side,
    }
    logger.info(
        "Exit friction %s close: mid=$%.3f → effective=$%.3f (spread=$%.3f, slip=$%.4f)",
        side, mid_price, effective, spread, slippage,
    )
    return round(effective, 5), detail


def compute_holding_costs(
    side: str,
    lots: float,
    margin_used: float,
    opened_at: datetime,
    closed_at: datetime,
) -> tuple[float, dict]:
    """Compute swap fees + currency conversion costs for a closed position.

    Returns (total_cost_usd, detail_dict).
    """
    # Nights held — count rollovers (each midnight UTC is one swap charge)
    # Simplified: integer days between open and close dates. Wednesday
    # typically charges 3x (weekend swap) but we just use calendar days.
    days_held = max(0, (closed_at.date() - opened_at.date()).days)
    nights = days_held  # each day = one overnight charge

    swap_cost = nights * lots * SWAP_PER_LOT_PER_NIGHT if lots > 0 else 0.0

    # Currency conversion: on open (margin → USD) and on close (P/L → PLN).
    # We model it as a flat % of margin charged twice.
    conversion_cost = 0.0
    if CURRENCY_CONVERSION_PCT > 0 and margin_used > 0:
        conversion_cost = margin_used * (CURRENCY_CONVERSION_PCT / 100) * 2  # open + close

    total = round(swap_cost + conversion_cost, 2)
    detail = {
        "nights_held": nights,
        "swap_per_lot_per_night": SWAP_PER_LOT_PER_NIGHT,
        "swap_cost_usd": round(swap_cost, 2),
        "currency_conversion_pct": CURRENCY_CONVERSION_PCT,
        "conversion_cost_usd": round(conversion_cost, 2),
        "total_holding_cost_usd": total,
    }
    return total, detail


def compute_commission(lots: float) -> tuple[float, dict]:
    """Commission for a single side (open or close). Called twice per round trip."""
    cost = round(lots * COMMISSION_PER_LOT, 2) if lots > 0 else 0.0
    return cost, {"lots": lots, "per_lot": COMMISSION_PER_LOT, "total": cost}


def friction_summary() -> dict:
    """Return the current friction config for display/audit."""
    return {
        "spread_half_usd": SPREAD_HALF_USD,
        "spread_round_trip_usd": SPREAD_HALF_USD * 2,
        "slippage_max_usd": SLIPPAGE_MAX_USD,
        "commission_per_lot": COMMISSION_PER_LOT,
        "swap_per_lot_per_night": SWAP_PER_LOT_PER_NIGHT,
        "currency_conversion_pct": CURRENCY_CONVERSION_PCT,
    }

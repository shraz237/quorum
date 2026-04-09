"""Position sizing for DCA campaigns — now with dynamic multipliers.

XTB/Binance CFD specs:
  - Lot size: 100 barrels
  - Leverage: x10
  - 1 lot at price P needs margin = (100 * P) / 10 = 10*P USD

Sizing model (2026-04 overhaul):
  1. A fixed BASE schedule of margin per DCA layer (sums to ~$100k = full
     account usage at max layer 5).
  2. A dynamic `size_multiplier` in [0.5, 3.0] computed from current
     market state (unified score, LLM confidence, volatility regime,
     account drawdown, funding extreme) — applied uniformly across
     every layer of a given campaign.
  3. An equity-cap safety net that refuses to let total open margin
     exceed MAX_TOTAL_EXPOSURE_PCT of current equity, no matter what
     the multiplier says.

The multiplier is computed ONCE at campaign open and stored on the
Campaign row, so every subsequent DCA layer uses the same proportion.
"""
from __future__ import annotations

LEVERAGE = 10
LOT_SIZE_BBL = 100  # 1 lot = 100 barrels

# Base DCA layer margin schedule (pre-multiplier). Sums to ~$100k.
# Front-loaded more aggressively than the legacy [3,6,10,20,30,30] so
# the bot takes meaningful risk on strong signals without having to wait
# for deep drawdowns to build a real position.
DCA_LAYERS_MARGIN_BASE: list[float] = [5000.0, 10000.0, 15000.0, 25000.0, 35000.0, 10000.0]

# Back-compat alias so older code paths that still read DCA_LAYERS_MARGIN
# continue to work (treated as the base schedule, multiplier 1.0).
DCA_LAYERS_MARGIN: list[float] = DCA_LAYERS_MARGIN_BASE

# Size multiplier bounds. At 3.0x on Layer 0 ($5k base) you enter with
# $15k margin = $150k nominal exposure (roughly 1.5x account equity).
# Clamped BELOW by the equity cap so you can't actually overleverage.
MIN_SIZE_MULTIPLIER = 0.5
MAX_SIZE_MULTIPLIER = 3.0

# Hard cap: total open margin (across all open campaigns) must never
# exceed this fraction of current equity. 0.80 = 80% utilisation max,
# leaving 20% buffer for adverse moves before margin call territory.
MAX_TOTAL_EXPOSURE_PCT = 0.80

# Drawdown threshold (% of avg entry) that triggers the next DCA layer.
DCA_DRAWDOWN_TRIGGER_PCT = 5.0

# Hard stop: close the campaign when its unrealised PnL drops below this % of margin.
HARD_STOP_DRAWDOWN_PCT = 50.0


def lots_from_margin(margin_usd: float, price: float) -> float:
    """Convert a margin amount into a number of lots at the given price."""
    nominal = margin_usd * LEVERAGE
    return nominal / (price * LOT_SIZE_BBL)


def margin_for_lots(lots: float, price: float) -> float:
    nominal = lots * LOT_SIZE_BBL * price
    return nominal / LEVERAGE


def nominal_value(lots: float, price: float) -> float:
    return lots * LOT_SIZE_BBL * price


def base_layer_margin(layer_index: int) -> float | None:
    """Return the BASE margin for a layer (pre-multiplier), or None if exhausted."""
    if layer_index >= len(DCA_LAYERS_MARGIN_BASE):
        return None
    return DCA_LAYERS_MARGIN_BASE[layer_index]


def scaled_layer_margin(layer_index: int, multiplier: float) -> float | None:
    """Return the EFFECTIVE margin for a layer after applying the multiplier."""
    base = base_layer_margin(layer_index)
    if base is None:
        return None
    return base * multiplier


def next_layer_margin(layers_used: int, multiplier: float = 1.0) -> float | None:
    """Return the next DCA layer's margin after multiplier, or None if exhausted.

    Back-compat: callers that don't pass a multiplier get the base schedule.
    """
    return scaled_layer_margin(layers_used, multiplier)


def total_planned_margin(multiplier: float = 1.0) -> float:
    return sum(DCA_LAYERS_MARGIN_BASE) * multiplier


def clamp_multiplier(multiplier: float) -> float:
    return max(MIN_SIZE_MULTIPLIER, min(MAX_SIZE_MULTIPLIER, multiplier))

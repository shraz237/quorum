"""Risk tooling: scenario calculator + Monte Carlo margin-call simulator.

Both run on demand against the current account/position state. No DB
writes, no LLM calls — pure numpy math.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import desc

from shared.account_manager import recompute_account_state
from shared.models.base import SessionLocal
from shared.models.ohlcv import OHLCV
from shared.position_manager import list_open_campaigns

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scenario calculator
# ---------------------------------------------------------------------------

def compute_scenarios(price_offsets: list[float] | None = None) -> dict:
    """Given current account + open campaigns, return PnL / equity / margin
    level / risk snapshot at multiple price points.

    Output shape:
    {
      "current_price": 95.73,
      "current_equity": 100248.20,
      "current_margin_used": 69000.00,
      "key_levels": {
        "breakeven": 96.71,
        "stop_out_price": 99.23,      # equity == margin_used → margin call
        "half_drawdown_price": 99.80,  # equity == starting * 0.5 (hard stop)
      },
      "scenarios": [
         {
           "offset_pct": -5.0, "price": 90.94,
           "pnl": +17928, "equity": 117928, "free_margin": 48928,
           "margin_level": 170.9, "drawdown_pct": +17.9, "status": "SAFE"
         },
         ...
      ]
    }
    """
    if price_offsets is None:
        price_offsets = [-5.0, -3.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 3.0, 5.0]

    account = recompute_account_state()
    campaigns = list_open_campaigns()

    cash = account.get("cash") or 0.0
    starting_balance = account.get("starting_balance") or 100_000.0
    current_margin = account.get("margin_used") or 0.0
    current_price = account.get("current_price")  # might not exist
    if current_price is None:
        # Pull from latest 1min bar directly
        with SessionLocal() as session:
            row = (
                session.query(OHLCV)
                .filter(OHLCV.source == "yahoo", OHLCV.timeframe == "1min")
                .order_by(desc(OHLCV.timestamp))
                .first()
            )
            current_price = float(row.close) if row else None

    if current_price is None:
        return {"error": "no current price available"}

    # Build a flat list of (side, lots, entry_price) from every open campaign layer
    layers: list[tuple[str, float, float]] = []
    for c in campaigns:
        side = c.get("side")
        for p in c.get("positions", []):
            lots = p.get("lots") or 0.0
            entry = p.get("entry_price") or 0.0
            if lots > 0 and entry > 0:
                layers.append((side, lots, entry))

    def _pnl_at(price: float) -> float:
        total = 0.0
        for side, lots, entry in layers:
            if side == "LONG":
                total += (price - entry) * lots * 100
            else:
                total += (entry - price) * lots * 100
        return total

    def _snapshot(price: float, offset_pct: float) -> dict:
        pnl = _pnl_at(price)
        equity = cash + pnl
        free_margin = equity - current_margin
        margin_level = (equity / current_margin * 100) if current_margin > 0 else None
        drawdown_pct = ((equity - starting_balance) / starting_balance) * 100

        if margin_level is None:
            status = "NO_POSITION"
        elif margin_level <= 100:
            status = "MARGIN_CALL"
        elif margin_level <= 150:
            status = "DANGER"
        elif margin_level <= 300:
            status = "ELEVATED"
        else:
            status = "SAFE"

        return {
            "offset_pct": round(offset_pct, 2),
            "price": round(price, 3),
            "pnl": round(pnl, 2),
            "equity": round(equity, 2),
            "free_margin": round(free_margin, 2),
            "margin_level_pct": round(margin_level, 1) if margin_level else None,
            "drawdown_pct": round(drawdown_pct, 2),
            "status": status,
        }

    scenarios = [_snapshot(current_price * (1 + o / 100), o) for o in price_offsets]

    # Key price levels (computed analytically for the aggregate SHORT/LONG bias)
    # Assumption: the book is directionally consistent (every layer on one side).
    key_levels: dict = {}
    if layers:
        total_lots = sum(l for _, l, _ in layers)
        weighted_avg_entry = sum(l * e for _, l, e in layers) / total_lots
        side = layers[0][0]
        key_levels["breakeven"] = round(weighted_avg_entry, 3)

        # Stop-out: equity == margin_used → pnl == margin_used - cash
        target_pnl = current_margin - cash
        # For a SHORT: pnl = total_lots * 100 * (avg_entry - price)
        # → price = avg_entry - target_pnl / (total_lots * 100)
        if side == "SHORT":
            stop_out_price = weighted_avg_entry - target_pnl / (total_lots * 100)
        else:
            stop_out_price = weighted_avg_entry + target_pnl / (total_lots * 100)
        key_levels["stop_out_price"] = round(stop_out_price, 3)

        # 50% equity loss: equity == starting * 0.5 → pnl == starting*0.5 - cash
        half_target_pnl = (starting_balance * 0.5) - cash
        if side == "SHORT":
            half_dd_price = weighted_avg_entry - half_target_pnl / (total_lots * 100)
        else:
            half_dd_price = weighted_avg_entry + half_target_pnl / (total_lots * 100)
        key_levels["half_drawdown_price"] = round(half_dd_price, 3)

        # Distance to each in percentage terms
        if current_price > 0:
            key_levels["distance_to_stop_out_pct"] = round(
                (stop_out_price - current_price) / current_price * 100, 2,
            )
            key_levels["distance_to_half_dd_pct"] = round(
                (half_dd_price - current_price) / current_price * 100, 2,
            )

    return {
        "current_price": round(current_price, 3),
        "current_equity": round(cash + _pnl_at(current_price), 2),
        "current_margin_used": round(current_margin, 2),
        "current_cash": round(cash, 2),
        "starting_balance": starting_balance,
        "side_bias": layers[0][0] if layers else None,
        "total_lots": round(sum(l for _, l, _ in layers), 4) if layers else 0,
        "key_levels": key_levels,
        "scenarios": scenarios,
    }


# ---------------------------------------------------------------------------
# Monte Carlo margin call simulator
# ---------------------------------------------------------------------------

def _compute_log_returns_sigma(hours: int = 24 * 7) -> float | None:
    """Rolling 1-hour log-return stdev over the last `hours` hours."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        bars = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "yahoo",
                OHLCV.timeframe == "1H",
                OHLCV.timestamp >= since,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )
    closes = [b.close for b in bars if b.close]
    if len(closes) < 10:
        return None
    arr = np.array(closes)
    rets = np.diff(np.log(arr))
    return float(np.std(rets))  # 1h log-return stdev


def simulate_margin_call(
    horizon_hours: int = 24,
    n_paths: int = 2000,
    lookback_hours: int = 24 * 7,
) -> dict:
    """Geometric Brownian Motion simulation of margin-call probability.

    Uses rolling 1h log-return stdev from last `lookback_hours` as sigma.
    Drift assumed zero (no directional bias). For each simulated path,
    track whether equity drops below margin_used at any hourly step.
    """
    sigma_h = _compute_log_returns_sigma(hours=lookback_hours)
    if sigma_h is None or sigma_h <= 0:
        return {"error": "insufficient history to compute volatility"}

    scenarios_base = compute_scenarios(price_offsets=[0.0])
    if "error" in scenarios_base:
        return scenarios_base

    current_price = scenarios_base["current_price"]
    cash = scenarios_base["current_cash"]
    margin_used = scenarios_base["current_margin_used"]
    starting_balance = scenarios_base["starting_balance"]
    total_lots = scenarios_base["total_lots"]
    side = scenarios_base.get("side_bias")

    if total_lots <= 0 or margin_used <= 0 or side is None:
        return {
            "horizon_hours": horizon_hours,
            "n_paths": n_paths,
            "sigma_hourly": sigma_h,
            "prob_margin_call": 0.0,
            "prob_half_dd": 0.0,
            "expected_equity": cash,
            "p5_equity": cash,
            "p95_equity": cash,
            "note": "no open position",
        }

    # We need the avg entry for PnL computation
    campaigns = list_open_campaigns()
    layers: list[tuple[float, float]] = []
    for c in campaigns:
        if c.get("side") != side:
            continue
        for p in c.get("positions", []):
            lots = p.get("lots") or 0.0
            entry = p.get("entry_price") or 0.0
            if lots > 0 and entry > 0:
                layers.append((lots, entry))

    if not layers:
        return {"error": "no layers found"}

    lot_sum = sum(l for l, _ in layers)
    avg_entry = sum(l * e for l, e in layers) / lot_sum
    direction = 1.0 if side == "LONG" else -1.0

    # GBM paths, hourly steps
    rng = np.random.default_rng()
    shocks = rng.normal(loc=0.0, scale=sigma_h, size=(n_paths, horizon_hours))
    log_price_paths = np.log(current_price) + np.cumsum(shocks, axis=1)
    price_paths = np.exp(log_price_paths)  # shape (n_paths, horizon_hours)

    # PnL at each step: direction * (end - entry) * lots * 100
    pnl_paths = direction * (price_paths - avg_entry) * lot_sum * 100
    equity_paths = cash + pnl_paths  # shape (n_paths, horizon_hours)

    # Margin call = equity <= margin_used at ANY step
    margin_call_hit = (equity_paths <= margin_used).any(axis=1)
    half_dd_hit = (equity_paths <= starting_balance * 0.5).any(axis=1)

    final_equities = equity_paths[:, -1]

    return {
        "horizon_hours": horizon_hours,
        "n_paths": n_paths,
        "sigma_hourly": round(sigma_h, 6),
        "sigma_hourly_pct": round(sigma_h * 100, 3),
        "current_equity": round(scenarios_base["current_equity"], 2),
        "prob_margin_call": round(float(margin_call_hit.mean()) * 100, 2),
        "prob_half_dd": round(float(half_dd_hit.mean()) * 100, 2),
        "expected_equity": round(float(np.mean(final_equities)), 2),
        "p5_equity": round(float(np.percentile(final_equities, 5)), 2),
        "p50_equity": round(float(np.percentile(final_equities, 50)), 2),
        "p95_equity": round(float(np.percentile(final_equities, 95)), 2),
        "worst_equity": round(float(np.min(final_equities)), 2),
        "best_equity": round(float(np.max(final_equities)), 2),
    }

"""Analytics / risk / stats chat-tool plugin.

Read-only tools for support/resistance, VWAP, pivot points, SL/TP
suggestions, stress-testing, correlation, calendar events, system
health and PnL history.  None of these tools mutate any database row.

Integration contract
--------------------
PLUGIN_TOOLS : list[dict]
    Anthropic tool schemas to be merged into the main TOOLS list.

execute(name, tool_input) -> dict | None
    Dispatch a tool by name.  Returns None for unhandled tool names so
    the main dispatcher can fall through to other plugins / built-ins.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anthropic tool schemas
# ---------------------------------------------------------------------------

PLUGIN_TOOLS: list[dict] = [
    # 1 -----------------------------------------------------------------------
    {
        "name": "get_support_resistance",
        "description": (
            "Compute nearest support and resistance levels from recent swing "
            "highs and lows on Brent OHLCV data.  Returns price levels the AI "
            "can use for SL/TP placement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timeframe": {
                    "type": "string",
                    "enum": ["1min", "5min", "15min", "1H", "1D"],
                    "default": "1H",
                },
                "lookback_bars": {"type": "integer", "default": 100},
            },
        },
    },
    # 2 -----------------------------------------------------------------------
    {
        "name": "get_vwap",
        "description": (
            "Compute volume-weighted average price (VWAP) for Brent over the "
            "last N hours on a given timeframe.  Returns VWAP and distance from "
            "current price.  Falls back to simple mean of close when volume is "
            "zero/null (common with Stooq data)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timeframe": {
                    "type": "string",
                    "enum": ["1min", "5min", "15min", "1H"],
                    "default": "1H",
                },
                "hours": {"type": "integer", "default": 24},
            },
        },
    },
    # 3 -----------------------------------------------------------------------
    {
        "name": "get_pivot_points",
        "description": (
            "Compute classic daily pivot points (P, R1, R2, S1, S2) from "
            "yesterday's 1D OHLC bar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # 4 -----------------------------------------------------------------------
    {
        "name": "compute_optimal_sl_tp",
        "description": (
            "Suggest stop-loss and take-profit levels for a given trade "
            "direction and entry price.  Method 'atr' uses ATR(14) on 1H bars; "
            "'sr' uses the nearest support/resistance levels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "side": {"type": "string", "enum": ["LONG", "SHORT"]},
                "entry_price": {"type": "number"},
                "method": {
                    "type": "string",
                    "enum": ["atr", "sr"],
                    "default": "atr",
                },
                "atr_multiplier_sl": {"type": "number", "default": 1.5},
                "atr_multiplier_tp": {"type": "number", "default": 2.5},
            },
            "required": ["side", "entry_price"],
        },
    },
    # 5 -----------------------------------------------------------------------
    {
        "name": "stress_test_campaign",
        "description": (
            "Simulate what happens to an open campaign if Brent price moves by "
            "specific percentages.  Returns PnL and margin level at each "
            "scenario; flags scenarios where margin_level < 200% as margin-call "
            "risk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
                "scenarios_pct": {
                    "type": "array",
                    "items": {"type": "number"},
                    "default": [-5, -10, -15, -20, -30],
                },
            },
            "required": ["campaign_id"],
        },
    },
    # 6 -----------------------------------------------------------------------
    {
        "name": "get_correlation",
        "description": (
            "Compute the Pearson correlation of Brent crude log-returns against "
            "the USD broad index (FRED series DTWEXBGS) over a given window in "
            "hours.  Returns the correlation coefficient and an interpretation "
            "string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "default": 168},
            },
        },
    },
    # 7 -----------------------------------------------------------------------
    {
        "name": "get_upcoming_events",
        "description": (
            "List upcoming oil-relevant macro calendar events (EIA inventory "
            "release, OPEC MOMR, FOMC, etc.) in the next N days."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7},
            },
        },
    },
    # 8 -----------------------------------------------------------------------
    {
        "name": "get_system_health",
        "description": (
            "Return status of the trading bot's data sources and services — "
            "freshness of Yahoo/Stooq/EIA/FRED/marketfeed/sentiment, last "
            "update timestamps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # 9 -----------------------------------------------------------------------
    {
        "name": "get_data_sources_status",
        "description": (
            "Return a per-source status table showing last update, age, "
            "expected cadence and next expected update.  Alias of "
            "get_system_health with a different presentation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # 10 ----------------------------------------------------------------------
    {
        "name": "get_campaign_pnl_history",
        "description": (
            "Return the PnL curve of a campaign over time since open, computed "
            "from 1min OHLCV bars and the layer entry fills."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "integer"},
            },
            "required": ["campaign_id"],
        },
    },
    # 11 ----------------------------------------------------------------------
    {
        "name": "get_llm_cost_today",
        "description": (
            "Estimate today's LLM usage cost (Anthropic + xAI) based on "
            "approximate token counts logged by the services."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def execute(name: str, tool_input: dict) -> dict | None:
    """Dispatch a tool call to its implementation.

    Returns a serialisable dict for handled tools.
    Returns None for unhandled tool names so the main dispatcher can fall
    through to other plugins or built-ins.
    """
    if name == "get_support_resistance":
        return _get_support_resistance(**tool_input)
    if name == "get_vwap":
        return _get_vwap(**tool_input)
    if name == "get_pivot_points":
        return _get_pivot_points()
    if name == "compute_optimal_sl_tp":
        return _compute_optimal_sl_tp(**tool_input)
    if name == "stress_test_campaign":
        return _stress_test_campaign(**tool_input)
    if name == "get_correlation":
        return _get_correlation(**tool_input)
    if name == "get_upcoming_events":
        return _get_upcoming_events(**tool_input)
    if name == "get_system_health":
        return _get_system_health()
    if name == "get_data_sources_status":
        return _get_data_sources_status()
    if name == "get_campaign_pnl_history":
        return _get_campaign_pnl_history(**tool_input)
    if name == "get_llm_cost_today":
        return _get_llm_cost_today()
    # Not handled by this plugin
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_ohlcv_bars(
    timeframe: str,
    limit: int,
    since: datetime | None = None,
) -> list:
    """Return OHLCV rows ordered ascending by timestamp."""
    from shared.models.base import SessionLocal
    from shared.models.ohlcv import OHLCV
    from sqlalchemy import desc

    with SessionLocal() as session:
        q = session.query(OHLCV).filter(OHLCV.timeframe == timeframe)
        if since is not None:
            q = q.filter(OHLCV.timestamp >= since)
        rows = q.order_by(desc(OHLCV.timestamp)).limit(limit).all()
        # Detach objects so they can be used after session closes
        session.expunge_all()
    return list(reversed(rows))


def _compute_atr(bars: list, period: int = 14) -> float | None:
    """Compute ATR(period) from a list of OHLCV bar objects.

    True range = max(H-L, |H-prevC|, |L-prevC|).
    ATR = simple rolling mean of the last `period` true-range values.
    Returns None when there are not enough bars.
    """
    if len(bars) < period + 1:
        return None
    true_ranges: list[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        high = bars[i].high
        low = bars[i].low
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    # Use the last `period` true ranges
    recent_trs = true_ranges[-period:]
    return sum(recent_trs) / len(recent_trs)


def _find_swing_highs_lows(bars: list, window: int = 3) -> tuple[list, list]:
    """Find swing highs and lows using a simple local-extrema scan.

    A bar is a swing high if its high is higher than the `window` bars before
    and `window` bars after it.  Symmetric definition for swing lows.

    Returns (swing_highs, swing_lows) where each element is a dict with
    keys: price, timestamp.
    """
    swing_highs: list[dict] = []
    swing_lows: list[dict] = []

    n = len(bars)
    for i in range(window, n - window):
        bar = bars[i]
        neighbours_high = [bars[j].high for j in range(i - window, i + window + 1) if j != i]
        neighbours_low = [bars[j].low for j in range(i - window, i + window + 1) if j != i]

        if all(bar.high > h for h in neighbours_high):
            swing_highs.append(
                {"price": bar.high, "timestamp": bar.timestamp.isoformat()}
            )
        if all(bar.low < lo for lo in neighbours_low):
            swing_lows.append(
                {"price": bar.low, "timestamp": bar.timestamp.isoformat()}
            )

    return swing_highs, swing_lows


def _pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    """Compute Pearson r between two equal-length lists.  Returns None on error."""
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = math.sqrt(var_x * var_y)
    if denom == 0:
        return None
    return cov / denom


def _interpret_correlation(r: float) -> str:
    abs_r = abs(r)
    sign = "positive" if r >= 0 else "negative"
    if abs_r >= 0.7:
        strength = "strong"
    elif abs_r >= 0.4:
        strength = "moderate"
    elif abs_r >= 0.2:
        strength = "weak"
    else:
        strength = "very weak / negligible"
    return f"{strength} {sign}"


def _age_human(seconds: float) -> str:
    if seconds < 120:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _get_support_resistance(
    timeframe: str = "1H",
    lookback_bars: int = 100,
) -> dict:
    try:
        bars = _fetch_ohlcv_bars(timeframe, lookback_bars)
        if len(bars) < 10:
            return {"error": f"not enough bars for timeframe={timeframe} (got {len(bars)})"}

        current_price = bars[-1].close

        swing_highs, swing_lows = _find_swing_highs_lows(bars, window=3)

        # Resistances: swing highs above current price, sorted ascending
        resistances = sorted(
            [s for s in swing_highs if s["price"] > current_price],
            key=lambda s: s["price"],
        )[:3]

        # Supports: swing lows below current price, sorted descending (nearest first)
        supports = sorted(
            [s for s in swing_lows if s["price"] < current_price],
            key=lambda s: s["price"],
            reverse=True,
        )[:3]

        return {
            "timeframe": timeframe,
            "lookback_bars": lookback_bars,
            "current_price": round(current_price, 3),
            "supports": [
                {"price": round(s["price"], 3), "timestamp": s["timestamp"]}
                for s in supports
            ],
            "resistances": [
                {"price": round(r["price"], 3), "timestamp": r["timestamp"]}
                for r in resistances
            ],
            "note": (
                "Swing high/low defined as local max/min with 3-bar lookback "
                "on each side.  Use as approximate zones, not exact levels."
            ),
        }
    except Exception as exc:
        logger.exception("get_support_resistance failed")
        return {"error": str(exc)}


def _get_vwap(timeframe: str = "1H", hours: int = 24) -> dict:
    try:
        since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        bars = _fetch_ohlcv_bars(timeframe, limit=10_000, since=since)
        if not bars:
            return {"error": f"no bars found for timeframe={timeframe} in last {hours}h"}

        # Typical price = (H + L + C) / 3
        typical_prices = [(b.high + b.low + b.close) / 3 for b in bars]
        volumes = [b.volume if b.volume else 0.0 for b in bars]

        total_vol = sum(volumes)
        used_simple_avg = False

        if total_vol > 0:
            vwap = sum(tp * v for tp, v in zip(typical_prices, volumes)) / total_vol
        else:
            # Fall back to simple mean of close (Stooq has no volume)
            vwap = sum(b.close for b in bars) / len(bars)
            used_simple_avg = True

        current_price = bars[-1].close
        distance = current_price - vwap
        distance_pct = (distance / vwap) * 100 if vwap else None

        result: dict = {
            "timeframe": timeframe,
            "hours": hours,
            "bar_count": len(bars),
            "vwap": round(vwap, 3),
            "current_price": round(current_price, 3),
            "distance_points": round(distance, 3),
            "distance_pct": round(distance_pct, 3) if distance_pct is not None else None,
            "price_vs_vwap": "above" if distance > 0 else "below",
        }
        if used_simple_avg:
            result["fallback"] = (
                "Volume data was zero/null (Stooq source); VWAP computed as "
                "simple mean of close prices instead."
            )
        return result
    except Exception as exc:
        logger.exception("get_vwap failed")
        return {"error": str(exc)}


def _get_pivot_points() -> dict:
    try:
        # Fetch the last 2 daily bars; [0] = two days ago, [1] = yesterday
        bars = _fetch_ohlcv_bars("1D", limit=2)
        if len(bars) < 2:
            return {"error": "need at least 2 daily bars to compute pivot points"}

        prev = bars[-2]  # yesterday's bar
        today = bars[-1]

        H = prev.high
        L = prev.low
        C = prev.close

        P = (H + L + C) / 3
        R1 = 2 * P - L
        S1 = 2 * P - H
        R2 = P + (H - L)
        S2 = P - (H - L)

        current_price = today.close

        return {
            "based_on_date": prev.timestamp.isoformat(),
            "prev_high": round(H, 3),
            "prev_low": round(L, 3),
            "prev_close": round(C, 3),
            "P": round(P, 3),
            "R1": round(R1, 3),
            "R2": round(R2, 3),
            "S1": round(S1, 3),
            "S2": round(S2, 3),
            "current_price": round(current_price, 3),
            "position": (
                "above_P" if current_price > P
                else "at_P" if abs(current_price - P) < 0.05
                else "below_P"
            ),
        }
    except Exception as exc:
        logger.exception("get_pivot_points failed")
        return {"error": str(exc)}


def _compute_optimal_sl_tp(
    side: str,
    entry_price: float,
    method: str = "atr",
    atr_multiplier_sl: float = 1.5,
    atr_multiplier_tp: float = 2.5,
) -> dict:
    try:
        side = side.upper()
        if side not in ("LONG", "SHORT"):
            return {"error": f"invalid side: {side}"}

        if method == "atr":
            bars = _fetch_ohlcv_bars("1H", limit=50)
            if len(bars) < 15:
                return {"error": f"not enough 1H bars for ATR (got {len(bars)})"}

            atr = _compute_atr(bars, period=14)
            if atr is None:
                return {"error": "could not compute ATR(14) — insufficient data"}

            if side == "LONG":
                sl = entry_price - atr_multiplier_sl * atr
                tp = entry_price + atr_multiplier_tp * atr
            else:  # SHORT
                sl = entry_price + atr_multiplier_sl * atr
                tp = entry_price - atr_multiplier_tp * atr

            risk_points = abs(entry_price - sl)
            reward_points = abs(tp - entry_price)

            return {
                "method": "atr",
                "side": side,
                "entry_price": round(entry_price, 3),
                "atr": round(atr, 3),
                "atr_multiplier_sl": atr_multiplier_sl,
                "atr_multiplier_tp": atr_multiplier_tp,
                "sl": round(sl, 3),
                "tp": round(tp, 3),
                "risk_points": round(risk_points, 3),
                "reward_points": round(reward_points, 3),
                "rr_ratio": round(reward_points / risk_points, 2) if risk_points else None,
            }

        elif method == "sr":
            sr = _get_support_resistance(timeframe="1H", lookback_bars=100)
            if "error" in sr:
                return sr

            supports = sr.get("supports", [])
            resistances = sr.get("resistances", [])

            if side == "LONG":
                # SL at nearest support below entry, TP at nearest resistance above entry
                below = [s for s in supports if s["price"] < entry_price]
                above = [r for r in resistances if r["price"] > entry_price]
                if not below:
                    return {"error": "no support level found below entry price"}
                if not above:
                    return {"error": "no resistance level found above entry price"}
                sl = below[0]["price"]   # nearest support
                tp = above[0]["price"]   # nearest resistance
            else:  # SHORT
                # SL at nearest resistance above entry, TP at nearest support below entry
                above = [r for r in resistances if r["price"] > entry_price]
                below = [s for s in supports if s["price"] < entry_price]
                if not above:
                    return {"error": "no resistance level found above entry price"}
                if not below:
                    return {"error": "no support level found below entry price"}
                sl = above[0]["price"]
                tp = below[0]["price"]

            risk_points = abs(entry_price - sl)
            reward_points = abs(tp - entry_price)

            return {
                "method": "sr",
                "side": side,
                "entry_price": round(entry_price, 3),
                "atr": None,
                "sl": round(sl, 3),
                "tp": round(tp, 3),
                "risk_points": round(risk_points, 3),
                "reward_points": round(reward_points, 3),
                "rr_ratio": round(reward_points / risk_points, 2) if risk_points else None,
            }

        else:
            return {"error": f"unknown method: {method}. Use 'atr' or 'sr'."}

    except Exception as exc:
        logger.exception("compute_optimal_sl_tp failed")
        return {"error": str(exc)}


def _stress_test_campaign(
    campaign_id: int,
    scenarios_pct: list[float] | None = None,
) -> dict:
    if scenarios_pct is None:
        scenarios_pct = [-5, -10, -15, -20, -30]

    try:
        from shared.models.base import SessionLocal
        from shared.models.campaigns import Campaign
        from shared.models.positions import Position
        from shared.account_manager import recompute_account_state
        from shared.position_manager import get_current_price

        current_price = get_current_price()
        if current_price is None:
            return {"error": "no current price available"}

        with SessionLocal() as session:
            campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
            if campaign is None:
                return {"error": f"campaign {campaign_id} not found"}
            if campaign.status != "open":
                return {"error": f"campaign {campaign_id} is not open (status={campaign.status})"}

            positions = (
                session.query(Position)
                .filter(Position.campaign_id == campaign_id, Position.status == "open")
                .all()
            )
            if not positions:
                return {"error": f"no open positions found for campaign {campaign_id}"}

            # Snapshot needed data before session closes
            pos_data = [
                {
                    "entry_price": p.entry_price,
                    "lots": p.lots or 0.0,
                    "margin_used": p.margin_used or 0.0,
                    "side": p.side,
                }
                for p in positions
            ]
            session.expunge_all()

        total_margin_used = sum(p["margin_used"] for p in pos_data)
        account = recompute_account_state()
        cash = account["cash"]

        # Current unrealised PnL (at current_price)
        def _unrealised(price: float, pos: dict) -> float:
            sign = 1 if pos["side"] == "LONG" else -1
            return (price - pos["entry_price"]) * pos["lots"] * 100 * sign

        current_pnl = sum(_unrealised(current_price, p) for p in pos_data)
        current_equity = cash + current_pnl + total_margin_used

        scenarios: list[dict] = []
        for pct in scenarios_pct:
            hypo_price = current_price * (1 + pct / 100)
            hypo_pnl = sum(_unrealised(hypo_price, p) for p in pos_data)
            # equity = cash (uninvested cash) + margin reserved + hypo_pnl on open book
            hypo_equity = cash + total_margin_used + hypo_pnl
            margin_level = (hypo_equity / total_margin_used * 100) if total_margin_used else None
            pnl_change = hypo_pnl - current_pnl

            scenarios.append(
                {
                    "scenario_pct": pct,
                    "hypothetical_price": round(hypo_price, 3),
                    "unrealised_pnl": round(hypo_pnl, 2),
                    "pnl_change_vs_now": round(pnl_change, 2),
                    "equity": round(hypo_equity, 2),
                    "margin_level_pct": round(margin_level, 1) if margin_level is not None else None,
                    "margin_call_risk": (margin_level is not None and margin_level < 200),
                }
            )

        return {
            "campaign_id": campaign_id,
            "campaign_side": pos_data[0]["side"] if pos_data else None,
            "layers": len(pos_data),
            "current_price": round(current_price, 3),
            "current_unrealised_pnl": round(current_pnl, 2),
            "current_equity": round(current_equity, 2),
            "total_margin_used": round(total_margin_used, 2),
            "scenarios": scenarios,
            "note": (
                "margin_level < 200% is flagged as margin_call_risk.  "
                "Actual broker thresholds may differ."
            ),
        }
    except Exception as exc:
        logger.exception("stress_test_campaign failed")
        return {"error": str(exc)}


def _get_correlation(hours: int = 168) -> dict:
    try:
        from shared.models.base import SessionLocal
        from shared.models.ohlcv import OHLCV
        from shared.models.macro import MacroFRED
        from sqlalchemy import desc

        since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)

        with SessionLocal() as session:
            brent_rows = (
                session.query(OHLCV)
                .filter(OHLCV.timeframe == "1H", OHLCV.timestamp >= since)
                .order_by(OHLCV.timestamp)
                .all()
            )

            # FRED DTWEXBGS is a daily series; use daily granularity for alignment
            dxy_rows = (
                session.query(MacroFRED)
                .filter(
                    MacroFRED.series_id == "DTWEXBGS",
                    MacroFRED.timestamp >= since,
                )
                .order_by(MacroFRED.timestamp)
                .all()
            )
            session.expunge_all()

        if len(brent_rows) < 4:
            return {"error": f"insufficient Brent 1H bars (got {len(brent_rows)}) for last {hours}h"}
        if len(dxy_rows) < 4:
            return {
                "error": (
                    f"insufficient DXY (DTWEXBGS) data in FRED table "
                    f"(got {len(dxy_rows)}) for last {hours}h"
                )
            }

        # Build date → value maps for daily alignment
        brent_by_date: dict[str, list[float]] = {}
        for r in brent_rows:
            d = r.timestamp.strftime("%Y-%m-%d")
            brent_by_date.setdefault(d, []).append(r.close)

        # Daily Brent close = last bar of that day
        brent_daily: dict[str, float] = {
            d: closes[-1] for d, closes in brent_by_date.items()
        }

        dxy_daily: dict[str, float] = {}
        for r in dxy_rows:
            d = r.timestamp.strftime("%Y-%m-%d")
            if r.value is not None:
                dxy_daily[d] = r.value

        # Intersect dates
        common_dates = sorted(set(brent_daily) & set(dxy_daily))
        if len(common_dates) < 4:
            return {
                "error": (
                    f"only {len(common_dates)} dates with data in both Brent "
                    f"and DXY series; need at least 4"
                )
            }

        brent_vals = [brent_daily[d] for d in common_dates]
        dxy_vals = [dxy_daily[d] for d in common_dates]

        # Log returns
        def _log_returns(prices: list[float]) -> list[float]:
            return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]

        brent_ret = _log_returns(brent_vals)
        dxy_ret = _log_returns(dxy_vals)

        r = _pearson_correlation(brent_ret, dxy_ret)
        if r is None:
            return {"error": "could not compute correlation (degenerate data)"}

        return {
            "window_hours": hours,
            "common_days": len(common_dates),
            "brent_bars": len(brent_rows),
            "dxy_observations": len(dxy_rows),
            "pearson_r": round(r, 4),
            "interpretation": _interpret_correlation(r),
            "note": (
                "Brent prices aggregated to daily close; DXY from FRED DTWEXBGS. "
                "Log returns used for stationarity."
            ),
        }
    except Exception as exc:
        logger.exception("get_correlation failed")
        return {"error": str(exc)}


def _get_upcoming_events(days: int = 7) -> dict:
    """Return upcoming oil-relevant calendar events using a hardcoded schedule."""
    try:
        now = datetime.now(tz=timezone.utc)
        end = now + timedelta(days=days)
        events: list[dict] = []

        # Scan each day in window
        cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= end:
            weekday = cursor.weekday()  # 0=Mon … 6=Sun

            # --- EIA Weekly Petroleum Status Report ---
            # Every Wednesday at 14:30 UTC (moves to Thursday during US holidays,
            # but we use a fixed schedule for the hardcoded version)
            if weekday == 2:  # Wednesday
                event_dt = cursor.replace(hour=14, minute=30)
                if now <= event_dt <= end:
                    events.append(
                        {
                            "date": event_dt.strftime("%Y-%m-%d"),
                            "time_utc": "14:30",
                            "event": "EIA Weekly Petroleum Status Report",
                            "importance": "HIGH",
                            "note": (
                                "Crude oil, gasoline and distillate inventory "
                                "changes.  Major mover for Brent/WTI."
                            ),
                        }
                    )

            # --- FOMC Meeting decisions (8 per year, roughly quarterly) ---
            # Approximate fixed dates: Jan/Mar/May/Jun/Jul/Sep/Oct/Dec
            # We detect them by checking if the current month has a FOMC meeting
            # on the Wednesday of the 3rd full week (conventional schedule).
            # Simplified: flag the 2nd Wednesday of Jan, Mar, May, Jul, Sep, Nov.
            FOMC_MONTHS = {1, 3, 5, 7, 9, 11}
            if weekday == 2 and cursor.month in FOMC_MONTHS:
                # Roughly the 2nd Wednesday (between day 8 and 14)
                if 8 <= cursor.day <= 14:
                    event_dt = cursor.replace(hour=18, minute=0)
                    if now <= event_dt <= end:
                        events.append(
                            {
                                "date": event_dt.strftime("%Y-%m-%d"),
                                "time_utc": "18:00",
                                "event": "FOMC Rate Decision",
                                "importance": "HIGH",
                                "note": (
                                    "Federal Reserve interest rate decision.  "
                                    "Strong USD impact on oil prices."
                                ),
                            }
                        )

            # --- OPEC Monthly Oil Market Report (MOMR) ---
            # Released around the 10th–15th of each month
            if 10 <= cursor.day <= 15 and weekday < 5:  # weekday
                # Fire only once per month — on the 12th or, if weekend, next weekday
                if cursor.day == 12 or (cursor.day == 10 and weekday == 0):
                    event_dt = cursor.replace(hour=9, minute=0)
                    if now <= event_dt <= end:
                        events.append(
                            {
                                "date": event_dt.strftime("%Y-%m-%d"),
                                "time_utc": "09:00",
                                "event": "OPEC Monthly Oil Market Report (MOMR)",
                                "importance": "HIGH",
                                "note": (
                                    "OPEC demand/supply outlook and production "
                                    "data.  Key sentiment driver for Brent."
                                ),
                            }
                        )

            # --- IEA Oil Market Report ---
            # Released around the 13th–15th of each month
            if cursor.day == 14 and weekday < 5:
                event_dt = cursor.replace(hour=9, minute=0)
                if now <= event_dt <= end:
                    events.append(
                        {
                            "date": event_dt.strftime("%Y-%m-%d"),
                            "time_utc": "09:00",
                            "event": "IEA Oil Market Report",
                            "importance": "MEDIUM",
                            "note": (
                                "International Energy Agency monthly outlook "
                                "on supply, demand and stocks."
                            ),
                        }
                    )

            cursor += timedelta(days=1)

        # Sort by date
        events.sort(key=lambda e: (e["date"], e["time_utc"]))

        return {
            "window_days": days,
            "from": now.strftime("%Y-%m-%d %H:%M UTC"),
            "to": end.strftime("%Y-%m-%d %H:%M UTC"),
            "event_count": len(events),
            "events": events,
            "note": (
                "Schedule is hardcoded / approximate.  EIA always Wednesday "
                "14:30 UTC unless US holiday.  FOMC/OPEC/IEA dates are "
                "estimated; verify against official calendars."
            ),
        }
    except Exception as exc:
        logger.exception("get_upcoming_events failed")
        return {"error": str(exc)}


def _get_system_health() -> dict:
    """Query MAX(timestamp) for each data source and report freshness."""
    try:
        from shared.models.base import SessionLocal
        from shared.models.ohlcv import OHLCV
        from shared.models.macro import MacroEIA, MacroFRED
        from shared.models.knowledge import KnowledgeSummary
        from shared.models.sentiment import SentimentNews, SentimentTwitter
        from sqlalchemy import func

        now = datetime.now(tz=timezone.utc)

        def _stale_thresh(seconds: float) -> str:
            """Return 'fresh', 'stale' or 'missing' given age in seconds."""
            if seconds is None:
                return "missing"
            if seconds < 600:  # 10 min
                return "fresh"
            return "stale"

        with SessionLocal() as session:
            # OHLCV per source
            ohlcv_sources: dict[str, datetime | None] = {}
            for src in ("yahoo",):
                ts = session.query(func.max(OHLCV.timestamp)).filter(
                    OHLCV.source == src, OHLCV.timeframe == "1min"
                ).scalar()
                ohlcv_sources[src] = ts

            eia_ts = session.query(func.max(MacroEIA.timestamp)).scalar()
            fred_ts = session.query(func.max(MacroFRED.timestamp)).scalar()

            # KnowledgeSummary per source
            mf_ts = session.query(func.max(KnowledgeSummary.timestamp)).filter(
                KnowledgeSummary.source == "@marketfeed"
            ).scalar()
            sentiment_rss_ts = session.query(func.max(SentimentNews.timestamp)).scalar()
            twitter_ts = session.query(func.max(SentimentTwitter.timestamp)).scalar()

        def _entry(label: str, ts: datetime | None, stale_sec: int) -> dict:
            if ts is None:
                return {
                    "source": label,
                    "last_update": None,
                    "age_seconds": None,
                    "status": "missing",
                }
            # Make timezone-aware if naive
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (now - ts).total_seconds()
            status = "fresh" if age < stale_sec else "stale"
            return {
                "source": label,
                "last_update": ts.isoformat(),
                "age_seconds": int(age),
                "age_human": _age_human(age),
                "status": status,
            }

        return {
            "checked_at": now.isoformat(),
            "sources": {
                "yahoo_1min": _entry("yahoo", ohlcv_sources.get("yahoo"), 300),
                "eia": _entry("eia", eia_ts, 7 * 86400 + 3600),  # weekly + 1h grace
                "fred": _entry("fred", fred_ts, 86400 + 3600),   # daily + 1h grace
                "marketfeed": _entry("marketfeed", mf_ts, 600),
                "sentiment_rss": _entry("sentiment_rss", sentiment_rss_ts, 3600),
                "sentiment_twitter": _entry("sentiment_twitter", twitter_ts, 1800),
            },
        }
    except Exception as exc:
        logger.exception("get_system_health failed")
        return {"error": str(exc)}


def _get_data_sources_status() -> dict:
    """Per-source status table with cadence information."""
    try:
        health = _get_system_health()
        if "error" in health:
            return health

        now_dt = datetime.now(tz=timezone.utc)

        # Expected cadence metadata
        cadences: dict[str, dict] = {
            "yahoo_1min": {
                "expected_cadence": "1min",
                "stale_after_seconds": 300,
                "next_calc": lambda age: max(0, 60 - age),
            },
            "eia": {
                "expected_cadence": "weekly (Wed ~14:30 UTC)",
                "stale_after_seconds": 7 * 86400 + 3600,
                "next_calc": lambda age: max(0, 7 * 86400 - age),
            },
            "fred": {
                "expected_cadence": "daily",
                "stale_after_seconds": 86400 + 3600,
                "next_calc": lambda age: max(0, 86400 - age),
            },
            "marketfeed": {
                "expected_cadence": "5min",
                "stale_after_seconds": 600,
                "next_calc": lambda age: max(0, 300 - age),
            },
            "sentiment_rss": {
                "expected_cadence": "30min",
                "stale_after_seconds": 3600,
                "next_calc": lambda age: max(0, 1800 - age),
            },
            "sentiment_twitter": {
                "expected_cadence": "15min",
                "stale_after_seconds": 1800,
                "next_calc": lambda age: max(0, 900 - age),
            },
        }

        rows: list[dict] = []
        for key, src in health["sources"].items():
            cadence_meta = cadences.get(key, {})
            age_sec = src.get("age_seconds")
            next_in = None
            if age_sec is not None and "next_calc" in cadence_meta:
                next_sec = cadence_meta["next_calc"](age_sec)
                next_dt = now_dt + timedelta(seconds=next_sec)
                next_in = next_dt.isoformat()

            rows.append(
                {
                    "source": key,
                    "last_update_iso": src.get("last_update"),
                    "age_human": src.get("age_human"),
                    "status": src.get("status"),
                    "expected_cadence": cadence_meta.get("expected_cadence"),
                    "next_expected_at": next_in,
                }
            )

        return {
            "checked_at": health["checked_at"],
            "sources": rows,
        }
    except Exception as exc:
        logger.exception("get_data_sources_status failed")
        return {"error": str(exc)}


def _get_campaign_pnl_history(campaign_id: int) -> dict:
    """Compute PnL curve from 1min bars since campaign open."""
    try:
        from shared.models.base import SessionLocal
        from shared.models.campaigns import Campaign
        from shared.models.positions import Position
        from shared.models.ohlcv import OHLCV
        from sqlalchemy import desc

        with SessionLocal() as session:
            campaign = session.query(Campaign).filter(Campaign.id == campaign_id).first()
            if campaign is None:
                return {"error": f"campaign {campaign_id} not found"}

            opened_at = campaign.opened_at
            closed_at = campaign.closed_at  # None if still open
            side = campaign.side

            positions = (
                session.query(Position)
                .filter(Position.campaign_id == campaign_id)
                .order_by(Position.opened_at)
                .all()
            )
            if not positions:
                return {"error": f"no positions found for campaign {campaign_id}"}

            pos_data = [
                {
                    "entry_price": p.entry_price,
                    "lots": p.lots or 0.0,
                    "side": p.side,
                    "opened_at": p.opened_at,
                }
                for p in positions
            ]

            # Fetch 1min bars from campaign open until now (or close)
            end_ts = closed_at or datetime.now(tz=timezone.utc)
            # Limit to 10 000 bars to avoid memory issues (≈ 7 days of 1min)
            bars = (
                session.query(OHLCV)
                .filter(
                    OHLCV.timeframe == "1min",
                    OHLCV.timestamp >= opened_at,
                    OHLCV.timestamp <= end_ts,
                )
                .order_by(OHLCV.timestamp)
                .limit(10_000)
                .all()
            )
            session.expunge_all()

        if not bars:
            return {
                "error": (
                    "no 1min bars found for this campaign's timespan "
                    f"(from {opened_at.isoformat()})"
                )
            }

        curve: list[dict] = []
        for bar in bars:
            bar_ts = bar.timestamp
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.replace(tzinfo=timezone.utc)

            # Only count positions open by this bar's timestamp
            active_positions = [
                p for p in pos_data
                if p["opened_at"].replace(tzinfo=timezone.utc) <= bar_ts
            ]

            pnl = 0.0
            for p in active_positions:
                sign = 1 if p["side"] == "LONG" else -1
                pnl += (bar.close - p["entry_price"]) * p["lots"] * 100 * sign

            curve.append(
                {
                    "timestamp": bar_ts.isoformat(),
                    "pnl": round(pnl, 2),
                    "price": bar.close,
                }
            )

        # Summary stats
        pnl_values = [c["pnl"] for c in curve]
        return {
            "campaign_id": campaign_id,
            "side": side,
            "opened_at": opened_at.isoformat(),
            "closed_at": closed_at.isoformat() if closed_at else None,
            "bar_count": len(curve),
            "max_pnl": round(max(pnl_values), 2) if pnl_values else None,
            "min_pnl": round(min(pnl_values), 2) if pnl_values else None,
            "final_pnl": round(pnl_values[-1], 2) if pnl_values else None,
            "curve": curve,
        }
    except Exception as exc:
        logger.exception("get_campaign_pnl_history failed")
        return {"error": str(exc)}


def _get_llm_cost_today() -> dict:
    """Placeholder — token counting is not yet instrumented."""
    return {
        "status": "not_tracked",
        "note": (
            "Token counting is not instrumented yet.  "
            "Estimated Haiku calls per scrape cycle: ~2.  "
            "Estimated Opus calls per analysis cycle: ~1.  "
            "Run for 24 h to see empirical numbers on "
            "https://console.anthropic.com/usage"
        ),
    }

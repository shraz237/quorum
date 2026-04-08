"""Technical indicator scoring for Brent crude oil analysis."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Timeframe weights for multi-timeframe aggregation.
# The analyzer currently computes scores for 1H, 1D, and 1W only
# (no 5min/15min/4H timeframes from the collector).
# Weight 1H heavily for responsiveness in the 15-min trading cycle.
TIMEFRAME_WEIGHTS: dict[str, float] = {
    "1H": 0.55,
    "1D": 0.35,
    "1W": 0.10,
}


def _clamp(value: float, lo: float = -100.0, hi: float = 100.0) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Individual indicator scorers
# ---------------------------------------------------------------------------


def score_rsi(rsi_value: float) -> float:
    """Convert RSI to a sentiment score in [-100, 100].

    Below 30 = oversold = bullish (+60 to +100).
    Above 70 = overbought = bearish (-60 to -100).
    Linear interpolation between 30 and 70 (neutral zone → near 0).
    """
    if rsi_value <= 30:
        # Linearly map [0, 30] → [+100, +60]
        score = 100.0 - (rsi_value / 30.0) * 40.0
    elif rsi_value >= 70:
        # Linearly map [70, 100] → [-60, -100]
        score = -60.0 - ((rsi_value - 70.0) / 30.0) * 40.0
    else:
        # Neutral zone: linearly map [30, 70] → [+60, -60]
        score = 60.0 - ((rsi_value - 30.0) / 40.0) * 120.0
    return _clamp(score)


def score_macd(macd_line: float, signal_line: float, histogram: float) -> float:
    """Score MACD based on histogram direction and crossover signal.

    Positive histogram = bullish momentum.
    Negative histogram = bearish momentum.
    Crossover (macd crossing signal) amplifies the signal.
    """
    # Base score from histogram magnitude (scale: ±50 points per unit)
    base = _clamp(histogram * 50.0, -60.0, 60.0)

    # Crossover bonus: +/- 40 points when macd and signal are on same side
    if macd_line > signal_line:
        crossover_bonus = 40.0
    elif macd_line < signal_line:
        crossover_bonus = -40.0
    else:
        crossover_bonus = 0.0

    return _clamp(base + crossover_bonus)


def score_ma_crossover(short_ma: float, long_ma: float, price: float) -> float:
    """Score based on short vs long MA relationship and price vs long MA.

    Short MA above long MA = bullish (golden cross).
    Price above long MA adds confirmation.
    """
    score = 0.0

    # MA relationship: ±50 points
    if long_ma > 0:
        ma_diff_pct = (short_ma - long_ma) / long_ma * 100.0
        score += _clamp(ma_diff_pct * 10.0, -50.0, 50.0)

    # Price vs long MA: ±50 points
    if long_ma > 0:
        price_diff_pct = (price - long_ma) / long_ma * 100.0
        score += _clamp(price_diff_pct * 10.0, -50.0, 50.0)

    return _clamp(score)


def score_bollinger(price: float, upper: float, lower: float, mid: float) -> float:
    """Score based on price position within Bollinger Bands.

    Price at lower band = oversold = bullish (+100).
    Price at upper band = overbought = bearish (-100).
    Price at mid = neutral (0).
    """
    band_width = upper - lower
    if band_width <= 0:
        return 0.0

    # Position: 0 = at lower band, 0.5 = at mid, 1 = at upper band
    position = (price - lower) / band_width
    # Map [0, 1] → [+100, -100]
    score = 100.0 - position * 200.0
    return _clamp(score)


def aggregate_technical(scores_dict: dict[str, float | None], adx: float | None = None) -> float:
    """Weighted aggregate of individual indicator scores, regime-adjusted by ADX.

    ADX regime logic:
      adx > 25 → Trend regime: momentum indicators (MACD, MA) dominate.
      adx < 20 → Chop regime: mean-reversion indicators (RSI, BB) dominate.
      otherwise → Equal / unknown weights.

    Ignores None values and renormalises weights accordingly.
    """
    if adx is not None and adx > 25:
        # Trend regime: momentum dominates
        rsi_w, macd_w, ma_w, bb_w = 0.10, 0.35, 0.40, 0.15
    elif adx is not None and adx < 20:
        # Chop regime: mean-reversion dominates
        rsi_w, macd_w, ma_w, bb_w = 0.35, 0.15, 0.10, 0.40
    else:
        # Mixed / unknown: equal weights
        rsi_w, macd_w, ma_w, bb_w = 0.25, 0.25, 0.25, 0.25

    weights: dict[str, float] = {
        "rsi": rsi_w,
        "macd": macd_w,
        "ma_cross": ma_w,
        "bbands": bb_w,
    }

    total_weight = 0.0
    weighted_sum = 0.0
    for key, weight in weights.items():
        val = scores_dict.get(key)
        if val is not None:
            weighted_sum += val * weight
            total_weight += weight

    if total_weight == 0:
        return 0.0

    return _clamp(weighted_sum / total_weight)


def compute_adx(df: pd.DataFrame, length: int = 14) -> float | None:
    """Return latest ADX value (0-100). Higher = stronger trend.

    Uses pandas-ta adx() which returns a DataFrame with an ADX_<length> column.
    Returns None if insufficient data or computation fails.
    """
    try:
        import pandas_ta as ta  # type: ignore[import]
    except ImportError:
        return None
    try:
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=length)
        if adx_df is None or adx_df.empty:
            return None
        # pandas-ta returns a DataFrame with ADX_14 column
        col = next((c for c in adx_df.columns if c.startswith("ADX_")), None)
        if col is None:
            return None
        val = adx_df[col].iloc[-1]
        if np.isnan(val):
            return None
        return float(val)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_ohlcv_dataframe(timeframe: str, limit: int = 200) -> pd.DataFrame:
    """Load OHLCV rows for *timeframe* from the DB into a pandas DataFrame.

    Returns a DataFrame with columns: timestamp, open, high, low, close, volume.
    Sorted ascending by timestamp.
    """
    from shared.models import SessionLocal, OHLCV
    from sqlalchemy import select

    session = SessionLocal()
    try:
        stmt = (
            select(OHLCV)
            .where(OHLCV.timeframe == timeframe)
            .order_by(OHLCV.timestamp.desc())
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
    finally:
        session.close()

    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    data = [
        {
            "timestamp": r.timestamp,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        }
        for r in reversed(rows)  # ascending order
    ]
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp")
    return df


# ---------------------------------------------------------------------------
# Indicator computation
# ---------------------------------------------------------------------------


def _compute_indicators_for_df(df: pd.DataFrame) -> float | None:
    """Run pandas-ta indicators on *df* and return an aggregate score."""
    if len(df) < 20:
        logger.warning("Not enough rows (%d) to compute indicators", len(df))
        return None

    try:
        import pandas_ta as ta  # type: ignore[import]
    except ImportError:
        logger.error("pandas-ta not installed")
        return None

    scores: dict[str, float | None] = {}

    # RSI (14)
    try:
        rsi_series = ta.rsi(df["close"], length=14)
        if rsi_series is not None and not rsi_series.empty:
            rsi_val = rsi_series.iloc[-1]
            if not np.isnan(rsi_val):
                scores["rsi"] = score_rsi(float(rsi_val))
    except Exception:
        logger.exception("RSI computation failed")

    # MACD (12, 26, 9)
    try:
        macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd_df is not None and not macd_df.empty:
            macd_col = [c for c in macd_df.columns if c.startswith("MACD_") and "s" not in c.lower() and "h" not in c.lower()]
            signal_col = [c for c in macd_df.columns if "MACDs_" in c]
            hist_col = [c for c in macd_df.columns if "MACDh_" in c]
            if macd_col and signal_col and hist_col:
                ml = float(macd_df[macd_col[0]].iloc[-1])
                sl = float(macd_df[signal_col[0]].iloc[-1])
                hl = float(macd_df[hist_col[0]].iloc[-1])
                if not any(np.isnan(v) for v in [ml, sl, hl]):
                    scores["macd"] = score_macd(ml, sl, hl)
    except Exception:
        logger.exception("MACD computation failed")

    # Moving averages (20, 50)
    try:
        ma20 = ta.sma(df["close"], length=20)
        ma50 = ta.sma(df["close"], length=50)
        if ma20 is not None and ma50 is not None:
            short_val = float(ma20.iloc[-1])
            long_val = float(ma50.iloc[-1])
            price_val = float(df["close"].iloc[-1])
            if not any(np.isnan(v) for v in [short_val, long_val, price_val]):
                scores["ma_cross"] = score_ma_crossover(short_val, long_val, price_val)
    except Exception:
        logger.exception("MA crossover computation failed")

    # Bollinger Bands (20, 2)
    try:
        bbands_df = ta.bbands(df["close"], length=20, std=2.0)
        if bbands_df is not None and not bbands_df.empty:
            lower_col = [c for c in bbands_df.columns if c.startswith("BBL_")]
            mid_col = [c for c in bbands_df.columns if c.startswith("BBM_")]
            upper_col = [c for c in bbands_df.columns if c.startswith("BBU_")]
            if lower_col and mid_col and upper_col:
                lower = float(bbands_df[lower_col[0]].iloc[-1])
                mid = float(bbands_df[mid_col[0]].iloc[-1])
                upper = float(bbands_df[upper_col[0]].iloc[-1])
                price_val = float(df["close"].iloc[-1])
                if not any(np.isnan(v) for v in [lower, mid, upper, price_val]):
                    scores["bbands"] = score_bollinger(price_val, upper, lower, mid)
    except Exception:
        logger.exception("Bollinger Bands computation failed")

    if not scores:
        return None

    # Compute ADX for regime detection; pass into aggregation
    adx = compute_adx(df)
    if adx is not None:
        logger.debug("ADX=%.1f → %s regime", adx, "trend" if adx > 25 else ("chop" if adx < 20 else "mixed"))
    return aggregate_technical(scores, adx=adx)


def compute_technical_score() -> float | None:
    """Multi-timeframe technical score.

    Timeframe weights: 1H=0.55, 1D=0.35, 1W=0.10.
    Only 1H, 1D, and 1W are computed (no 4H or shorter timeframes from collector).
    Returns a single score in [-100, 100] or None if no data available.
    """
    total_weight = 0.0
    weighted_sum = 0.0

    for timeframe, weight in TIMEFRAME_WEIGHTS.items():
        try:
            df = get_ohlcv_dataframe(timeframe, limit=200)
            if df.empty:
                logger.info("No OHLCV data for timeframe %s", timeframe)
                continue
            score = _compute_indicators_for_df(df)
            if score is not None:
                weighted_sum += score * weight
                total_weight += weight
        except Exception:
            logger.exception("Error computing technical score for %s", timeframe)

    if total_weight == 0:
        return None

    return _clamp(weighted_sum / total_weight)

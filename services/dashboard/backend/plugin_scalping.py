"""Scalping range analyzer — short-timeframe entry levels.

Computes where to buy and where to sell for intraday scalping on
5-minute bars. Pure math, no LLM. Outputs concrete price levels with
stop-loss and take-profit suggestions based on the current range and
volatility (ATR).

Algorithm:
  1. Load last N hours of 5m OHLCV (default 4h = 48 bars)
  2. Compute range_high = 85th percentile of highs
                range_low  = 15th percentile of lows
                range_mid  = (high + low) / 2
  3. Compute 5m ATR over the last 14 bars
  4. Classify volatility regime: tight / normal / wide based on ATR / range
  5. Compute session VWAP for directional bias
  6. Check if current price is upper/mid/lower third of the range
  7. Emit suggested long + short entries:
        LONG:   entry = range_low  + 0.2 * ATR
                SL    = range_low  - 0.8 * ATR
                TP1   = range_mid
                TP2   = range_high - 0.2 * ATR
        SHORT:  entry = range_high - 0.2 * ATR
                SL    = range_high + 0.8 * ATR
                TP1   = range_mid
                TP2   = range_low  + 0.2 * ATR
     R:R computed deterministically for each.
  8. Flag warnings when the setup is risky:
        - current price too close to entry (no room)
        - volatility regime wide (ATR > 1% → scalping risky)
        - funding extreme (crowded positioning)
        - anomaly radar active (sev >= 7)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)


def _percentile(values: list[float], pct: float) -> float:
    """Simple percentile (linear interpolation). pct is in [0, 1]."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = pct * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _atr_from_bars(bars: list[OHLCV], period: int = 14) -> float | None:
    """True-range-based ATR computed over the most recent `period` bars."""
    if len(bars) < period + 1:
        return None
    recent = bars[-(period + 1):]
    trs: list[float] = []
    prev_close = recent[0].close
    for b in recent[1:]:
        tr = max(
            b.high - b.low,
            abs(b.high - prev_close),
            abs(b.low - prev_close),
        )
        trs.append(tr)
        prev_close = b.close
    return sum(trs) / len(trs) if trs else None


def _classify_volatility(atr: float, range_width: float, current_price: float) -> str:
    """tight / normal / wide based on ATR as % of price and range width."""
    if current_price <= 0:
        return "unknown"
    atr_pct = atr / current_price * 100
    if atr_pct >= 1.0:
        return "wide"
    if atr_pct >= 0.4:
        return "normal"
    return "tight"


def _current_zone(price: float, lo: float, mid: float, hi: float) -> str:
    """Which third of the range the price is sitting in."""
    if price >= mid + (hi - mid) * 0.33:
        return "upper"
    if price <= mid - (mid - lo) * 0.33:
        return "lower"
    return "middle"


def compute_scalping_range(
    timeframe: str = "5min",
    lookback_hours: int = 2,
) -> dict:
    """Main entry point — returns a full scalping snapshot.

    The "percentile range" uses a tighter 2-hour window (was 4h) to stop
    old prints from staying stuck in the 85th/15th percentile. On top of
    that, we surface a separate REALTIME 30-min high/low so the UI never
    looks anchored to prints that have already dropped out of the scalp
    relevance window.
    """
    since = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
    symbol = (settings.binance_symbol or "CLUSDT").upper()

    with SessionLocal() as session:
        bars = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "twelve",
                OHLCV.timeframe == timeframe,
                OHLCV.timestamp >= since,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )

    if len(bars) < 20:
        return {
            "error": f"need at least 20 {timeframe} bars, only {len(bars)} available",
        }

    # Basic range — 85th/15th percentile over the full lookback window
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    range_high = round(_percentile(highs, 0.85), 3)
    range_low = round(_percentile(lows, 0.15), 3)
    range_mid = round((range_high + range_low) / 2, 3)
    range_width = range_high - range_low

    # Realtime range — last 30 minutes, plain high/low (no percentile).
    # This is what the UI shows as the "live" range. It updates instantly
    # as prints come in and drop out after 30 minutes — so it can never
    # get stuck on old levels the way the percentile range can.
    rt_cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
    rt_bars = [b for b in bars if b.timestamp >= rt_cutoff]
    if rt_bars:
        realtime_high = round(max(b.high for b in rt_bars), 3)
        realtime_low = round(min(b.low for b in rt_bars), 3)
    else:
        # Fall back to the last ~6 bars (30 min of 5-min data) if the timestamp
        # comparison hits none — e.g. during a collector hiccup
        tail = bars[-6:]
        realtime_high = round(max(b.high for b in tail), 3)
        realtime_low = round(min(b.low for b in tail), 3)
    realtime_mid = round((realtime_high + realtime_low) / 2, 3)

    current_price = bars[-1].close
    atr = _atr_from_bars(bars)
    if atr is None:
        return {"error": "not enough bars to compute ATR"}
    atr_pct = round(atr / current_price * 100, 3)

    # VWAP (volume-weighted) over the window
    total_vol = sum(b.volume or 0.0 for b in bars)
    vwap = None
    if total_vol > 0:
        vwap = sum(((b.high + b.low + b.close) / 3) * (b.volume or 0.0) for b in bars) / total_vol
        vwap = round(vwap, 3)

    vol_regime = _classify_volatility(atr, range_width, current_price)
    zone = _current_zone(current_price, range_low, range_mid, range_high)

    # VWAP bias
    vwap_bias = None
    if vwap is not None:
        dist_pct = (current_price - vwap) / vwap * 100
        if dist_pct >= 0.15:
            vwap_bias = "bullish_above_vwap"
        elif dist_pct <= -0.15:
            vwap_bias = "bearish_below_vwap"
        else:
            vwap_bias = "neutral_at_vwap"

    # Long setup
    long_entry = round(range_low + 0.2 * atr, 3)
    long_sl = round(range_low - 0.8 * atr, 3)
    long_tp1 = range_mid
    long_tp2 = round(range_high - 0.2 * atr, 3)
    long_rr1 = round((long_tp1 - long_entry) / (long_entry - long_sl), 2) if (long_entry > long_sl) else None
    long_rr2 = round((long_tp2 - long_entry) / (long_entry - long_sl), 2) if (long_entry > long_sl) else None

    # Short setup
    short_entry = round(range_high - 0.2 * atr, 3)
    short_sl = round(range_high + 0.8 * atr, 3)
    short_tp1 = range_mid
    short_tp2 = round(range_low + 0.2 * atr, 3)
    short_rr1 = round((short_entry - short_tp1) / (short_sl - short_entry), 2) if (short_entry < short_sl) else None
    short_rr2 = round((short_entry - short_tp2) / (short_sl - short_entry), 2) if (short_entry < short_sl) else None

    # Distances to entries from current price
    dist_to_long_pct = round((long_entry - current_price) / current_price * 100, 3)
    dist_to_short_pct = round((short_entry - current_price) / current_price * 100, 3)

    # Warnings
    warnings: list[str] = []
    if vol_regime == "wide":
        warnings.append("WIDE volatility regime — scalping R:R degrades; consider sitting out")
    if abs(dist_to_long_pct) < atr_pct * 0.3:
        warnings.append("Price very close to LONG entry — already touching support; little room")
    if abs(dist_to_short_pct) < atr_pct * 0.3:
        warnings.append("Price very close to SHORT entry — already touching resistance; little room")
    if range_width < atr * 1.5:
        warnings.append("Range compressed (< 1.5 ATR) — breakout risk, scalping may fail")

    # Funding / anomaly context (best-effort)
    funding_pct = None
    active_anomalies = 0
    try:
        from shared.models.binance_metrics import BinanceFundingRate
        with SessionLocal() as session:
            fr = (
                session.query(BinanceFundingRate)
                .order_by(desc(BinanceFundingRate.funding_time))
                .first()
            )
            if fr:
                funding_pct = round(fr.funding_rate * 100, 4)
                if abs(funding_pct) >= 0.03:
                    warnings.append(
                        f"Funding extreme ({funding_pct:+.4f}%) — "
                        f"{'shorts' if funding_pct < 0 else 'longs'} crowded, squeeze risk"
                    )
    except Exception:
        pass

    try:
        from plugin_anomalies import detect_anomalies
        anomalies = detect_anomalies()
        active_anomalies = len(anomalies)
        if any(a.get("severity", 0) >= 7 for a in anomalies):
            warnings.append("High-severity anomaly active — verify before scalping")
    except Exception:
        pass

    # Preferred side based on context
    prefer_side = None
    prefer_reason = ""
    if zone == "lower" and vwap_bias in ("bullish_above_vwap", "neutral_at_vwap"):
        prefer_side = "LONG"
        prefer_reason = "price in lower third of range + VWAP support"
    elif zone == "upper" and vwap_bias in ("bearish_below_vwap", "neutral_at_vwap"):
        prefer_side = "SHORT"
        prefer_reason = "price in upper third of range + VWAP resistance"
    elif zone == "middle":
        prefer_side = "WAIT"
        prefer_reason = "price in middle of range — wait for extreme"
    else:
        prefer_side = "CAUTION"
        prefer_reason = f"price in {zone} but VWAP contradicts"

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "lookback_hours": lookback_hours,
        "bar_count": len(bars),
        "current_price": round(current_price, 3),
        "range": {
            "low": range_low,
            "mid": range_mid,
            "high": range_high,
            "width": round(range_width, 3),
            "width_pct": round(range_width / current_price * 100, 3),
        },
        # Realtime 30-min high/low — cannot get stuck on old prints
        "realtime_range": {
            "low": realtime_low,
            "mid": realtime_mid,
            "high": realtime_high,
            "width": round(realtime_high - realtime_low, 3),
            "window_minutes": 30,
        },
        "atr_5m": round(atr, 3),
        "atr_pct": atr_pct,
        "volatility_regime": vol_regime,
        "vwap": vwap,
        "vwap_bias": vwap_bias,
        "zone": zone,
        "prefer_side": prefer_side,
        "prefer_reason": prefer_reason,
        "long_setup": {
            "entry": long_entry,
            "stop_loss": long_sl,
            "take_profit_1": long_tp1,
            "take_profit_2": long_tp2,
            "rr_tp1": long_rr1,
            "rr_tp2": long_rr2,
            "distance_from_current_pct": dist_to_long_pct,
        },
        "short_setup": {
            "entry": short_entry,
            "stop_loss": short_sl,
            "take_profit_1": short_tp1,
            "take_profit_2": short_tp2,
            "rr_tp1": short_rr1,
            "rr_tp2": short_rr2,
            "distance_from_current_pct": dist_to_short_pct,
        },
        "funding_rate_pct": funding_pct,
        "active_anomalies": active_anomalies,
        "warnings": warnings,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }

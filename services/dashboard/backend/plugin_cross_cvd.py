"""Cross-asset snapshot + Cumulative Volume Delta (CVD) plugin.

cross_asset_snapshot()
  Reads the latest bars of DXY / SPX / Gold / BTC / VIX from the shared
  OHLCV table (where the data-collector cross_assets collector writes
  them under source='cross_asset') and computes:
    - current value
    - 1h / 24h change %
    - rolling correlation with CLUSDT over the last N hours

cvd_series()
  Computes Cumulative Volume Delta for CLUSDT over the last N minutes by
  pulling Binance aggTrades and summing buy_volume - sell_volume in
  1-minute buckets, then cumulatively adding. Divergence between CVD and
  price is a strong reversal signal.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import desc

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)

_SYMBOL = "CLUSDT"
_CROSS_ASSETS = [
    "DXY",       # real computed from forex basket (not ETF proxy)
    "SPX",       # via SPY ETF
    "GOLD",      # XAU/USD spot
    "BTC",       # BTC/USD
    "ETH",       # ETH/USD (risk-on barometer)
    "SOL",       # SOL/USD (high-beta risk-on)
    "VIX",       # via VIXY ETF
    "EURUSD",    # major forex
    "USDJPY",    # safe-haven JPY
    "GBPUSD",    # UK exposure
]


# ---------------------------------------------------------------------------
# Cross-asset snapshot
# ---------------------------------------------------------------------------

def _load_series(label: str, hours: int) -> list[tuple[datetime, float]]:
    """Load (timestamp, close) tuples for a cross-asset ticker over the window."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    tf = f"{label}:1h"
    with SessionLocal() as session:
        rows = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "cross_asset",
                OHLCV.timeframe == tf,
                OHLCV.timestamp >= since,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )
    return [(r.timestamp, float(r.close)) for r in rows if r.close is not None]


def _load_clusdt_series(hours: int) -> list[tuple[datetime, float]]:
    """Load the canonical WTI price series for cross-asset correlation.

    Historically this read Binance CLUSDT klines; now reads Twelve Data
    WTI/USD (source='twelve') since that's the single canonical feed.
    Kept the legacy name so existing callers don't break.
    """
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        rows = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "twelve",
                OHLCV.timeframe == "1H",
                OHLCV.timestamp >= since,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )
    return [(r.timestamp, float(r.close)) for r in rows]


def _pct_change_over(series: list[tuple[datetime, float]], hours: int) -> float | None:
    """Return the % change between the oldest bar in `hours` back and the latest."""
    if not series:
        return None
    latest_ts, latest_val = series[-1]
    cutoff = latest_ts - timedelta(hours=hours)
    old_val = None
    for ts, v in series:
        if ts >= cutoff:
            old_val = v
            break
    if old_val is None or old_val == 0:
        return None
    return round((latest_val - old_val) / old_val * 100, 3)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Simple Pearson correlation, no numpy."""
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    xs = xs[-n:]
    ys = ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 3)


def cross_asset_snapshot(hours: int = 24) -> dict:
    """Return current values, changes, and correlation for each cross asset."""
    clusdt = _load_clusdt_series(hours=hours)
    clusdt_closes = [c for _, c in clusdt]

    snapshots: dict[str, dict] = {}
    for label in _CROSS_ASSETS:
        series = _load_series(label, hours=hours)
        if not series:
            snapshots[label] = {"error": "no data"}
            continue

        latest_ts, latest = series[-1]
        closes = [c for _, c in series]

        # Align series by trimming to the same length as clusdt
        n = min(len(closes), len(clusdt_closes))
        corr = _pearson(closes[-n:], clusdt_closes[-n:]) if n >= 5 else None

        snapshots[label] = {
            "latest": round(latest, 3),
            "latest_time": latest_ts.isoformat(),
            "change_1h_pct": _pct_change_over(series, 1),
            "change_24h_pct": _pct_change_over(series, 24),
            "correlation_clusdt_24h": corr,
            "bar_count": len(series),
        }

    return {
        "window_hours": hours,
        "symbols": snapshots,
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# CVD (Cumulative Volume Delta) from Binance aggTrades
# ---------------------------------------------------------------------------

def _symbol() -> str:
    return (settings.binance_symbol or "CLUSDT").upper()


def cvd_series(minutes: int = 60) -> dict:
    """Compute CVD over the last ~N minutes using Binance aggTrades.

    The aggTrades endpoint returns up to 1000 aggregated trades. On CLUSDT
    this covers roughly 1-5 minutes of activity depending on flow. For
    longer windows we'd need multiple pages, but for a "last 60m" widget
    1000 trades gives us a recent slice.

    We fall back to computing from 1-min klines (taker buy volume is
    available in kline data via takerBuyBaseAssetVolume). That gives us
    accurate CVD over arbitrary windows without paging aggTrades.
    """
    # Pull up to 500 most recent 1-min klines and compute CVD from them.
    limit = max(5, min(500, minutes))
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": _symbol(), "interval": "1m", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as exc:
        return {"error": f"klines fetch failed: {exc}"}

    # Kline row:
    # [0] openTime, [1] open, [2] high, [3] low, [4] close, [5] volume,
    # [6] closeTime, [7] quoteVolume, [8] trades, [9] takerBuyBase,
    # [10] takerBuyQuote
    cumulative = 0.0
    points: list[dict] = []
    for row in raw:
        try:
            t_ms = int(row[0])
            volume = float(row[5])
            close = float(row[4])
            taker_buy = float(row[9])
            taker_sell = volume - taker_buy
            delta = taker_buy - taker_sell
            cumulative += delta
        except (ValueError, IndexError, TypeError):
            continue
        points.append({
            "time": t_ms // 1000,
            "close": close,
            "volume": round(volume, 2),
            "taker_buy": round(taker_buy, 2),
            "taker_sell": round(taker_sell, 2),
            "delta": round(delta, 2),
            "cvd": round(cumulative, 2),
        })

    if not points:
        return {"error": "no klines"}

    # Detect price/CVD divergence over last N bars
    tail = points[-min(20, len(points)):]
    divergence = None
    if len(tail) >= 5:
        first = tail[0]
        last = tail[-1]
        price_change = last["close"] - first["close"]
        cvd_change = last["cvd"] - first["cvd"]
        if price_change > 0 and cvd_change < 0:
            divergence = {
                "type": "BEARISH_DIVERGENCE",
                "message": "Price rising but CVD falling — hidden selling pressure, reversal risk",
                "price_change": round(price_change, 3),
                "cvd_change": round(cvd_change, 2),
            }
        elif price_change < 0 and cvd_change > 0:
            divergence = {
                "type": "BULLISH_DIVERGENCE",
                "message": "Price falling but CVD rising — hidden buying pressure, reversal setup",
                "price_change": round(price_change, 3),
                "cvd_change": round(cvd_change, 2),
            }

    return {
        "symbol": _symbol(),
        "window_minutes": len(points),
        "current_cvd": points[-1]["cvd"],
        "current_price": points[-1]["close"],
        "divergence": divergence,
        "series": points,
    }

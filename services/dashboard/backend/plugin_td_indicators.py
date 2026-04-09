"""Twelve Data technical indicators — external sanity check on our analyzer.

We compute our own RSI/MACD/ATR/ADX/BBANDS in services/analyzer using
pandas-ta. This plugin fetches the same indicators from Twelve Data's
pre-computed endpoints and exposes them side-by-side so we can:
  1. Verify our analyzer isn't drifting (divergence alert)
  2. Use cross-asset indicators as macro stress signals (SPY RSI,
     BTC ADX, DXY MACD) without adding more pandas-ta computation
  3. Give the LLM agents ready-to-cite indicator values in one place

Credit cost: 1 credit per indicator call. For the default fetch this
is 5 WTI indicators on 1h + 3 cross-asset indicators = 8 credits per
refresh. With 5-minute cache that's ~100 credits/hour ≈ 1.6/min, well
below the 55/min Grow-plan limit.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from shared.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.twelvedata.com"
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 300  # 5 minutes


def _cache_get(key: str) -> dict | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if time.time() - entry["ts"] > _CACHE_TTL_SECONDS:
            return None
        return entry["data"]


def _cache_set(key: str, data: dict) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data}


def _fetch_indicator(
    endpoint: str,
    symbol: str,
    interval: str,
    extra: dict | None = None,
) -> dict | None:
    """Fetch one indicator series. Returns the latest value + metadata."""
    if not settings.twelve_api_key:
        return None
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 5,
        "apikey": settings.twelve_api_key,
        "format": "JSON",
    }
    if extra:
        params.update(extra)
    try:
        r = requests.get(f"{_BASE}{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        d = r.json()
    except Exception as exc:
        logger.warning("TD %s %s %s failed: %s", endpoint, symbol, interval, exc)
        return None
    if isinstance(d, dict) and d.get("status") == "error":
        logger.warning("TD %s %s: %s", endpoint, symbol, d.get("message", ""))
        return None
    values = d.get("values") or []
    if not values:
        return None
    latest = values[0]  # newest first
    return {
        "latest": latest,
        "previous": values[1] if len(values) > 1 else None,
        "values": values[:5],
    }


# ---------------------------------------------------------------------------
# Composite fetch — all indicators for one symbol
# ---------------------------------------------------------------------------

_WTI_INDICATORS = [
    ("/rsi",    {"time_period": 14}),
    ("/macd",   {"fast_period": 12, "slow_period": 26, "signal_period": 9}),
    ("/atr",    {"time_period": 14}),
    ("/adx",    {"time_period": 14}),
    ("/bbands", {"time_period": 20, "sd": 2}),
]


def fetch_wti_indicators(interval: str = "1h") -> dict:
    """All 5 WTI indicators on a single timeframe.

    Returns structured dict with rsi, macd, atr, adx, bbands sub-keys,
    each containing latest value and a simple interpretation label.
    """
    cache_key = f"wti_{interval}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result: dict = {"symbol": "WTI/USD", "interval": interval}

    # Fetch in parallel
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_fetch_indicator, endpoint, "WTI/USD", interval, extra): endpoint
            for endpoint, extra in _WTI_INDICATORS
        }
        for future in as_completed(futures):
            endpoint = futures[future]
            try:
                data = future.result(timeout=15)
            except Exception:
                logger.exception("indicator fetch crashed for %s", endpoint)
                data = None
            key = endpoint.strip("/")
            result[key] = data

    # Interpret each indicator
    result["interpretation"] = _interpret_wti(result)
    _cache_set(cache_key, result)
    return result


def _interpret_wti(r: dict) -> dict:
    """Turn raw indicator values into human-readable labels + bull/bear flags."""
    out: dict = {}

    # RSI
    rsi = r.get("rsi", {})
    if rsi and rsi.get("latest"):
        v = float(rsi["latest"]["rsi"])
        if v >= 70:
            label, bias = f"Overbought ({v:.1f})", "bearish"
        elif v >= 60:
            label, bias = f"Bullish zone ({v:.1f})", "bullish"
        elif v > 40:
            label, bias = f"Neutral ({v:.1f})", "neutral"
        elif v > 30:
            label, bias = f"Bearish zone ({v:.1f})", "bearish"
        else:
            label, bias = f"Oversold ({v:.1f})", "bullish"
        out["rsi"] = {"value": v, "label": label, "bias": bias}

    # MACD
    macd = r.get("macd", {})
    if macd and macd.get("latest"):
        line = float(macd["latest"]["macd"])
        signal = float(macd["latest"]["macd_signal"])
        hist = float(macd["latest"]["macd_hist"])
        cross = "above" if line > signal else "below"
        bias = "bullish" if line > signal else "bearish"
        out["macd"] = {
            "line": line, "signal": signal, "histogram": hist,
            "label": f"MACD {cross} signal (hist {hist:+.3f})",
            "bias": bias,
        }

    # ATR
    atr = r.get("atr", {})
    if atr and atr.get("latest"):
        v = float(atr["latest"]["atr"])
        out["atr"] = {"value": round(v, 4), "label": f"ATR {v:.3f}"}

    # ADX
    adx = r.get("adx", {})
    if adx and adx.get("latest"):
        v = float(adx["latest"]["adx"])
        if v >= 40:
            regime = "strong trend"
        elif v >= 25:
            regime = "trending"
        else:
            regime = "range / no trend"
        out["adx"] = {"value": round(v, 1), "label": f"ADX {v:.1f} ({regime})"}

    # BBANDS
    bb = r.get("bbands", {})
    if bb and bb.get("latest"):
        u = float(bb["latest"]["upper_band"])
        m = float(bb["latest"]["middle_band"])
        l = float(bb["latest"]["lower_band"])
        out["bbands"] = {
            "upper": round(u, 3), "middle": round(m, 3), "lower": round(l, 3),
            "width_pct": round((u - l) / m * 100, 3) if m else None,
            "label": f"BB [{l:.2f}, {u:.2f}] width {((u-l)/m*100) if m else 0:.2f}%",
        }

    # Overall bias
    bulls = sum(1 for k in ("rsi", "macd") if out.get(k, {}).get("bias") == "bullish")
    bears = sum(1 for k in ("rsi", "macd") if out.get(k, {}).get("bias") == "bearish")
    if bulls > bears:
        out["overall_bias"] = "bullish"
    elif bears > bulls:
        out["overall_bias"] = "bearish"
    else:
        out["overall_bias"] = "neutral"
    return out


# ---------------------------------------------------------------------------
# Multi-timeframe RSI — for scalp brain alignment check
# ---------------------------------------------------------------------------

def fetch_multi_tf_rsi(intervals: tuple[str, ...] = ("1min", "5min", "15min")) -> dict:
    """Fetch RSI for WTI on multiple intraday timeframes in parallel.

    Used by the scalp brain to check multi-TF alignment — when all three
    timeframes are oversold and turning up (or overbought and turning
    down), it's a high-conviction scalp setup.

    Credit cost: 1 credit per interval. Cached for 60 seconds (we want
    faster freshness than the 5-min cache used by the main indicator
    fetcher — scalping needs live data). 3 intervals × 60 calls/hr =
    180 credits/hr ≈ 3 credits/min.
    """
    cache_key = f"multi_tf_rsi_{'_'.join(intervals)}"
    cached = _cache_get_with_ttl(cache_key, ttl_seconds=60)
    if cached is not None:
        return cached

    result: dict = {}
    with ThreadPoolExecutor(max_workers=len(intervals)) as executor:
        futures = {
            executor.submit(
                _fetch_indicator, "/rsi", "WTI/USD", interval, {"time_period": 14}
            ): interval
            for interval in intervals
        }
        for future in as_completed(futures):
            interval = futures[future]
            try:
                data = future.result(timeout=10)
            except Exception:
                data = None
            if data and data.get("latest") and data["latest"].get("rsi") is not None:
                latest_val = float(data["latest"]["rsi"])
                prev_val = None
                if data.get("previous") and data["previous"].get("rsi") is not None:
                    try:
                        prev_val = float(data["previous"]["rsi"])
                    except (TypeError, ValueError):
                        prev_val = None
                # Direction: turning_up if latest > previous, turning_down otherwise
                direction = None
                if prev_val is not None:
                    direction = "turning_up" if latest_val > prev_val else "turning_down"
                # Zone
                if latest_val >= 70:
                    zone = "overbought"
                elif latest_val >= 55:
                    zone = "bullish"
                elif latest_val >= 45:
                    zone = "neutral"
                elif latest_val >= 30:
                    zone = "bearish"
                else:
                    zone = "oversold"
                result[interval] = {
                    "rsi": round(latest_val, 2),
                    "prev_rsi": round(prev_val, 2) if prev_val is not None else None,
                    "zone": zone,
                    "direction": direction,
                }

    out = {"intervals": result}
    _cache_set(cache_key, out)
    return out


def _cache_get_with_ttl(key: str, ttl_seconds: int) -> dict | None:
    """Variant of _cache_get that lets callers override the TTL."""
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if time.time() - entry["ts"] > ttl_seconds:
            return None
        return entry["data"]


# ---------------------------------------------------------------------------
# Cross-asset stress meter — RSI on SPY, BTC, UUP
# ---------------------------------------------------------------------------

def fetch_cross_asset_stress() -> dict:
    """Fetch 1h RSI for three stress barometers: SPY (risk), BTC (crypto
    risk-on), UUP (DXY proxy). Used to label macro regime at a glance."""
    cache_key = "cross_stress"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    symbols = {
        "SPY":  ("risk-on US equities", "bullish_if_overbought"),
        "BTC/USD": ("crypto risk-on", "bullish_if_overbought"),
        "UUP":  ("DXY proxy (USD)", "bearish_for_oil_if_overbought"),
    }

    result: dict = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_fetch_indicator, "/rsi", sym, "1h", {"time_period": 14}): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                data = future.result(timeout=15)
            except Exception:
                data = None
            if data and data.get("latest"):
                v = float(data["latest"]["rsi"])
                description, bias_rule = symbols[sym]
                if v >= 70:
                    state = "overbought"
                elif v >= 55:
                    state = "mild bull"
                elif v >= 45:
                    state = "neutral"
                elif v >= 30:
                    state = "mild bear"
                else:
                    state = "oversold"
                result[sym] = {
                    "rsi": round(v, 1),
                    "state": state,
                    "description": description,
                }

    _cache_set(cache_key, {"symbols": result})
    return {"symbols": result}

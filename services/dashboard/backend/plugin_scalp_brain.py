"""Scalp Brain — one-panel scalping verdict.

Takes every signal we already compute (multi-TF RSI, VWAP bands, session
range, ORB, CVD, orderbook imbalance, whales, conviction, cross-asset,
session regime, BBANDS squeeze) and stitches them into ONE verdict:

    LONG NOW  |  SHORT NOW  |  LEAN LONG  |  LEAN SHORT  |  WAIT

…plus deterministic entry / SL / TP1 / TP2 / R:R based on ATR and
structural levels. No LLM — pure scoring math so the same inputs always
produce the same output.

Weights (deliberately conservative — tune from real data later):

    multi_tf_rsi          15
    vwap_bands            15
    session_range_pos     10
    opening_range_bo      10
    cvd                   10
    orderbook_imbalance   10
    whale_bias             8
    conviction_trend       8
    cross_asset_stress     5
    session_regime         5
    bbands_squeeze         4
                         ----
    total weight         100

Each signal votes bullish / bearish / neutral with a weight contribution.
Verdict:
    long_pct  = bullish_weight / 100
    short_pct = bearish_weight / 100
    bias_pct  = long_pct - short_pct

    bias_pct >=  0.30  +  ≥3/4 gatekeepers pass  →  LONG NOW
    bias_pct <= -0.30  +  ≥3/4 gatekeepers pass  →  SHORT NOW
    bias_pct >=  0.15                             →  LEAN LONG
    bias_pct <= -0.15                             →  LEAN SHORT
    otherwise                                     →  WAIT

Four gatekeepers — all must pass for a NOW verdict, one miss downgrades
to LEAN, two misses force WAIT:

    1. ATR wide enough for R:R ≥ 1.5 on a scalp (configurable floor)
    2. ADX ≥ 18 — not random chop
    3. No higher-timeframe wall against the trade
    4. Not contradicted by open heartbeat-managed campaign
       (warning rather than block — we still show the verdict)
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from shared.models.base import SessionLocal
from shared.models.campaigns import Campaign
from shared.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache — scalp brain is called often, and a 10-second cache
# is fast enough for the UI while collapsing expensive downstream calls
# (TD indicators, Binance orderbook, CVD) into one pass.
# ---------------------------------------------------------------------------

_CACHE: dict | None = None
_CACHE_TS: float = 0.0
_CACHE_TTL_SECONDS = 10
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Scoring weights (sum to 100)
# ---------------------------------------------------------------------------

WEIGHTS = {
    "multi_tf_rsi": 15,
    "vwap_bands": 15,
    "session_range_pos": 10,
    "opening_range_bo": 10,
    "cvd": 10,
    "orderbook_imbalance": 10,
    "whale_bias": 8,
    "conviction_trend": 8,
    "cross_asset_stress": 5,
    "session_regime": 5,
    "bbands_squeeze": 4,
}
assert sum(WEIGHTS.values()) == 100, "weights must sum to 100"

# Minimum ATR in dollars for a scalp to have room for R:R ≥ 1.5
ATR_FLOOR_USD = 0.20
ADX_FLOOR = 18.0


# ---------------------------------------------------------------------------
# Signal helpers — each returns (bias, detail_dict)
# bias is "bullish" | "bearish" | "neutral"
# ---------------------------------------------------------------------------


def _sig_multi_tf_rsi() -> tuple[str, dict]:
    """All three sub-hour RSIs aligned same direction = strong vote."""
    try:
        from plugin_td_indicators import fetch_multi_tf_rsi
        data = fetch_multi_tf_rsi()
    except Exception:
        logger.exception("multi_tf_rsi fetch failed")
        return "neutral", {"error": "fetch_failed"}

    intervals = data.get("intervals") or {}
    if len(intervals) < 2:
        return "neutral", {"intervals": intervals, "note": "insufficient data"}

    # Count zones
    oversold_up = sum(
        1 for v in intervals.values()
        if v.get("zone") == "oversold" and v.get("direction") == "turning_up"
    )
    overbought_down = sum(
        1 for v in intervals.values()
        if v.get("zone") == "overbought" and v.get("direction") == "turning_down"
    )
    bullish_zones = sum(1 for v in intervals.values() if v.get("zone") in ("oversold", "bearish"))
    bearish_zones = sum(1 for v in intervals.values() if v.get("zone") in ("overbought", "bullish"))

    # Strongest signal: all intervals oversold AND turning up (or symmetric)
    if oversold_up >= 2:
        return "bullish", {"intervals": intervals, "reason": f"{oversold_up}/3 oversold+turning_up"}
    if overbought_down >= 2:
        return "bearish", {"intervals": intervals, "reason": f"{overbought_down}/3 overbought+turning_down"}
    # Weak alignment
    if bullish_zones >= 2 and bearish_zones == 0:
        return "bullish", {"intervals": intervals, "reason": "majority oversold/bearish zone"}
    if bearish_zones >= 2 and bullish_zones == 0:
        return "bearish", {"intervals": intervals, "reason": "majority overbought/bullish zone"}
    return "neutral", {"intervals": intervals, "reason": "mixed"}


def _sig_vwap_bands(current_price: float) -> tuple[str, dict]:
    """Compute VWAP ± 2σ over last 4h of 5-min bars and check where price sits.

    Scalper read:
      - price tagged lower band and is bouncing → bullish
      - price tagged upper band and is fading → bearish
      - price within 1σ of VWAP → neutral
    """
    since = datetime.now(tz=timezone.utc) - timedelta(hours=4)
    with SessionLocal() as session:
        bars = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "twelve",
                OHLCV.timeframe == "5min",
                OHLCV.timestamp >= since,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )
    if len(bars) < 10:
        return "neutral", {"error": "insufficient bars"}

    # Volume-weighted typical price
    typical = [(b.high + b.low + b.close) / 3 for b in bars]
    volumes = [(b.volume or 0.0) for b in bars]
    total_vol = sum(volumes)
    if total_vol <= 0:
        # Fall back to unweighted mean
        vwap = sum(typical) / len(typical)
    else:
        vwap = sum(t * v for t, v in zip(typical, volumes)) / total_vol

    # Volatility-weighted std dev of typical prices about VWAP
    variance = sum((t - vwap) ** 2 for t in typical) / len(typical)
    sigma = variance ** 0.5
    upper = vwap + 2 * sigma
    lower = vwap - 2 * sigma

    distance_to_vwap = current_price - vwap

    if current_price <= lower + (sigma * 0.3):  # touching / below lower band
        bias = "bullish"
        reason = f"at/below VWAP-2σ (${lower:.3f}), scalp long opportunity"
    elif current_price >= upper - (sigma * 0.3):  # touching / above upper band
        bias = "bearish"
        reason = f"at/above VWAP+2σ (${upper:.3f}), scalp short opportunity"
    elif distance_to_vwap > sigma:  # bullish context, waiting for pullback
        bias = "neutral"
        reason = "above VWAP but not at upper band"
    elif distance_to_vwap < -sigma:  # bearish context
        bias = "neutral"
        reason = "below VWAP but not at lower band"
    else:
        bias = "neutral"
        reason = f"within 1σ of VWAP (${vwap:.3f})"

    return bias, {
        "vwap": round(vwap, 3),
        "sigma": round(sigma, 3),
        "upper_band": round(upper, 3),
        "lower_band": round(lower, 3),
        "distance_to_vwap": round(distance_to_vwap, 3),
        "reason": reason,
    }


def _sig_session_range_pos(current_price: float) -> tuple[str, dict]:
    """Where is price within the last 30-min high/low, and what's the sequence?

    Bullish = near session low with higher-low sequence
    Bearish = near session high with lower-high sequence
    """
    rt_cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
    with SessionLocal() as session:
        bars = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "twelve",
                OHLCV.timeframe == "1min",
                OHLCV.timestamp >= rt_cutoff,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )
    if len(bars) < 10:
        return "neutral", {"error": "insufficient bars"}

    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    session_high = max(highs)
    session_low = min(lows)
    range_width = session_high - session_low
    if range_width <= 0:
        return "neutral", {"error": "zero range"}

    pos_pct = (current_price - session_low) / range_width  # 0 = at low, 1 = at high

    # Simple sequence check — compare first half vs second half of the window
    mid = len(bars) // 2
    first_low = min(b.low for b in bars[:mid])
    second_low = min(b.low for b in bars[mid:])
    first_high = max(b.high for b in bars[:mid])
    second_high = max(b.high for b in bars[mid:])
    higher_lows = second_low > first_low
    lower_highs = second_high < first_high

    if pos_pct <= 0.25 and higher_lows:
        return "bullish", {
            "pos_pct": round(pos_pct, 3),
            "session_high": round(session_high, 3),
            "session_low": round(session_low, 3),
            "reason": "near session low with higher-low sequence",
        }
    if pos_pct >= 0.75 and lower_highs:
        return "bearish", {
            "pos_pct": round(pos_pct, 3),
            "session_high": round(session_high, 3),
            "session_low": round(session_low, 3),
            "reason": "near session high with lower-high sequence",
        }
    return "neutral", {
        "pos_pct": round(pos_pct, 3),
        "session_high": round(session_high, 3),
        "session_low": round(session_low, 3),
        "reason": f"{int(pos_pct*100)}% of range",
    }


def _sig_opening_range_breakout(current_price: float) -> tuple[str, dict]:
    """First 15 minutes of NY session = opening range.

    Bullish = price trading > ORB high after the 15-min mark
    Bearish = price trading < ORB low after the 15-min mark
    Neutral = still inside the ORB, or NY session not yet open
    """
    now = datetime.now(tz=timezone.utc)
    # NY equity open ≈ 13:30 UTC (standard time) or 14:30 UTC (DST).
    # Use 13:30 as the conservative default — energy futures have their own
    # sessions but NY equity open is when the biggest WTI flow shows up.
    session_open = now.replace(hour=13, minute=30, second=0, microsecond=0)
    orb_end = session_open + timedelta(minutes=15)
    if now < orb_end:
        return "neutral", {"reason": "NY session not open / inside ORB window"}

    with SessionLocal() as session:
        bars = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "twelve",
                OHLCV.timeframe == "1min",
                OHLCV.timestamp >= session_open,
                OHLCV.timestamp < orb_end,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )
    if len(bars) < 5:
        return "neutral", {"reason": "insufficient ORB bars"}

    orb_high = max(b.high for b in bars)
    orb_low = min(b.low for b in bars)
    if current_price > orb_high:
        return "bullish", {
            "orb_high": round(orb_high, 3),
            "orb_low": round(orb_low, 3),
            "reason": f"price > ORB high ${orb_high:.3f}",
        }
    if current_price < orb_low:
        return "bearish", {
            "orb_high": round(orb_high, 3),
            "orb_low": round(orb_low, 3),
            "reason": f"price < ORB low ${orb_low:.3f}",
        }
    return "neutral", {
        "orb_high": round(orb_high, 3),
        "orb_low": round(orb_low, 3),
        "reason": "inside ORB, waiting for breakout",
    }


def _sig_cvd() -> tuple[str, dict]:
    """CVD divergence + recent delta direction."""
    try:
        from plugin_cross_cvd import cvd_series
        data = cvd_series(minutes=60)
    except Exception:
        logger.exception("cvd_series failed")
        return "neutral", {"error": "fetch_failed"}

    if data.get("error"):
        return "neutral", {"error": data["error"]}

    div = data.get("divergence") or {}
    series = data.get("series") or []
    if len(series) < 5:
        return "neutral", {"reason": "insufficient data"}

    # Last 5 bars of delta
    recent_deltas = [p.get("delta", 0) for p in series[-5:]]
    recent_sum = sum(recent_deltas)

    # Divergence trumps raw delta direction
    if div.get("type") == "BULLISH_DIVERGENCE":
        return "bullish", {"reason": "CVD bullish divergence", "recent_delta_sum": round(recent_sum, 2)}
    if div.get("type") == "BEARISH_DIVERGENCE":
        return "bearish", {"reason": "CVD bearish divergence", "recent_delta_sum": round(recent_sum, 2)}

    if recent_sum > 0 and all(d >= 0 for d in recent_deltas[-3:]):
        return "bullish", {"reason": "CVD thrust up (3 consecutive positive deltas)", "recent_delta_sum": round(recent_sum, 2)}
    if recent_sum < 0 and all(d <= 0 for d in recent_deltas[-3:]):
        return "bearish", {"reason": "CVD thrust down (3 consecutive negative deltas)", "recent_delta_sum": round(recent_sum, 2)}

    return "neutral", {"reason": "mixed CVD", "recent_delta_sum": round(recent_sum, 2)}


def _sig_orderbook_imbalance() -> tuple[str, dict]:
    """Top-of-book bid/ask volume imbalance as a scalp-time flow read."""
    import requests
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/depth",
            params={"symbol": "CLUSDT", "limit": 20},
            timeout=5,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception:
        return "neutral", {"error": "depth fetch failed"}

    bids = [(float(p), float(q)) for p, q in raw.get("bids", [])[:10]]
    asks = [(float(p), float(q)) for p, q in raw.get("asks", [])[:10]]
    bid_vol = sum(q for _, q in bids)
    ask_vol = sum(q for _, q in asks)
    total = bid_vol + ask_vol
    if total <= 0:
        return "neutral", {"error": "empty book"}

    imbalance = (bid_vol - ask_vol) / total  # -1 (all ask) to +1 (all bid)
    ratio = bid_vol / ask_vol if ask_vol > 0 else 99.0

    if imbalance >= 0.2 and ratio >= 1.5:
        return "bullish", {"imbalance": round(imbalance, 3), "ratio": round(ratio, 2), "reason": f"top-10 bid stack {ratio:.1f}x ask"}
    if imbalance <= -0.2 and ratio <= (1 / 1.5):
        return "bearish", {"imbalance": round(imbalance, 3), "ratio": round(ratio, 2), "reason": f"top-10 ask stack {(1/ratio):.1f}x bid"}
    return "neutral", {"imbalance": round(imbalance, 3), "ratio": round(ratio, 2), "reason": "balanced book"}


def _sig_whale_bias() -> tuple[str, dict]:
    """Last 10-min whale trade delta from the /api/whale-trades data path."""
    import requests
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/aggTrades",
            params={"symbol": "CLUSDT", "limit": 1000},
            timeout=5,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception:
        return "neutral", {"error": "aggTrades fetch failed"}

    cutoff_ms = int((datetime.now(tz=timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    buy_usd = 0.0
    sell_usd = 0.0
    min_whale_usd = 50_000
    for row in raw:
        try:
            ts = int(row["T"])
            if ts < cutoff_ms:
                continue
            price = float(row["p"])
            qty = float(row["q"])
            quote = price * qty
            if quote < min_whale_usd:
                continue
            is_seller = bool(row.get("m", False))  # m=true means buyer is maker → taker sold
            if is_seller:
                sell_usd += quote
            else:
                buy_usd += quote
        except (KeyError, ValueError, TypeError):
            continue

    total = buy_usd + sell_usd
    if total < 100_000:  # not enough whale activity
        return "neutral", {
            "buy_usd": round(buy_usd, 0),
            "sell_usd": round(sell_usd, 0),
            "reason": "low whale activity",
        }

    bias_pct = (buy_usd - sell_usd) / total
    if bias_pct >= 0.2:
        return "bullish", {
            "buy_usd": round(buy_usd, 0),
            "sell_usd": round(sell_usd, 0),
            "reason": f"whales {int(bias_pct*100)}% net buying",
        }
    if bias_pct <= -0.2:
        return "bearish", {
            "buy_usd": round(buy_usd, 0),
            "sell_usd": round(sell_usd, 0),
            "reason": f"whales {int(-bias_pct*100)}% net selling",
        }
    return "neutral", {
        "buy_usd": round(buy_usd, 0),
        "sell_usd": round(sell_usd, 0),
        "reason": "whales balanced",
    }


def _sig_conviction_trend() -> tuple[str, dict]:
    """Unified score direction + level."""
    from shared.models.signals import AnalysisScore
    with SessionLocal() as session:
        rows = (
            session.query(AnalysisScore)
            .order_by(AnalysisScore.timestamp.desc())
            .limit(3)
            .all()
        )
    if len(rows) < 2:
        return "neutral", {"reason": "insufficient score history"}

    latest = rows[0].unified_score
    previous = rows[1].unified_score
    if latest is None or previous is None:
        return "neutral", {"reason": "null score"}

    delta = latest - previous
    if latest >= 15 and delta >= 0:
        return "bullish", {"unified": round(latest, 1), "delta": round(delta, 1), "reason": f"unified {latest:.0f} rising"}
    if latest <= -15 and delta <= 0:
        return "bearish", {"unified": round(latest, 1), "delta": round(delta, 1), "reason": f"unified {latest:.0f} falling"}
    if latest > 5 and delta > 3:
        return "bullish", {"unified": round(latest, 1), "delta": round(delta, 1), "reason": "unified rising"}
    if latest < -5 and delta < -3:
        return "bearish", {"unified": round(latest, 1), "delta": round(delta, 1), "reason": "unified falling"}
    return "neutral", {"unified": round(latest, 1), "delta": round(delta, 1), "reason": "flat score"}


def _sig_cross_asset_stress() -> tuple[str, dict]:
    """DXY weak + risk-on = bullish oil, DXY strong + risk-off = bearish oil."""
    try:
        from plugin_td_indicators import fetch_cross_asset_stress
        data = fetch_cross_asset_stress()
    except Exception:
        return "neutral", {"error": "fetch_failed"}

    syms = (data or {}).get("symbols") or {}
    uup = syms.get("UUP") or {}
    spy = syms.get("SPY") or {}

    uup_state = uup.get("state")
    spy_state = spy.get("state")

    if uup_state in ("oversold", "mild bear") and spy_state in ("overbought", "mild bull"):
        return "bullish", {"uup": uup_state, "spy": spy_state, "reason": "DXY weak + risk-on"}
    if uup_state in ("overbought", "mild bull") and spy_state in ("oversold", "mild bear"):
        return "bearish", {"uup": uup_state, "spy": spy_state, "reason": "DXY strong + risk-off"}
    return "neutral", {"uup": uup_state, "spy": spy_state, "reason": "mixed macro"}


def _sig_session_regime() -> tuple[str, dict]:
    """High-liquidity session = neutral boost; low-liquidity dampens."""
    try:
        from plugin_market_sessions import get_market_state
        data = get_market_state()
    except Exception:
        return "neutral", {"error": "fetch_failed"}

    regime = data.get("regime", "unknown")
    # The session never votes bullish or bearish on its own — it just
    # makes or breaks the gatekeeper. Return neutral but include info.
    return "neutral", {"regime": regime, "sizing_multiplier": data.get("sizing_multiplier")}


def _sig_bbands_squeeze() -> tuple[str, dict]:
    """BBands squeeze + breakout direction."""
    try:
        from plugin_td_indicators import fetch_wti_indicators
        data = fetch_wti_indicators(interval="5min")
    except Exception:
        return "neutral", {"error": "fetch_failed"}

    bb = (data or {}).get("bbands") or {}
    latest = bb.get("latest") or {}
    if not latest:
        return "neutral", {"error": "no bb data"}

    try:
        upper = float(latest["upper_band"])
        middle = float(latest["middle_band"])
        lower = float(latest["lower_band"])
    except (KeyError, TypeError, ValueError):
        return "neutral", {"error": "bad bb values"}

    width_pct = (upper - lower) / middle * 100 if middle else None
    if width_pct is None or width_pct > 0.8:
        return "neutral", {"width_pct": round(width_pct, 3) if width_pct is not None else None, "reason": "no squeeze"}

    # Squeeze is on — breakout direction not known from this endpoint
    # alone. Stay neutral but mark the squeeze so the why-text can surface it.
    return "neutral", {
        "width_pct": round(width_pct, 3),
        "upper": round(upper, 3),
        "lower": round(lower, 3),
        "reason": f"BB squeeze {width_pct:.2f}% wide — breakout imminent",
        "squeeze": True,
    }


# ---------------------------------------------------------------------------
# Gatekeepers — each returns (passes: bool, reason: str, detail: dict)
# ---------------------------------------------------------------------------


def _gate_atr(atr_5m: float) -> tuple[bool, str, dict]:
    passes = atr_5m >= ATR_FLOOR_USD
    return passes, f"ATR ${atr_5m:.3f}", {"atr": round(atr_5m, 3), "floor": ATR_FLOOR_USD}


def _gate_adx() -> tuple[bool, str, dict]:
    try:
        from plugin_td_indicators import fetch_wti_indicators
        data = fetch_wti_indicators(interval="1h")
    except Exception:
        return False, "ADX fetch failed", {}
    adx = (data or {}).get("adx") or {}
    latest = adx.get("latest") or {}
    try:
        val = float(latest.get("adx", 0))
    except (TypeError, ValueError):
        return False, "ADX invalid", {}
    passes = val >= ADX_FLOOR
    return passes, f"ADX {val:.1f}", {"adx": round(val, 1), "floor": ADX_FLOOR}


def _gate_htf_wall(side: str, current_price: float) -> tuple[bool, str, dict]:
    """Check whether we're pressed against a 1h RSI wall against the trade.

    Blocks LONG if 1h RSI > 75 (too extended).
    Blocks SHORT if 1h RSI < 25 (already oversold).
    """
    try:
        from plugin_td_indicators import fetch_wti_indicators
        data = fetch_wti_indicators(interval="1h")
    except Exception:
        return True, "HTF fetch failed — allowing", {}
    rsi = (data or {}).get("rsi") or {}
    latest = rsi.get("latest") or {}
    try:
        val = float(latest.get("rsi", 50))
    except (TypeError, ValueError):
        return True, "HTF RSI invalid — allowing", {}

    if side == "LONG" and val > 75:
        return False, f"1h RSI {val:.0f} overbought — no room for long", {"rsi_1h": round(val, 1)}
    if side == "SHORT" and val < 25:
        return False, f"1h RSI {val:.0f} oversold — no room for short", {"rsi_1h": round(val, 1)}
    return True, f"1h RSI {val:.0f} ok", {"rsi_1h": round(val, 1)}


def _gate_heartbeat_conflict(side: str) -> tuple[bool, str, dict]:
    """Warn if a suggested scalp would fight an open heartbeat-managed campaign.

    This does NOT actually block the verdict — it just downgrades to LEAN
    with a warning, since the scalp is a short-term view and the campaign
    might be longer-term.
    """
    with SessionLocal() as session:
        camps = (
            session.query(Campaign)
            .filter(Campaign.status == "open")
            .all()
        )
        open_sides = [c.side for c in camps]

    if not open_sides:
        return True, "no open campaigns", {"open_campaigns": 0}
    if side in open_sides:
        return True, f"aligned with open {side} campaign", {"open_sides": open_sides}
    return False, f"would hedge open {open_sides[0]} campaign", {"open_sides": open_sides}


# ---------------------------------------------------------------------------
# ATR + level math
# ---------------------------------------------------------------------------

def _compute_atr_and_structural_levels(current_price: float) -> dict:
    """Compute 5m ATR + session high/low for SL snapping."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    with SessionLocal() as session:
        bars = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "twelve",
                OHLCV.timeframe == "5min",
                OHLCV.timestamp >= since,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )
    if len(bars) < 15:
        return {"atr": 0.0, "session_high": current_price, "session_low": current_price}

    # True-range ATR(14)
    trs = []
    prev_close = bars[0].close
    for b in bars[1:15]:
        tr = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        trs.append(tr)
        prev_close = b.close
    atr = sum(trs) / len(trs) if trs else 0.0

    # Last 30 min high/low for structural SL
    rt_cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
    rt_bars = [b for b in bars if b.timestamp >= rt_cutoff] or bars[-6:]
    session_high = max(b.high for b in rt_bars)
    session_low = min(b.low for b in rt_bars)

    return {
        "atr": round(atr, 4),
        "session_high": round(session_high, 3),
        "session_low": round(session_low, 3),
    }


def _compute_trade_levels(side: str, current_price: float, structural: dict) -> dict | None:
    """Entry = current, SL = snap to (current ± 1.0*ATR, structural), TP1/TP2 from ATR multiples."""
    atr = structural.get("atr") or 0.0
    if atr <= 0:
        return None

    if side == "LONG":
        atr_sl = current_price - 1.0 * atr
        struct_sl = structural.get("session_low") or atr_sl
        sl = min(atr_sl, struct_sl) - 0.02  # tiny buffer
        tp1 = current_price + 1.5 * atr
        tp2 = current_price + 2.5 * atr
    else:
        atr_sl = current_price + 1.0 * atr
        struct_sl = structural.get("session_high") or atr_sl
        sl = max(atr_sl, struct_sl) + 0.02
        tp1 = current_price - 1.5 * atr
        tp2 = current_price - 2.5 * atr

    risk = abs(current_price - sl)
    if risk <= 0:
        return None
    rr_tp1 = round(abs(tp1 - current_price) / risk, 2)
    rr_tp2 = round(abs(tp2 - current_price) / risk, 2)

    return {
        "entry": round(current_price, 3),
        "stop_loss": round(sl, 3),
        "take_profit_1": round(tp1, 3),
        "take_profit_2": round(tp2, 3),
        "rr_tp1": rr_tp1,
        "rr_tp2": rr_tp2,
        "risk_per_contract": round(risk, 3),
    }


# ---------------------------------------------------------------------------
# Main aggregator
# ---------------------------------------------------------------------------


def _compute_scalp_brain() -> dict:
    # Current price from the freshest bar
    with SessionLocal() as session:
        row = (
            session.query(OHLCV)
            .filter(OHLCV.source == "twelve", OHLCV.timeframe == "1min")
            .order_by(OHLCV.timestamp.desc())
            .first()
        )
    if row is None:
        return {"error": "no price data"}
    current_price = float(row.close)

    # Run each signal and collect its vote
    signals: dict[str, dict] = {}

    def _vote(key: str, fn, *args):
        try:
            bias, detail = fn(*args)
        except Exception:
            logger.exception("scalp_brain signal %s crashed", key)
            bias, detail = "neutral", {"error": "crashed"}
        signals[key] = {
            "bias": bias,
            "weight": WEIGHTS[key],
            "detail": detail,
        }

    _vote("multi_tf_rsi", _sig_multi_tf_rsi)
    _vote("vwap_bands", _sig_vwap_bands, current_price)
    _vote("session_range_pos", _sig_session_range_pos, current_price)
    _vote("opening_range_bo", _sig_opening_range_breakout, current_price)
    _vote("cvd", _sig_cvd)
    _vote("orderbook_imbalance", _sig_orderbook_imbalance)
    _vote("whale_bias", _sig_whale_bias)
    _vote("conviction_trend", _sig_conviction_trend)
    _vote("cross_asset_stress", _sig_cross_asset_stress)
    _vote("session_regime", _sig_session_regime)
    _vote("bbands_squeeze", _sig_bbands_squeeze)

    # Tally weights
    bullish_weight = sum(s["weight"] for s in signals.values() if s["bias"] == "bullish")
    bearish_weight = sum(s["weight"] for s in signals.values() if s["bias"] == "bearish")
    long_pct = bullish_weight / 100
    short_pct = bearish_weight / 100
    bias_pct = long_pct - short_pct

    # Preliminary verdict
    if bias_pct >= 0.30:
        preliminary = "LONG"
    elif bias_pct <= -0.30:
        preliminary = "SHORT"
    elif bias_pct >= 0.15:
        preliminary = "LEAN_LONG"
    elif bias_pct <= -0.15:
        preliminary = "LEAN_SHORT"
    else:
        preliminary = "WAIT"

    # Structural + ATR + gatekeepers
    structural = _compute_atr_and_structural_levels(current_price)
    atr = structural["atr"]

    # Intended side for level + gate evaluation
    intended_side = (
        "LONG" if preliminary in ("LONG", "LEAN_LONG") else
        "SHORT" if preliminary in ("SHORT", "LEAN_SHORT") else
        None
    )

    gatekeepers: dict[str, dict] = {}
    gate_atr_ok, gate_atr_msg, gate_atr_detail = _gate_atr(atr)
    gatekeepers["atr"] = {"ok": gate_atr_ok, "message": gate_atr_msg, **gate_atr_detail}
    gate_adx_ok, gate_adx_msg, gate_adx_detail = _gate_adx()
    gatekeepers["adx"] = {"ok": gate_adx_ok, "message": gate_adx_msg, **gate_adx_detail}

    if intended_side:
        gate_htf_ok, gate_htf_msg, gate_htf_detail = _gate_htf_wall(intended_side, current_price)
        gatekeepers["htf_wall"] = {"ok": gate_htf_ok, "message": gate_htf_msg, **gate_htf_detail}
        gate_hb_ok, gate_hb_msg, gate_hb_detail = _gate_heartbeat_conflict(intended_side)
        gatekeepers["heartbeat"] = {"ok": gate_hb_ok, "message": gate_hb_msg, **gate_hb_detail}
    else:
        gatekeepers["htf_wall"] = {"ok": True, "message": "n/a — no side"}
        gatekeepers["heartbeat"] = {"ok": True, "message": "n/a — no side"}

    gates_passed = sum(1 for g in gatekeepers.values() if g.get("ok"))
    gates_total = len(gatekeepers)

    # Apply downgrade rules
    verdict = preliminary
    downgrade_reason = None
    if preliminary in ("LONG", "SHORT"):
        if gates_passed < 3:
            verdict = "LEAN_LONG" if preliminary == "LONG" else "LEAN_SHORT"
            downgrade_reason = f"downgraded: only {gates_passed}/{gates_total} gatekeepers pass"
        if gates_passed < 2:
            verdict = "WAIT"
            downgrade_reason = f"downgraded: only {gates_passed}/{gates_total} gatekeepers pass"

    # Trade levels only when we have a side and ATR is valid
    trade_levels = None
    if intended_side and atr > 0:
        trade_levels = _compute_trade_levels(intended_side, current_price, structural)
        # Final filter: insist on R:R >= 1.5 for a NOW verdict
        if trade_levels and verdict in ("LONG", "SHORT"):
            rr = trade_levels.get("rr_tp1") or 0
            if rr < 1.5:
                verdict = "LEAN_LONG" if verdict == "LONG" else "LEAN_SHORT"
                downgrade_reason = f"downgraded: TP1 R:R {rr:.2f} < 1.5"

    # Why-text — pick up to 4 strongest contributing signals
    contributors = [
        (k, s) for k, s in signals.items()
        if s["bias"] in ("bullish", "bearish") and s["bias"] == (
            "bullish" if intended_side == "LONG" else "bearish" if intended_side == "SHORT" else s["bias"]
        )
    ]
    contributors.sort(key=lambda kv: kv[1]["weight"], reverse=True)
    why_lines = []
    for k, s in contributors[:4]:
        reason = s["detail"].get("reason") or s["detail"].get("note") or ""
        why_lines.append(f"{k.replace('_', ' ')}: {reason}")
    why_text = " · ".join(why_lines) if why_lines else (
        "Mixed signals; no clean scalp opportunity."
    )

    # Conviction 0-100 as the max of long_pct or short_pct scaled to 100
    conviction_pct = round(max(long_pct, short_pct) * 100, 0)

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "current_price": round(current_price, 3),
        "verdict": verdict,
        "preliminary_verdict": preliminary,
        "downgrade_reason": downgrade_reason,
        "intended_side": intended_side,
        "conviction_pct": conviction_pct,
        "bias_pct": round(bias_pct, 3),
        "long_pct": round(long_pct, 3),
        "short_pct": round(short_pct, 3),
        "atr_5m": round(atr, 4),
        "structural": structural,
        "signals": signals,
        "gatekeepers": gatekeepers,
        "gates_passed": gates_passed,
        "gates_total": gates_total,
        "trade_levels": trade_levels,
        "why": why_text,
    }


def get_scalp_brain(force: bool = False) -> dict:
    """Public entry — 10s-cached scalp brain verdict."""
    global _CACHE, _CACHE_TS
    now = time.time()
    with _CACHE_LOCK:
        if not force and _CACHE is not None and (now - _CACHE_TS) < _CACHE_TTL_SECONDS:
            return {**_CACHE, "cache_age_seconds": round(now - _CACHE_TS, 1)}

    result = _compute_scalp_brain()

    with _CACHE_LOCK:
        _CACHE = result
        _CACHE_TS = now
    return {**result, "cache_age_seconds": 0.0}

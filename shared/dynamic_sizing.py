"""Dynamic position-size multiplier + equity cap.

Computes a multiplier in [MIN_SIZE_MULTIPLIER, MAX_SIZE_MULTIPLIER] from
current market state. Used by shared.position_manager.open_new_campaign
so every Campaign row carries a stored multiplier that subsequent DCA
layers also use.

Design principles:
  - Reads only from the shared DB (no dashboard plugin dependencies)
  - Safe default on any failure: return 1.0 + "neutral" reason
  - Fully explainable — returns the list of reasons that moved the
    multiplier up or down so the user can audit in the dashboard
  - Combined with an equity cap so the bot can never actually exceed
    MAX_TOTAL_EXPOSURE_PCT regardless of what the multiplier asks for
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc

from shared.models.base import SessionLocal
from shared.sizing import (
    MAX_SIZE_MULTIPLIER,
    MAX_TOTAL_EXPOSURE_PCT,
    MIN_SIZE_MULTIPLIER,
    clamp_multiplier,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State gathering — DB-only so this is safe to call from ai-brain or shared
# ---------------------------------------------------------------------------

def _gather_sizing_state(side: str | None = None) -> dict:
    """Pull every input to the size multiplier from the DB."""
    from shared.account_manager import recompute_account_state
    from shared.models.binance_metrics import BinanceFundingRate
    from shared.models.ohlcv import OHLCV
    from shared.models.signals import AnalysisScore

    state: dict = {}
    if side is not None:
        state["side"] = side.upper()

    # Account
    try:
        acc = recompute_account_state()
        state["equity"] = acc.get("equity") or 0
        state["cash"] = acc.get("cash") or 0
        state["margin_used"] = acc.get("margin_used") or 0
        state["drawdown_pct"] = acc.get("account_drawdown_pct") or 0
    except Exception:
        logger.exception("dynamic_sizing: account state fetch failed")
        state["equity"] = 0

    with SessionLocal() as session:
        scores_row = (
            session.query(AnalysisScore)
            .order_by(desc(AnalysisScore.timestamp))
            .first()
        )
        if scores_row:
            state["unified_score"] = scores_row.unified_score
            state["technical_score"] = scores_row.technical_score
            state["sentiment_score"] = scores_row.sentiment_score

        fr = (
            session.query(BinanceFundingRate)
            .order_by(desc(BinanceFundingRate.funding_time))
            .first()
        )
        if fr and fr.funding_rate is not None:
            state["funding_rate_pct"] = round(fr.funding_rate * 100, 4)

        # 5-minute ATR over the last 4 hours
        since = datetime.now(tz=timezone.utc) - timedelta(hours=4)
        bars = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "binance",
                OHLCV.timeframe == "5min",
                OHLCV.timestamp >= since,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )
        if len(bars) >= 15:
            trs: list[float] = []
            prev_close = bars[0].close
            for b in bars[1:]:
                tr = max(
                    b.high - b.low,
                    abs(b.high - prev_close),
                    abs(b.low - prev_close),
                )
                trs.append(tr)
                prev_close = b.close
            atr = sum(trs[-14:]) / min(14, len(trs))
            current_price = bars[-1].close
            if current_price > 0:
                state["atr_pct_5m"] = round(atr / current_price * 100, 3)
                state["current_price"] = current_price

    return state


# ---------------------------------------------------------------------------
# Multiplier computation — fully explainable
# ---------------------------------------------------------------------------

def compute_size_multiplier(
    state: dict | None = None,
    llm_confidence: float | None = None,
) -> tuple[float, dict]:
    """Compute dynamic size multiplier + reasoning.

    Parameters
    ----------
    state:
        Optional pre-built state dict. If None, we gather from the DB.
    llm_confidence:
        Optional LLM confidence from the recommendation that triggered
        this open (0.0..1.0). Boosts the multiplier when high, penalises
        when low.

    Returns
    -------
    (multiplier, info) where info = {
        "multiplier": float,
        "base": float,
        "reasons": [str, ...],   # human-readable trace
        "state": dict,           # snapshot of inputs used
    }
    """
    if state is None:
        state = _gather_sizing_state()
    if llm_confidence is not None:
        state["llm_confidence"] = llm_confidence

    reasons: list[str] = []

    # ---------- Primary: unified score magnitude ----------
    unified = state.get("unified_score")
    if unified is None:
        base = 1.0
        reasons.append("no unified score → neutral 1.0×")
    else:
        abs_u = abs(unified)
        if abs_u >= 50:
            base = 3.0
            reasons.append(f"strong unified |{unified:.0f}| ≥ 50 → 3.0×")
        elif abs_u >= 30:
            base = 2.0
            reasons.append(f"solid unified |{unified:.0f}| ≥ 30 → 2.0×")
        elif abs_u >= 15:
            base = 1.3
            reasons.append(f"moderate unified |{unified:.0f}| ≥ 15 → 1.3×")
        elif abs_u >= 5:
            base = 1.0
            reasons.append(f"mild unified |{unified:.0f}| → 1.0×")
        else:
            base = 0.6
            reasons.append(f"flat unified |{unified:.0f}| → 0.6×")

    multiplier = base

    # ---------- LLM confidence adjustment ----------
    llm_conf = state.get("llm_confidence")
    if llm_conf is not None:
        if llm_conf >= 0.75:
            multiplier += 0.5
            reasons.append(f"LLM conf {llm_conf:.2f} high → +0.5×")
        elif llm_conf >= 0.65:
            multiplier += 0.2
            reasons.append(f"LLM conf {llm_conf:.2f} solid → +0.2×")
        elif llm_conf < 0.55:
            multiplier -= 0.3
            reasons.append(f"LLM conf {llm_conf:.2f} low → −0.3×")

    # ---------- Contrarian funding bonus ----------
    # If the committee says LONG and funding is deeply negative (shorts
    # crowded, squeeze setup) → bonus. And vice versa.
    side = (state.get("side") or "").upper()
    funding = state.get("funding_rate_pct")
    if side in ("LONG", "SHORT") and funding is not None and abs(funding) >= 0.03:
        if (funding <= -0.03 and side == "LONG") or (funding >= 0.03 and side == "SHORT"):
            multiplier += 0.5
            reasons.append(f"contrarian funding edge {funding:+.4f}% ({side}) → +0.5×")
        elif (funding >= 0.03 and side == "LONG") or (funding <= -0.03 and side == "SHORT"):
            multiplier -= 0.3
            reasons.append(f"fighting funding {funding:+.4f}% ({side}) → −0.3×")

    # ---------- Drawdown penalty ----------
    dd = state.get("drawdown_pct") or 0
    if dd < -20:
        multiplier *= 0.4
        reasons.append(f"heavy drawdown {dd:.1f}% × 0.4 penalty")
    elif dd < -10:
        multiplier *= 0.6
        reasons.append(f"drawdown {dd:.1f}% × 0.6 penalty")
    elif dd < -5:
        multiplier *= 0.8
        reasons.append(f"mild drawdown {dd:.1f}% × 0.8 penalty")

    # ---------- Volatility regime ----------
    atr_pct = state.get("atr_pct_5m") or 0
    if atr_pct >= 1.0:
        multiplier *= 0.75
        reasons.append(f"wide 5m ATR {atr_pct:.2f}% × 0.75")
    elif atr_pct >= 0.5:
        pass  # normal
    elif atr_pct > 0:
        multiplier *= 1.1
        reasons.append(f"tight 5m ATR {atr_pct:.2f}% × 1.1 bonus")

    multiplier = clamp_multiplier(multiplier)

    info = {
        "multiplier": round(multiplier, 3),
        "base": round(base, 3),
        "reasons": reasons,
        "state": {
            "unified_score": state.get("unified_score"),
            "llm_confidence": llm_conf,
            "funding_rate_pct": funding,
            "drawdown_pct": dd,
            "atr_pct_5m": atr_pct,
            "side": side or None,
            "equity": state.get("equity"),
        },
    }
    logger.info(
        "dynamic_sizing: multiplier=%.2f (base=%.2f) — %s",
        multiplier, base, " | ".join(reasons),
    )
    return multiplier, info


# ---------------------------------------------------------------------------
# Equity cap — final safety net
# ---------------------------------------------------------------------------

def apply_equity_cap(requested_margin: float, equity: float, already_locked: float = 0.0) -> float:
    """Cap a requested new-layer margin so total open margin <= MAX_TOTAL_EXPOSURE_PCT.

    Returns the largest allowed margin <= requested.
    If the cap would leave less than $100, returns 0 (refuse to open).
    """
    if equity <= 0:
        return 0.0
    max_total = equity * MAX_TOTAL_EXPOSURE_PCT
    available = max_total - already_locked
    if available <= 100:
        return 0.0
    return min(requested_margin, available)

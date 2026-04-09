"""Smart alerts — confluence-based condition trees.

Each smart alert has an `expression` tree like:
  {"op": "AND", "clauses": [
      {"metric": "funding_rate_pct", "cmp": "<=", "value": -0.03},
      {"metric": "orderbook_imbalance_pct", "cmp": ">=", "value": 30}
  ]}

Supported operators:
  "AND" / "OR" on clause lists
  leaf: {"metric": <name>, "cmp": <cmp>, "value": <num>}
  cmp: "<", "<=", ">", ">=", "==", "!="

Supported metrics (resolved against a live state dict):
  price                     — last CLUSDT close
  technical, fundamental, sentiment, shipping, unified — analysis scores
  conviction_score          — 0..100 composite
  funding_rate_pct          — per 8h
  open_interest, open_interest_change_24h_pct
  top_trader_long_pct, global_retail_long_pct
  retail_delta_pct          — retail_long - smart_long, in pct points
  taker_buysell_ratio
  orderbook_imbalance_pct   — from orderbook depth snapshot
  equity, drawdown_pct      — current account state
  active_anomaly_count, max_anomaly_severity

This plugin provides:
  evaluate_smart_alerts()   — called by the existing alerts_evaluator loop
  create_smart_alert(...)   — CRUD used by API
  list_smart_alerts()
  delete_smart_alert(id)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from sqlalchemy import desc

from shared.config import settings
from shared.models.alerts import Alert
from shared.models.base import SessionLocal
from shared.models.binance_metrics import (
    BinanceFundingRate,
    BinanceLongShortRatio,
    BinanceOpenInterest,
)
from shared.models.ohlcv import OHLCV
from shared.models.signals import AnalysisScore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State snapshot builder
# ---------------------------------------------------------------------------

def _build_state() -> dict[str, Any]:
    """Assemble a flat dict of current values for every metric expressions use."""
    state: dict[str, Any] = {}
    now = datetime.now(tz=timezone.utc)

    try:
        with SessionLocal() as session:
            ohlc = (
                session.query(OHLCV)
                .filter(OHLCV.source == "binance", OHLCV.timeframe == "1min")
                .order_by(desc(OHLCV.timestamp))
                .first()
            )
            if ohlc:
                state["price"] = float(ohlc.close)

            scores = (
                session.query(AnalysisScore)
                .order_by(desc(AnalysisScore.timestamp))
                .first()
            )
            if scores:
                state["technical"] = scores.technical_score
                state["fundamental"] = scores.fundamental_score
                state["sentiment"] = scores.sentiment_score
                state["shipping"] = scores.shipping_score
                state["unified"] = scores.unified_score

            fr = (
                session.query(BinanceFundingRate)
                .order_by(desc(BinanceFundingRate.funding_time))
                .first()
            )
            if fr:
                state["funding_rate_pct"] = round(fr.funding_rate * 100, 4)

            oi_latest = (
                session.query(BinanceOpenInterest)
                .order_by(desc(BinanceOpenInterest.timestamp))
                .first()
            )
            if oi_latest:
                state["open_interest"] = oi_latest.open_interest
            oi_24h = (
                session.query(BinanceOpenInterest)
                .filter(BinanceOpenInterest.timestamp <= now - timedelta(hours=24))
                .order_by(desc(BinanceOpenInterest.timestamp))
                .first()
            )
            if oi_latest and oi_24h and oi_24h.open_interest:
                state["open_interest_change_24h_pct"] = round(
                    (oi_latest.open_interest - oi_24h.open_interest) / oi_24h.open_interest * 100, 3,
                )

            top = (
                session.query(BinanceLongShortRatio)
                .filter(BinanceLongShortRatio.ratio_type == "top_position")
                .order_by(desc(BinanceLongShortRatio.timestamp))
                .first()
            )
            glob = (
                session.query(BinanceLongShortRatio)
                .filter(BinanceLongShortRatio.ratio_type == "global_account")
                .order_by(desc(BinanceLongShortRatio.timestamp))
                .first()
            )
            if top and top.long_pct is not None:
                state["top_trader_long_pct"] = round(top.long_pct * 100, 2)
            if glob and glob.long_pct is not None:
                state["global_retail_long_pct"] = round(glob.long_pct * 100, 2)
            if top and glob and top.long_pct is not None and glob.long_pct is not None:
                state["retail_delta_pct"] = round((glob.long_pct - top.long_pct) * 100, 2)

            taker = (
                session.query(BinanceLongShortRatio)
                .filter(BinanceLongShortRatio.ratio_type == "taker")
                .order_by(desc(BinanceLongShortRatio.timestamp))
                .first()
            )
            if taker:
                state["taker_buysell_ratio"] = round(taker.long_short_ratio, 3)
    except Exception:
        logger.exception("Failed to load DB state for smart alert")

    # Conviction meter
    try:
        from plugin_conviction import compute_conviction
        conv = compute_conviction()
        state["conviction_score"] = conv.get("score")
    except Exception:
        pass

    # Orderbook imbalance (live from Binance REST)
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/depth",
            params={"symbol": settings.binance_symbol or "CLUSDT", "limit": 100},
            timeout=5,
        )
        if r.ok:
            raw = r.json()
            bids = sum(float(q) for _, q in raw.get("bids", []))
            asks = sum(float(q) for _, q in raw.get("asks", []))
            if bids + asks > 0:
                state["orderbook_imbalance_pct"] = round((bids - asks) / (bids + asks) * 100, 2)
    except Exception:
        pass

    # Account state
    try:
        from shared.account_manager import recompute_account_state
        acc = recompute_account_state()
        state["equity"] = acc.get("equity")
        state["drawdown_pct"] = acc.get("account_drawdown_pct")
    except Exception:
        pass

    # Anomalies
    try:
        from plugin_anomalies import detect_anomalies
        anoms = detect_anomalies()
        state["active_anomaly_count"] = len(anoms)
        state["max_anomaly_severity"] = max((a.get("severity", 0) for a in anoms), default=0)
    except Exception:
        pass

    return state


# ---------------------------------------------------------------------------
# Expression evaluator
# ---------------------------------------------------------------------------

_CMP_FNS = {
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _evaluate(expr: dict, state: dict) -> tuple[bool, list[str]]:
    """Return (matched, trace). Trace is a flat list of human-readable strings
    showing which leaf conditions matched or failed, for logging/notification."""
    if not isinstance(expr, dict):
        return False, [f"invalid expression: {expr!r}"]

    op = expr.get("op")
    if op in ("AND", "OR"):
        clauses = expr.get("clauses") or []
        if not clauses:
            return False, ["empty clauses"]
        results: list[tuple[bool, list[str]]] = [_evaluate(c, state) for c in clauses]
        values = [r[0] for r in results]
        trace: list[str] = []
        for ok, sub in results:
            trace.extend(sub)
        if op == "AND":
            return all(values), trace
        return any(values), trace

    # Leaf condition
    metric = expr.get("metric")
    cmp = expr.get("cmp")
    value = expr.get("value")
    if metric is None or cmp not in _CMP_FNS or value is None:
        return False, [f"invalid leaf: {expr!r}"]

    current = state.get(metric)
    if current is None:
        return False, [f"{metric}=N/A · condition not evaluable"]
    try:
        ok = _CMP_FNS[cmp](float(current), float(value))
    except (TypeError, ValueError):
        return False, [f"{metric} type mismatch"]
    icon = "✓" if ok else "✗"
    return ok, [f"{icon} {metric} ({current}) {cmp} {value}"]


# ---------------------------------------------------------------------------
# Evaluator loop hook
# ---------------------------------------------------------------------------

def evaluate_smart_alerts() -> list[dict]:
    """Evaluate every active smart alert against current state.

    Returns a list of alerts that triggered this cycle (for downstream
    notification publishing). Marks them triggered if one_shot.
    """
    now = datetime.now(tz=timezone.utc)
    state = _build_state()
    fired: list[dict] = []

    with SessionLocal() as session:
        active = (
            session.query(Alert)
            .filter(Alert.kind == "smart", Alert.status == "active")
            .all()
        )
        for alert in active:
            if alert.expression is None:
                continue
            ok, trace = _evaluate(alert.expression, state)
            if not ok:
                continue
            fired.append({
                "id": alert.id,
                "message": alert.message,
                "expression": alert.expression,
                "trace": trace,
                "triggered_at": now.isoformat(),
            })
            alert.triggered_at = now
            if alert.one_shot:
                alert.status = "triggered"
        if fired:
            session.commit()

    for f in fired:
        logger.info("Smart alert fired: %s", f)
        try:
            from shared.redis_streams import publish
            publish("alert.triggered", {
                "alert_id": f["id"],
                "kind": "smart",
                "message": f["message"] or "Smart alert triggered",
                "trace": f["trace"],
                "triggered_at": f["triggered_at"],
            })
        except Exception:
            logger.exception("Failed to publish smart alert")

    return fired


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_smart_alert(
    expression: dict,
    message: str | None = None,
    one_shot: bool = True,
) -> dict:
    # Validate by dry-running against current state
    state = _build_state()
    ok, trace = _evaluate(expression, state)

    with SessionLocal() as session:
        row = Alert(
            created_at=datetime.now(tz=timezone.utc),
            kind="smart",
            status="active",
            expression=expression,
            message=message,
            one_shot=one_shot,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        alert_id = row.id

    return {
        "id": alert_id,
        "matches_now": ok,
        "trace": trace,
    }


def list_smart_alerts(status: str | None = None) -> list[dict]:
    with SessionLocal() as session:
        q = session.query(Alert).filter(Alert.kind == "smart")
        if status:
            q = q.filter(Alert.status == status)
        rows = q.order_by(desc(Alert.created_at)).all()
    state = _build_state()
    out = []
    for r in rows:
        current_ok = False
        trace: list[str] = []
        if r.expression:
            try:
                current_ok, trace = _evaluate(r.expression, state)
            except Exception:
                trace = ["eval error"]
        out.append({
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "status": r.status,
            "expression": r.expression,
            "message": r.message,
            "one_shot": r.one_shot,
            "triggered_at": r.triggered_at.isoformat() if r.triggered_at else None,
            "matches_now": current_ok,
            "trace": trace,
        })
    return out


def delete_smart_alert(alert_id: int) -> bool:
    with SessionLocal() as session:
        row = session.query(Alert).filter(
            Alert.id == alert_id, Alert.kind == "smart"
        ).first()
        if row is None:
            return False
        session.delete(row)
        session.commit()
    return True

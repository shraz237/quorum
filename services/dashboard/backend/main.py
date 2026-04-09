"""Dashboard backend — FastAPI service exposing REST + WebSocket endpoints."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import redis as redis_sync
import docker as docker_sdk
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, text

from shared.config import settings
from shared.models.base import Base, SessionLocal, engine
from shared.models.ohlcv import OHLCV
from shared.models.positions import Position
from shared.models.signals import AIRecommendation, AnalysisScore
from shared.position_manager import (
    list_open_positions,
    list_open_campaigns,
    list_campaigns,
    compute_campaign_state,
    close_campaign,
    add_dca_layer,
    get_current_price,
)
from shared.account_manager import recompute_account_state

from chat import stream_chat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Trading Dashboard API", version="1.0.0")


# ---------------------------------------------------------------------------
# API key gate — protects mutating endpoints, log streams, and /api/chat.
# Opt-in: if DASHBOARD_API_KEY is empty (default), auth is a no-op for
# convenient local development. Set it in .env the moment you expose the
# dashboard beyond localhost.
# ---------------------------------------------------------------------------

from fastapi import Header, HTTPException


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = settings.dashboard_api_key or ""
    if not expected:
        return  # no-auth mode
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://localhost:8001",
        "http://localhost:5173",  # vite dev server
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8001",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        logger.info("WebSocket connected — total: %d", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)
            logger.info("WebSocket disconnected — total: %d", len(self.active))

    async def broadcast(self, data: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _score_to_dict(score: AnalysisScore) -> dict[str, Any]:
    return {
        "id": score.id,
        "timestamp": score.timestamp.isoformat(),
        "technical_score": score.technical_score,
        "fundamental_score": score.fundamental_score,
        "sentiment_score": score.sentiment_score,
        "shipping_score": score.shipping_score,
        "unified_score": score.unified_score,
    }


def _rec_to_dict(rec: AIRecommendation) -> dict[str, Any]:
    return {
        "id": rec.id,
        "timestamp": rec.timestamp.isoformat(),
        "action": rec.action,
        "confidence": rec.confidence,
        "unified_score": rec.unified_score,
        "opus_override_score": rec.opus_override_score,
        "entry_price": rec.entry_price,
        "stop_loss": rec.stop_loss,
        "take_profit": rec.take_profit,
        "haiku_summary": rec.haiku_summary,
        "grok_narrative": rec.grok_narrative,
    }


def _ohlcv_to_dict(bar: OHLCV) -> dict[str, Any]:
    return {
        "time": int(bar.timestamp.timestamp()),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/scores/latest")
def get_latest_score() -> dict[str, Any]:
    """Return the most recent AnalysisScore row."""
    db = SessionLocal()
    try:
        row = db.query(AnalysisScore).order_by(desc(AnalysisScore.timestamp)).first()
        if row is None:
            return {"data": None}
        return {"data": _score_to_dict(row)}
    finally:
        db.close()


@app.get("/api/scores/history")
def get_score_history(hours: int = Query(default=24, ge=1, le=720)) -> dict[str, Any]:
    """Return AnalysisScore rows from the last *hours* hours."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        rows = (
            db.query(AnalysisScore)
            .filter(AnalysisScore.timestamp >= cutoff)
            .order_by(AnalysisScore.timestamp)
            .all()
        )
        return {"data": [_score_to_dict(r) for r in rows]}
    finally:
        db.close()


@app.get("/api/signals")
def get_signals(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    """Return the most recent AIRecommendation rows."""
    db = SessionLocal()
    try:
        rows = (
            db.query(AIRecommendation)
            .order_by(desc(AIRecommendation.timestamp))
            .limit(limit)
            .all()
        )
        return {"data": [_rec_to_dict(r) for r in rows]}
    finally:
        db.close()


@app.get("/api/ohlcv")
def get_ohlcv(
    timeframe: str = Query(default="1H"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """Return OHLCV bars from Twelve Data (canonical WTI feed)."""
    db = SessionLocal()
    try:
        rows = (
            db.query(OHLCV)
            .filter(OHLCV.timeframe == timeframe, OHLCV.source == "twelve")
            .order_by(desc(OHLCV.timestamp))
            .limit(limit)
            .all()
        )

        # Skip bars with null OHLC (lightweight-charts can't render them).
        valid = [
            r for r in rows
            if r.open is not None and r.high is not None
            and r.low is not None and r.close is not None
        ]
        ordered = sorted(valid, key=lambda r: r.timestamp)
        return {"data": [_ohlcv_to_dict(r) for r in ordered], "source": "twelve"}
    finally:
        db.close()


@app.get("/api/positions")
def get_positions(status: str | None = Query(default=None)) -> dict[str, Any]:
    """Return positions, optionally filtered by status (open/closed_*)."""
    if status == "open":
        return {"data": list_open_positions()}

    db = SessionLocal()
    try:
        q = db.query(Position).order_by(desc(Position.opened_at))
        if status:
            q = q.filter(Position.status == status)
        rows = q.limit(100).all()
        return {
            "data": [
                {
                    "id": p.id,
                    "side": p.side,
                    "status": p.status,
                    "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                    "closed_at": p.closed_at.isoformat() if p.closed_at else None,
                    "entry_price": p.entry_price,
                    "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
                    "close_price": p.close_price,
                    "realised_pnl": p.realised_pnl,
                    "recommendation_id": p.recommendation_id,
                    "notes": p.notes,
                }
                for p in rows
            ]
        }
    finally:
        db.close()


@app.get("/api/account")
def get_account_endpoint() -> dict[str, Any]:
    """Return current account state (cash, equity, margin, PnL)."""
    try:
        return {"data": recompute_account_state()}
    except Exception as exc:
        logger.exception("get_account_endpoint failed")
        return {"error": str(exc)}


@app.get("/api/conviction")
def get_conviction_endpoint() -> dict[str, Any]:
    """Return the composite conviction reading (0..100 + direction + drivers)."""
    try:
        from plugin_conviction import compute_conviction
        return {"data": compute_conviction()}
    except Exception as exc:
        logger.exception("get_conviction_endpoint failed")
        return {"error": str(exc)}


@app.get("/api/now-brief")
def get_now_brief_endpoint(force: bool = Query(default=False)) -> dict[str, Any]:
    """Return an AI-generated synthesis of the current dashboard state."""
    try:
        from plugin_now_brief import compute_now_brief
        return {"data": compute_now_brief(force=force)}
    except Exception as exc:
        logger.exception("get_now_brief_endpoint failed")
        return {"error": str(exc)}


@app.get("/api/signal-confluence")
def get_signal_confluence_endpoint() -> dict[str, Any]:
    """Return per-signal BULL/BEAR/NEUTRAL classification."""
    try:
        from plugin_now_brief import compute_signal_confluence
        return {"data": compute_signal_confluence()}
    except Exception as exc:
        logger.exception("get_signal_confluence_endpoint failed")
        return {"error": str(exc)}


@app.get("/api/scenario-calculator")
def get_scenario_calculator_endpoint() -> dict[str, Any]:
    """Return PnL/equity/margin snapshots at multiple price levels + key levels."""
    try:
        from plugin_risk_tools import compute_scenarios
        return {"data": compute_scenarios()}
    except Exception as exc:
        logger.exception("scenario calculator failed")
        return {"error": str(exc)}


@app.get("/api/monte-carlo")
def get_monte_carlo_endpoint(
    horizon_hours: int = Query(default=24, ge=1, le=168),
    n_paths: int = Query(default=2000, ge=100, le=10000),
) -> dict[str, Any]:
    """Return GBM Monte Carlo simulation of margin-call probability."""
    try:
        from plugin_risk_tools import simulate_margin_call
        return {"data": simulate_margin_call(horizon_hours=horizon_hours, n_paths=n_paths)}
    except Exception as exc:
        logger.exception("monte carlo failed")
        return {"error": str(exc)}


@app.get("/api/upcoming-events")
def get_upcoming_events_endpoint(days: int = Query(default=7, ge=1, le=30)) -> dict[str, Any]:
    """Return upcoming economic events relevant to oil."""
    try:
        from plugin_analytics import _get_upcoming_events
        return {"data": _get_upcoming_events(days=days)}
    except Exception as exc:
        logger.exception("upcoming events failed")
        return {"error": str(exc)}


from pydantic import BaseModel as _BaseModel

class _SmartAlertIn(_BaseModel):
    expression: dict
    message: str | None = None
    one_shot: bool = True


@app.get("/api/smart-alerts")
def list_smart_alerts_endpoint(status: str | None = Query(default=None)) -> dict[str, Any]:
    try:
        from plugin_smart_alerts import list_smart_alerts
        return {"data": list_smart_alerts(status=status)}
    except Exception as exc:
        logger.exception("list smart alerts failed")
        return {"error": str(exc)}


@app.post("/api/smart-alerts", dependencies=[Depends(require_api_key)])
def create_smart_alert_endpoint(payload: _SmartAlertIn) -> dict[str, Any]:
    try:
        from plugin_smart_alerts import create_smart_alert
        return {"data": create_smart_alert(
            expression=payload.expression,
            message=payload.message,
            one_shot=payload.one_shot,
        )}
    except Exception as exc:
        logger.exception("create smart alert failed")
        return {"error": str(exc)}


@app.delete("/api/smart-alerts/{alert_id}", dependencies=[Depends(require_api_key)])
def delete_smart_alert_endpoint(alert_id: int) -> dict[str, Any]:
    try:
        from plugin_smart_alerts import delete_smart_alert
        return {"data": {"deleted": delete_smart_alert(alert_id)}}
    except Exception as exc:
        logger.exception("delete smart alert failed")
        return {"error": str(exc)}


@app.post("/api/smart-alerts/evaluate", dependencies=[Depends(require_api_key)])
def evaluate_smart_alerts_endpoint() -> dict[str, Any]:
    """Manual trigger — evaluate all smart alerts right now."""
    try:
        from plugin_smart_alerts import evaluate_smart_alerts
        return {"data": {"fired": evaluate_smart_alerts()}}
    except Exception as exc:
        logger.exception("evaluate smart alerts failed")
        return {"error": str(exc)}


@app.get("/api/pattern-match")
def get_pattern_match_endpoint(top_n: int = Query(default=10, ge=1, le=50)) -> dict[str, Any]:
    """Return historical snapshots most similar to current market state + their forward returns."""
    try:
        from plugin_learning import find_similar_moments
        return {"data": find_similar_moments(top_n=top_n)}
    except Exception as exc:
        logger.exception("pattern match failed")
        return {"error": str(exc)}


@app.get("/api/signal-performance")
def get_signal_performance_endpoint() -> dict[str, Any]:
    """Return per-feature bucket forward-return statistics."""
    try:
        from plugin_learning import compute_signal_performance
        return {"data": compute_signal_performance()}
    except Exception as exc:
        logger.exception("signal performance failed")
        return {"error": str(exc)}


@app.get("/api/trade-journal")
def get_trade_journal_endpoint(
    limit: int = Query(default=50, ge=1, le=500),
    include_open: bool = Query(default=False),
) -> dict[str, Any]:
    """Return closed campaigns with snapshots + aggregate performance stats."""
    try:
        from plugin_trade_journal import get_journal
        return {"data": get_journal(limit=limit, include_open=include_open)}
    except Exception as exc:
        logger.exception("trade journal failed")
        return {"error": str(exc)}


@app.get("/api/cross-assets")
def get_cross_assets_endpoint(hours: int = Query(default=24, ge=1, le=168)) -> dict[str, Any]:
    """Return latest values / 1h / 24h change / correlation for DXY/SPX/Gold/BTC/VIX."""
    try:
        from plugin_cross_cvd import cross_asset_snapshot
        return {"data": cross_asset_snapshot(hours=hours)}
    except Exception as exc:
        logger.exception("cross-assets failed")
        return {"error": str(exc)}


@app.get("/api/cvd")
def get_cvd_endpoint(minutes: int = Query(default=60, ge=5, le=500)) -> dict[str, Any]:
    """Return Cumulative Volume Delta series for CLUSDT."""
    try:
        from plugin_cross_cvd import cvd_series
        return {"data": cvd_series(minutes=minutes)}
    except Exception as exc:
        logger.exception("cvd failed")
        return {"error": str(exc)}


@app.get("/api/ticker")
def get_live_ticker_endpoint() -> dict[str, Any]:
    """Cheap read of the background live-ticker cache (no TD call).

    The plugin_live_ticker worker polls Twelve Data /quote every 3 sec
    and keeps a single in-memory snapshot. This endpoint just returns
    that snapshot so the frontend can poll it cheaply (1-2 sec) without
    burning Twelve Data credits per client refresh.
    """
    try:
        from plugin_live_ticker import get_cached_ticker
        return {"data": get_cached_ticker()}
    except Exception as exc:
        logger.exception("live ticker endpoint failed")
        return {"error": str(exc)}


@app.get("/api/td-indicators/wti")
def get_td_indicators_wti(interval: str = Query(default="1h")) -> dict[str, Any]:
    """Twelve Data pre-computed RSI/MACD/ATR/ADX/BBANDS for WTI."""
    try:
        from plugin_td_indicators import fetch_wti_indicators
        return {"data": fetch_wti_indicators(interval=interval)}
    except Exception as exc:
        logger.exception("td-indicators wti failed")
        return {"error": str(exc)}


@app.get("/api/td-indicators/cross-stress")
def get_td_cross_stress_endpoint() -> dict[str, Any]:
    """1h RSI for SPY, BTC/USD, UUP — macro stress barometers."""
    try:
        from plugin_td_indicators import fetch_cross_asset_stress
        return {"data": fetch_cross_asset_stress()}
    except Exception as exc:
        logger.exception("td-cross-stress failed")
        return {"error": str(exc)}


@app.get("/api/market-sessions")
def get_market_sessions_endpoint() -> dict[str, Any]:
    """Current global market session state + sizing-regime label."""
    try:
        from plugin_market_sessions import get_market_state
        return {"data": get_market_state()}
    except Exception as exc:
        logger.exception("market-sessions failed")
        return {"error": str(exc)}


@app.get("/api/scalp-brain")
def get_scalp_brain_endpoint() -> dict[str, Any]:
    """Ultimate scalper verdict — stitches every intraday signal into
    one LONG NOW / SHORT NOW / LEAN / WAIT answer with entry/SL/TP levels.

    Reads are 10-second cached so repeated polls don't hammer downstream
    services. Also fires a Telegram alert the first time the verdict
    transitions into LONG NOW or SHORT NOW (5-minute cooldown per side).
    """
    try:
        from plugin_scalp_brain import get_scalp_brain
        data = get_scalp_brain()

        # Alert-on-transition: only when the verdict actually changes into
        # a NOW state from something else. Cooldown per side via Redis.
        _maybe_fire_scalp_brain_alert(data)

        return {"data": data}
    except Exception as exc:
        logger.exception("scalp-brain endpoint failed")
        return {"error": str(exc)}


_SCALP_BRAIN_ALERT_COOLDOWN_SECONDS = 300  # 5 minutes per side
_SCALP_BRAIN_LAST_VERDICT_KEY = "scalp_brain:last_verdict"
_SCALP_BRAIN_LAST_ALERT_KEY = "scalp_brain:last_alert_ts:{side}"


def _maybe_fire_scalp_brain_alert(data: dict) -> None:
    """Publish a scalp_brain_alert event when the verdict first turns NOW.

    Side-effect only — silently swallows errors (alerting must never break
    the read endpoint).
    """
    verdict = data.get("verdict")
    if verdict not in ("LONG", "SHORT"):
        return
    try:
        from shared.redis_streams import get_redis, publish
        import time as _time
        r = get_redis()

        # Read previous verdict
        prev_raw = r.get(_SCALP_BRAIN_LAST_VERDICT_KEY)
        if isinstance(prev_raw, bytes):
            prev = prev_raw.decode("utf-8")
        else:
            prev = prev_raw if prev_raw else None
        r.set(_SCALP_BRAIN_LAST_VERDICT_KEY, verdict)

        # Only alert on transition into NOW state (not every poll while in it)
        if prev == verdict:
            return

        # Per-side cooldown
        side_key = _SCALP_BRAIN_LAST_ALERT_KEY.format(side=verdict)
        last_raw = r.get(side_key)
        if isinstance(last_raw, bytes):
            last = last_raw.decode("utf-8")
        else:
            last = last_raw if last_raw else None
        now_ts = _time.time()
        if last is not None:
            try:
                if (now_ts - float(last)) < _SCALP_BRAIN_ALERT_COOLDOWN_SECONDS:
                    return
            except (TypeError, ValueError):
                pass
        r.set(side_key, str(now_ts))

        levels = data.get("trade_levels") or {}
        publish(
            "position.event",
            {
                "type": "scalp_brain_alert",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "verdict": verdict,
                "current_price": data.get("current_price"),
                "conviction_pct": data.get("conviction_pct"),
                "entry": levels.get("entry"),
                "stop_loss": levels.get("stop_loss"),
                "take_profit_1": levels.get("take_profit_1"),
                "take_profit_2": levels.get("take_profit_2"),
                "rr_tp1": levels.get("rr_tp1"),
                "why": data.get("why"),
            },
        )
    except Exception:
        logger.exception("scalp-brain alert publish failed")


@app.get("/api/scalping-range")
def get_scalping_range_endpoint(
    timeframe: str = Query(default="5min"),
    lookback_hours: int = Query(default=2, ge=1, le=24),
) -> dict[str, Any]:
    """Short-timeframe scalping range + suggested long/short entries with SL/TP."""
    try:
        from plugin_scalping import compute_scalping_range
        return {"data": compute_scalping_range(timeframe=timeframe, lookback_hours=lookback_hours)}
    except Exception as exc:
        logger.exception("scalping range failed")
        return {"error": str(exc)}


@app.get("/api/vwap")
def get_vwap_endpoint(
    timeframe: str = Query(default="1H"),
    hours: int = Query(default=24, ge=1, le=168),
) -> dict[str, Any]:
    """Return VWAP for a given timeframe/lookback."""
    try:
        from plugin_analytics import _get_vwap
        return {"data": _get_vwap(timeframe=timeframe, hours=hours)}
    except Exception as exc:
        logger.exception("vwap failed")
        return {"error": str(exc)}


@app.get("/api/anomalies")
def get_anomalies_endpoint(hours: int = Query(default=24, ge=1, le=168)) -> dict[str, Any]:
    """Return currently-active extreme anomalies and recent history."""
    try:
        from plugin_anomalies import detect_anomalies, get_anomaly_history
        return {
            "data": {
                "current": detect_anomalies(),
                "history": get_anomaly_history(hours=hours),
            }
        }
    except Exception as exc:
        logger.exception("get_anomalies_endpoint failed")
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Binance derived metrics endpoints
# ---------------------------------------------------------------------------

from shared.models.binance_metrics import (
    BinanceFundingRate,
    BinanceOpenInterest,
    BinanceLongShortRatio,
    BinanceLiquidation,
)


def _symbol() -> str:
    return (settings.binance_symbol or "CLUSDT").upper()


@app.get("/api/funding-rate")
def get_funding_rate(hours: int = Query(default=168, ge=1, le=24 * 30)) -> dict[str, Any]:
    """Historical funding rates for the configured perpetual. Default 7 days."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        rows = (
            session.query(BinanceFundingRate)
            .filter(
                BinanceFundingRate.symbol == _symbol(),
                BinanceFundingRate.funding_time >= since,
            )
            .order_by(BinanceFundingRate.funding_time.asc())
            .all()
        )
    series = [
        {
            "time": int(r.funding_time.timestamp()),
            "rate": r.funding_rate,
            "rate_pct": round(r.funding_rate * 100, 4),
            "mark_price": r.mark_price,
        }
        for r in rows
    ]
    latest = series[-1] if series else None
    return {"data": {"symbol": _symbol(), "latest": latest, "series": series}}


@app.get("/api/open-interest")
def get_open_interest(hours: int = Query(default=24, ge=1, le=24 * 30)) -> dict[str, Any]:
    """Open-interest history. Default 24h."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        rows = (
            session.query(BinanceOpenInterest)
            .filter(
                BinanceOpenInterest.symbol == _symbol(),
                BinanceOpenInterest.timestamp >= since,
            )
            .order_by(BinanceOpenInterest.timestamp.asc())
            .all()
        )
    series = [
        {
            "time": int(r.timestamp.timestamp()),
            "open_interest": r.open_interest,
            "open_interest_value_usd": r.open_interest_value_usd,
        }
        for r in rows
    ]
    latest = series[-1] if series else None
    prev = series[0] if series else None
    change_pct = None
    if latest and prev and prev["open_interest"]:
        change_pct = round(
            (latest["open_interest"] - prev["open_interest"]) / prev["open_interest"] * 100,
            2,
        )
    return {
        "data": {
            "symbol": _symbol(),
            "latest": latest,
            "change_pct_over_window": change_pct,
            "series": series,
        }
    }


@app.get("/api/long-short-ratio")
def get_long_short_ratio(hours: int = Query(default=24, ge=1, le=24 * 30)) -> dict[str, Any]:
    """Long/short positioning ratios: top traders, global accounts, taker flow."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        rows = (
            session.query(BinanceLongShortRatio)
            .filter(
                BinanceLongShortRatio.symbol == _symbol(),
                BinanceLongShortRatio.timestamp >= since,
            )
            .order_by(BinanceLongShortRatio.timestamp.asc())
            .all()
        )
    buckets: dict[str, list] = {"top_position": [], "global_account": [], "taker": []}
    for r in rows:
        buckets.setdefault(r.ratio_type, []).append({
            "time": int(r.timestamp.timestamp()),
            "long_pct": r.long_pct,
            "short_pct": r.short_pct,
            "ratio": r.long_short_ratio,
            "buy_volume": r.buy_volume,
            "sell_volume": r.sell_volume,
        })

    def _latest(series: list) -> dict | None:
        return series[-1] if series else None

    return {
        "data": {
            "symbol": _symbol(),
            "latest": {
                "top_position": _latest(buckets["top_position"]),
                "global_account": _latest(buckets["global_account"]),
                "taker": _latest(buckets["taker"]),
            },
            "series": buckets,
        }
    }


@app.get("/api/orderbook")
def get_orderbook(depth: int = Query(default=100, ge=5, le=500)) -> dict[str, Any]:
    """Proxy Binance CLUSDT depth snapshot with bid/ask wall aggregation."""
    import requests
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/depth",
            params={"symbol": _symbol(), "limit": depth},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as exc:
        logger.exception("orderbook proxy failed")
        return {"error": str(exc)}

    bids = [(float(p), float(q)) for p, q in raw.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in raw.get("asks", [])]
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
    spread = (best_ask - best_bid) if best_bid and best_ask else None

    total_bid_vol = sum(q for _, q in bids)
    total_ask_vol = sum(q for _, q in asks)
    imbalance = (
        (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol)
        if (total_bid_vol + total_ask_vol) > 0
        else 0.0
    )

    return {
        "data": {
            "symbol": _symbol(),
            "mid": mid,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "total_bid_volume": round(total_bid_vol, 2),
            "total_ask_volume": round(total_ask_vol, 2),
            "imbalance": round(imbalance, 4),
            "bids": [{"price": p, "qty": q} for p, q in bids],
            "asks": [{"price": p, "qty": q} for p, q in asks],
            "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    }


@app.get("/api/whale-trades")
def get_whale_trades(
    limit: int = Query(default=1000, ge=1, le=1000),
    min_usd: float = Query(default=50_000, ge=1_000),
) -> dict[str, Any]:
    """Recent aggregated trades on CLUSDT, filtered to whales (>= min_usd)."""
    import requests
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/aggTrades",
            params={"symbol": _symbol(), "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as exc:
        logger.exception("whale trades fetch failed")
        return {"error": str(exc)}

    trades = []
    buy_usd = 0.0
    sell_usd = 0.0
    for row in raw:
        try:
            price = float(row["p"])
            qty = float(row["q"])
            quote = price * qty
            is_buyer_maker = bool(row.get("m", False))
            side = "SELL" if is_buyer_maker else "BUY"
            ts_ms = int(row["T"])
        except (KeyError, ValueError, TypeError):
            continue
        if quote < min_usd:
            continue
        trades.append({
            "time": ts_ms // 1000,
            "price": price,
            "qty": qty,
            "quote_usd": round(quote, 2),
            "side": side,
        })
        if side == "BUY":
            buy_usd += quote
        else:
            sell_usd += quote

    trades.sort(key=lambda t: t["time"], reverse=True)
    return {
        "data": {
            "symbol": _symbol(),
            "min_usd": min_usd,
            "count": len(trades),
            "buy_volume_usd": round(buy_usd, 2),
            "sell_volume_usd": round(sell_usd, 2),
            "delta_usd": round(buy_usd - sell_usd, 2),
            "trades": trades[:100],  # cap to 100 most recent for payload size
        }
    }


@app.get("/api/volume-profile")
def get_volume_profile(
    timeframe: str = Query(default="5min"),
    hours: int = Query(default=24, ge=1, le=24 * 7),
    buckets: int = Query(default=30, ge=10, le=100),
) -> dict[str, Any]:
    """Horizontal volume histogram computed from existing OHLCV rows."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        rows = (
            session.query(OHLCV)
            .filter(
                OHLCV.source == "twelve",
                OHLCV.timeframe == timeframe,
                OHLCV.timestamp >= since,
            )
            .order_by(OHLCV.timestamp.asc())
            .all()
        )

    if not rows:
        return {"data": {"symbol": _symbol(), "buckets": [], "poc_price": None}}

    prices = [(r.high + r.low + r.close) / 3 for r in rows]  # typical price
    vols = [r.volume or 0.0 for r in rows]
    # Twelve Data commodity feeds return no volume — fall back to
    # equal-weight "time at price" histogram so the POC still reflects
    # where price spent the most time.
    if sum(vols) == 0:
        vols = [1.0] * len(rows)
    p_min = min(prices)
    p_max = max(prices)
    if p_max <= p_min:
        p_max = p_min + 0.01

    bucket_size = (p_max - p_min) / buckets
    hist = [0.0] * buckets
    for typical, v in zip(prices, vols):
        idx = min(buckets - 1, int((typical - p_min) / bucket_size))
        hist[idx] += v

    total_vol = sum(hist)
    poc_idx = max(range(buckets), key=lambda i: hist[i])
    poc_price = p_min + bucket_size * (poc_idx + 0.5)

    # Value Area: smallest contiguous range of buckets around POC containing 70% vol
    target = total_vol * 0.70
    lo, hi = poc_idx, poc_idx
    accum = hist[poc_idx]
    while accum < target and (lo > 0 or hi < buckets - 1):
        left = hist[lo - 1] if lo > 0 else -1
        right = hist[hi + 1] if hi < buckets - 1 else -1
        if left >= right:
            if lo > 0:
                lo -= 1
                accum += hist[lo]
            else:
                hi += 1
                accum += hist[hi]
        else:
            hi += 1
            accum += hist[hi]

    val_price = p_min + bucket_size * lo
    vah_price = p_min + bucket_size * (hi + 1)

    bucket_data = [
        {
            "price_lo": round(p_min + bucket_size * i, 3),
            "price_hi": round(p_min + bucket_size * (i + 1), 3),
            "volume": round(hist[i], 2),
        }
        for i in range(buckets)
    ]

    return {
        "data": {
            "symbol": _symbol(),
            "timeframe": timeframe,
            "hours": hours,
            "total_volume": round(total_vol, 2),
            "poc_price": round(poc_price, 3),
            "value_area_low": round(val_price, 3),
            "value_area_high": round(vah_price, 3),
            "buckets": bucket_data,
        }
    }


@app.get("/api/liquidations")
def get_liquidations(
    hours: int = Query(default=24, ge=1, le=24 * 7),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    """Recent force-liquidation events."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    with SessionLocal() as session:
        rows = (
            session.query(BinanceLiquidation)
            .filter(
                BinanceLiquidation.symbol == _symbol(),
                BinanceLiquidation.timestamp >= since,
            )
            .order_by(BinanceLiquidation.timestamp.desc())
            .limit(limit)
            .all()
        )
    events = [
        {
            "time": int(r.timestamp.timestamp()),
            "side": r.side,
            "price": r.price,
            "executed_qty": r.executed_qty,
            "quote_qty_usd": r.quote_qty_usd,
            "order_status": r.order_status,
        }
        for r in rows
    ]
    # Aggregate stats for the window
    buy_usd = sum(e["quote_qty_usd"] or 0 for e in events if e["side"] == "BUY")
    sell_usd = sum(e["quote_qty_usd"] or 0 for e in events if e["side"] == "SELL")
    return {
        "data": {
            "symbol": _symbol(),
            "window_hours": hours,
            "count": len(events),
            "buy_volume_usd": round(buy_usd, 2),   # shorts liquidated
            "sell_volume_usd": round(sell_usd, 2),  # longs liquidated
            "events": events,
        }
    }


# ---------------------------------------------------------------------------
# LLM usage rollups — token + cost tracker
# ---------------------------------------------------------------------------

@app.get("/api/llm-usage")
def get_llm_usage_endpoint() -> dict[str, Any]:
    """Rollups of every LLM call the bot has made.

    Reads the llm_usage table (populated by shared/llm_usage.py from every
    call site across ai-brain / dashboard / sentiment) and returns:

      - today / yesterday / last_7d / last_30d totals + breakdowns
      - by_call_site, by_model, by_service
      - cache_savings_usd (how much prompt caching has saved)
      - hourly_24h cost sparkline
      - heartbeat_24h skip ratio (how often the hash gate avoids Opus)
    """
    try:
        from plugin_llm_usage import get_llm_usage_rollup
        return {"data": get_llm_usage_rollup()}
    except Exception as exc:
        logger.exception("llm-usage endpoint failed")
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Heartbeat (Opus live position manager)
# ---------------------------------------------------------------------------

@app.get("/api/heartbeat/status")
def get_heartbeat_status_endpoint() -> dict[str, Any]:
    """Return heartbeat enabled flag, last/next run timestamps, recent decisions."""
    try:
        from plugin_heartbeat import get_status
        return {"data": get_status()}
    except Exception as exc:
        logger.exception("heartbeat status failed")
        return {"error": str(exc)}


@app.post("/api/heartbeat/pause", dependencies=[Depends(require_api_key)])
def pause_heartbeat_endpoint() -> dict[str, Any]:
    """Flip the Redis kill-switch to paused. The ai-brain worker reads this on next tick."""
    try:
        from plugin_heartbeat import set_enabled
        return {"data": set_enabled(False)}
    except Exception as exc:
        logger.exception("heartbeat pause failed")
        return {"error": str(exc)}


@app.post("/api/heartbeat/resume", dependencies=[Depends(require_api_key)])
def resume_heartbeat_endpoint() -> dict[str, Any]:
    """Flip the Redis kill-switch to enabled."""
    try:
        from plugin_heartbeat import set_enabled
        return {"data": set_enabled(True)}
    except Exception as exc:
        logger.exception("heartbeat resume failed")
        return {"error": str(exc)}


@app.get("/api/campaigns")
def get_campaigns_endpoint(
    status: str | None = Query(default="open"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Return campaigns filtered by status."""
    try:
        camps = list_campaigns(status=status, limit=limit)
        return {"data": camps}
    except Exception as exc:
        logger.exception("get_campaigns_endpoint failed")
        return {"error": str(exc)}


@app.get("/api/campaigns/{campaign_id}")
def get_campaign_detail_endpoint(campaign_id: int) -> dict[str, Any]:
    """Return full detail for a single campaign."""
    state = compute_campaign_state(campaign_id)
    if state is None:
        return {"error": "campaign not found"}
    return {"data": state}


@app.post("/api/campaigns/{campaign_id}/close", dependencies=[Depends(require_api_key)])
def close_campaign_endpoint(campaign_id: int) -> dict[str, Any]:
    """Manually close a campaign at the current market price."""
    snap = close_campaign(campaign_id, status="closed_manual", notes="Closed via dashboard")
    if snap is None:
        return {"error": "campaign not found or could not be closed (no price?)"}

    # Capture an exit snapshot for the trade journal (best-effort).
    try:
        from plugin_trade_journal import attach_exit_snapshot
        attach_exit_snapshot(campaign_id, reason="manual_close_dashboard")
    except Exception:
        logger.exception("attach_exit_snapshot failed")

    try:
        from shared.redis_streams import publish
        publish(
            "position.event",
            {
                "type": "campaign_manual_close",
                "campaign_id": campaign_id,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            },
        )
    except Exception:
        logger.exception("Failed to publish campaign_manual_close event")

    return {"data": snap}


@app.post("/api/campaigns/{campaign_id}/dca", dependencies=[Depends(require_api_key)])
def add_dca_layer_endpoint(campaign_id: int) -> dict[str, Any]:
    """Manually add the next DCA layer to a campaign."""
    price = get_current_price()
    if price is None:
        return {"error": "no current price available"}

    pos_id = add_dca_layer(campaign_id, price)
    if pos_id is None:
        return {"error": "could not add DCA layer (layers exhausted or campaign not open)"}

    try:
        from shared.redis_streams import publish
        publish(
            "position.event",
            {
                "type": "dca_layer_added",
                "campaign_id": campaign_id,
                "position_id": pos_id,
                "price": price,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            },
        )
    except Exception:
        logger.exception("Failed to publish dca_layer_added event")

    state = compute_campaign_state(campaign_id)
    return {"data": state}


@app.post("/api/positions/{position_id}/close", dependencies=[Depends(require_api_key)])
def close_position_endpoint(position_id: int) -> dict[str, Any]:
    """Manually close an open position at the current market price."""
    from shared.position_manager import close_position, get_current_price

    price = get_current_price()
    if price is None:
        return {"error": "no current price available"}

    snap = close_position(position_id, price, "closed_manual", notes="Closed via dashboard")
    if snap is None:
        return {"error": "position not found or already closed"}

    # Notify the notifier service via Redis stream
    try:
        from shared.redis_streams import publish
        publish("position.event", {"type": "manual_close", **snap, "timestamp": datetime.now(tz=timezone.utc).isoformat()})
    except Exception:
        logger.exception("Failed to publish manual_close event")

    return {"data": snap}


# ---------------------------------------------------------------------------
# Wave 3 endpoints: /api/signals/{id}, /api/knowledge, /api/chat
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


def _parse_key_events(raw: str | None) -> list:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


@app.post("/api/chat", dependencies=[Depends(require_api_key)])
def chat_endpoint(req: ChatRequest):
    """Stream a chat response via Server-Sent Events."""
    return StreamingResponse(
        stream_chat(req.message, req.session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/signals/{signal_id}")
def get_signal_detail(signal_id: int) -> dict[str, Any]:
    """Return full detail for a single AIRecommendation including nearby scores and knowledge."""
    from shared.models.knowledge import KnowledgeSummary

    db = SessionLocal()
    try:
        rec = db.query(AIRecommendation).filter(AIRecommendation.id == signal_id).first()
        if rec is None:
            return {"error": "not found"}

        window_start = rec.timestamp - timedelta(minutes=15)
        window_end = rec.timestamp + timedelta(minutes=15)

        # Closest AnalysisScore row within the ±15-min window
        scores = (
            db.query(AnalysisScore)
            .filter(AnalysisScore.timestamp.between(window_start, window_end))
            .order_by(desc(AnalysisScore.timestamp))
            .first()
        )

        # Knowledge digests within the same window
        nearby_knowledge = (
            db.query(KnowledgeSummary)
            .filter(KnowledgeSummary.timestamp.between(window_start, window_end))
            .order_by(desc(KnowledgeSummary.timestamp))
            .all()
        )

        return {
            "data": {
                "id": rec.id,
                "timestamp": rec.timestamp.isoformat(),
                "action": rec.action,
                "confidence": rec.confidence,
                "unified_score": rec.unified_score,
                "opus_override_score": rec.opus_override_score,
                "analysis_text": rec.analysis_text,
                "base_scenario": rec.base_scenario,
                "alt_scenario": rec.alt_scenario,
                "risk_factors": rec.risk_factors,
                "entry_price": rec.entry_price,
                "stop_loss": rec.stop_loss,
                "take_profit": rec.take_profit,
                "haiku_summary": rec.haiku_summary,
                "grok_narrative": rec.grok_narrative,
                "scores_at_signal": {
                    "technical_score": scores.technical_score,
                    "fundamental_score": scores.fundamental_score,
                    "sentiment_score": scores.sentiment_score,
                    "shipping_score": scores.shipping_score,
                    "unified_score": scores.unified_score,
                } if scores else None,
                "knowledge_summaries_nearby": [
                    {
                        "id": k.id,
                        "timestamp": k.timestamp.isoformat(),
                        "summary": k.summary,
                        "key_events": _parse_key_events(k.key_events),
                        "sentiment_score": k.sentiment_score,
                        "sentiment_label": k.sentiment_label,
                    }
                    for k in nearby_knowledge
                ],
            }
        }
    finally:
        db.close()


@app.get("/api/knowledge")
def get_knowledge(
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=50, ge=1, le=200),
    q: str | None = None,
) -> dict[str, Any]:
    """Browse the knowledge base. Supports hour window, result limit, and keyword filter."""
    from shared.models.knowledge import KnowledgeSummary

    db = SessionLocal()
    try:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        query = db.query(KnowledgeSummary).filter(KnowledgeSummary.timestamp >= cutoff)
        if q:
            query = query.filter(KnowledgeSummary.summary.ilike(f"%{q}%"))
        rows = query.order_by(desc(KnowledgeSummary.timestamp)).limit(limit).all()

        return {
            "data": [
                {
                    "id": k.id,
                    "timestamp": k.timestamp.isoformat(),
                    "source": k.source,
                    "window": k.window,
                    "message_count": k.message_count,
                    "summary": k.summary,
                    "key_events": _parse_key_events(k.key_events),
                    "sentiment_score": k.sentiment_score,
                    "sentiment_label": k.sentiment_label,
                }
                for k in rows
            ]
        }
    finally:
        db.close()


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    """Ping Redis and Postgres."""
    status: dict[str, Any] = {"status": "ok", "checks": {}}

    # Postgres
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        status["checks"]["postgres"] = "ok"
    except Exception as exc:
        status["checks"]["postgres"] = f"error: {exc}"
        status["status"] = "degraded"

    # Redis
    try:
        r = redis_sync.from_url(settings.redis_url, socket_connect_timeout=2)
        r.ping()
        status["checks"]["redis"] = "ok"
    except Exception as exc:
        status["checks"]["redis"] = f"error: {exc}"
        status["status"] = "degraded"

    return status


# ---------------------------------------------------------------------------
# WebSocket endpoint — pushes live updates every 5 s
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            # Send latest snapshot every 5 seconds
            db = SessionLocal()
            try:
                score_row = (
                    db.query(AnalysisScore)
                    .order_by(desc(AnalysisScore.timestamp))
                    .first()
                )
                rec_row = (
                    db.query(AIRecommendation)
                    .order_by(desc(AIRecommendation.timestamp))
                    .first()
                )
                payload: dict[str, Any] = {
                    "type": "update",
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    "latest_score": _score_to_dict(score_row) if score_row else None,
                    "latest_signal": _rec_to_dict(rec_row) if rec_row else None,
                }
            finally:
                db.close()

            await ws.send_json(payload)
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Logs WebSocket — streams live docker logs from all trading-* containers
# ---------------------------------------------------------------------------

# Dedicated executor for blocking docker log streams — avoids exhausting the
# default ThreadPoolExecutor (max 10) after repeated WebSocket reconnects.
_log_executor = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="docker-log")

# Service names we care about (must match container_name prefixes)
_LOG_SERVICES = [
    "data-collector",
    "sentiment",
    "analyzer",
    "ai-brain",
    "notifier",
    "dashboard",
    "postgres",
    "redis",
]


def _get_docker_client() -> docker_sdk.DockerClient | None:
    """Try to create a Docker client. Reads DOCKER_HOST from env (set in docker-compose to use socket-proxy)."""
    try:
        client = docker_sdk.from_env()  # picks up DOCKER_HOST automatically
        client.ping()
        return client
    except Exception as exc:
        logger.warning("Docker client unavailable: %s", exc)
        return None


def _find_trading_containers(client: docker_sdk.DockerClient) -> list:
    """Return all running containers whose name starts with 'trading-'."""
    containers = []
    for c in client.containers.list(all=False):
        if c.name.startswith("trading-"):
            containers.append(c)
    return containers


@app.get("/api/logs", dependencies=[Depends(require_api_key)])
def get_logs(
    service: str | None = Query(default=None),
    lines: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    """Return recent log lines from trading containers.

    If *service* is provided, filter to containers matching that name.
    Otherwise returns lines from all trading containers, tagged with service.
    """
    client = _get_docker_client()
    if client is None:
        return {"data": [], "error": "Docker socket not available"}

    containers = _find_trading_containers(client)
    if service:
        containers = [c for c in containers if service in c.name]

    all_lines: list[dict[str, Any]] = []
    for c in containers:
        try:
            raw = c.logs(tail=lines, timestamps=True, stdout=True, stderr=True)
            text_data = raw.decode("utf-8", errors="replace")
            service_name = c.name.replace("trading-", "").rstrip("-1234567890").rstrip("-")
            for line in text_data.splitlines():
                if not line.strip():
                    continue
                # Docker log format: "2026-04-08T14:52:49.123456789Z message"
                parts = line.split(" ", 1)
                if len(parts) == 2 and "T" in parts[0]:
                    ts, msg = parts
                else:
                    ts, msg = "", line
                all_lines.append({
                    "service": service_name,
                    "timestamp": ts,
                    "message": msg,
                })
        except Exception as exc:
            logger.warning("Failed to read logs for %s: %s", c.name, exc)

    # Sort by timestamp ascending, take last N
    all_lines.sort(key=lambda x: x["timestamp"])
    return {"data": all_lines[-lines:]}


@app.websocket("/ws/logs")
async def websocket_logs(ws: WebSocket) -> None:
    """Stream live docker logs from all trading containers."""
    await ws.accept()
    client = _get_docker_client()
    if client is None:
        await ws.send_json({"error": "Docker socket not available"})
        await ws.close()
        return

    containers = _find_trading_containers(client)
    if not containers:
        await ws.send_json({"error": "No trading containers found"})
        await ws.close()
        return

    loop = asyncio.get_running_loop()
    stop_flag = threading.Event()
    log_streams: list = []  # to close them on disconnect

    async def stream_container(container) -> None:
        service_name = container.name.replace("trading-", "").rstrip("-1234567890").rstrip("-")
        try:
            log_stream = container.logs(stream=True, follow=True, tail=50, timestamps=True)
            log_streams.append(log_stream)

            def _reader():
                try:
                    for raw_line in log_stream:
                        if stop_flag.is_set():
                            break
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                        if not line.strip():
                            continue
                        parts = line.split(" ", 1)
                        if len(parts) == 2 and "T" in parts[0]:
                            ts, msg = parts
                        else:
                            ts, msg = "", line
                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                ws.send_json({"service": service_name, "timestamp": ts, "message": msg}),
                                loop,
                            )
                            future.result(timeout=2)  # wait briefly so we know if WS is dead
                        except Exception:
                            stop_flag.set()
                            break
                except Exception as exc:
                    logger.warning("Log stream error for %s: %s", service_name, exc)

            await loop.run_in_executor(_log_executor, _reader)
        except Exception as exc:
            logger.warning("Stream failed for %s: %s", service_name, exc)

    tasks = [asyncio.create_task(stream_container(c)) for c in containers]

    try:
        while not stop_flag.is_set():
            await asyncio.sleep(10)
            try:
                await ws.send_json({"type": "heartbeat"})
            except Exception:
                stop_flag.set()
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("Logs WebSocket error: %s", exc)
    finally:
        stop_flag.set()
        # Force-close docker log streams so reader threads exit
        for stream in log_streams:
            try:
                stream.close()
            except Exception:
                pass
        for t in tasks:
            t.cancel()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event() -> None:
    logger.info("Dashboard backend starting — DB URL: %s", settings.postgres_url.split("@")[-1] if "@" in settings.postgres_url else "(local)")
    # Launch the learning worker: every 5 min capture a signal snapshot and
    # backfill any forward returns whose horizon has elapsed.
    try:
        from plugin_learning import start_learning_worker
        start_learning_worker()
    except Exception:
        logger.exception("Failed to start learning worker")

    # Launch the live ticker worker: polls Twelve Data /quote every 3 sec
    # and keeps a single in-memory snapshot the /api/ticker endpoint serves.
    try:
        from plugin_live_ticker import start_live_ticker_worker
        start_live_ticker_worker()
    except Exception:
        logger.exception("Failed to start live ticker worker")


# ---------------------------------------------------------------------------
# Serve compiled React frontend (must be mounted last)
# ---------------------------------------------------------------------------
import os as _os

_static_dir = _os.path.join(_os.path.dirname(__file__), "static")
if _os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="frontend")

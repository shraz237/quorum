"""Dashboard backend — FastAPI service exposing REST + WebSocket endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import redis as redis_sync
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, text

from shared.config import settings
from shared.models.base import Base, SessionLocal, engine
from shared.models.ohlcv import OHLCV
from shared.models.signals import AIRecommendation, AnalysisScore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Trading Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    """Return OHLCV bars suitable for Lightweight Charts (sorted ascending by time)."""
    db = SessionLocal()
    try:
        rows = (
            db.query(OHLCV)
            .filter(OHLCV.timeframe == timeframe)
            .order_by(desc(OHLCV.timestamp))
            .limit(limit)
            .all()
        )
        # Return in ascending time order for charts
        rows = list(reversed(rows))
        return {"data": [_ohlcv_to_dict(r) for r in rows]}
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
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event() -> None:
    logger.info("Dashboard backend starting — DB URL: %s", settings.postgres_url)


# ---------------------------------------------------------------------------
# Serve compiled React frontend (must be mounted last)
# ---------------------------------------------------------------------------
import os as _os

_static_dir = _os.path.join(_os.path.dirname(__file__), "static")
if _os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="frontend")

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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, text

from shared.config import settings
from shared.models.base import Base, SessionLocal, engine
from shared.models.ohlcv import OHLCV
from shared.models.positions import Position
from shared.models.signals import AIRecommendation, AnalysisScore
from shared.position_manager import list_open_positions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Trading Dashboard API", version="1.0.0")

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


@app.post("/api/positions/{position_id}/close")
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


@app.get("/api/logs")
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
    logger.info("Dashboard backend starting — DB URL: %s", settings.postgres_url)


# ---------------------------------------------------------------------------
# Serve compiled React frontend (must be mounted last)
# ---------------------------------------------------------------------------
import os as _os

_static_dir = _os.path.join(_os.path.dirname(__file__), "static")
if _os.path.isdir(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="frontend")

"""Live ticker worker — 3-second-cached WTI quote from Twelve Data.

Twelve Data Grow plan ($29) is REST-only — no WebSocket streaming.
Instead we run a background thread that polls /quote every 3 seconds
and keeps a single in-memory snapshot. The frontend polls /api/ticker
(cheap, DB-free) to read it.

Credit usage: 3-second cadence × 60 sec = 20 req/min. Combined with
our other Twelve Data jobs (~2 req/min for klines, cross-assets,
indicators) we sit at ~22/55 credits per minute, well within the
Grow plan limit.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import requests

from shared.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.twelvedata.com"
_SYMBOL = "WTI/USD"
_POLL_INTERVAL_SECONDS = 3.0

_CACHE: dict = {
    "symbol": _SYMBOL,
    "price": None,
    "change_pct": None,
    "high_24h": None,
    "low_24h": None,
    "open_24h": None,
    "is_market_open": None,
    "direction": "flat",  # up / down / flat vs previous tick
    "last_quote_at": None,
    "updated_at": None,
    "poll_count": 0,
    "error": None,
}
_CACHE_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


def get_cached_ticker() -> dict:
    """Return a snapshot of the current cached ticker state."""
    with _CACHE_LOCK:
        return dict(_CACHE)


def _poll_once() -> None:
    if not settings.twelve_api_key:
        return
    try:
        r = requests.get(
            f"{_BASE}/quote",
            params={"symbol": _SYMBOL, "apikey": settings.twelve_api_key},
            timeout=8,
        )
        r.raise_for_status()
        d = r.json()
    except Exception as exc:
        with _CACHE_LOCK:
            _CACHE["error"] = str(exc)[:200]
            _CACHE["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        logger.warning("live_ticker poll failed: %s", exc)
        return

    if isinstance(d, dict) and d.get("status") == "error":
        with _CACHE_LOCK:
            _CACHE["error"] = d.get("message", "unknown error")[:200]
        return

    try:
        new_price = float(d["close"])
        percent_change = float(d.get("percent_change") or 0)
        high = float(d["high"]) if d.get("high") else None
        low = float(d["low"]) if d.get("low") else None
        open_p = float(d.get("open")) if d.get("open") else None
        is_open = bool(d.get("is_market_open"))
        last_quote_at = d.get("last_quote_at")
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("malformed quote response: %s (%s)", d, exc)
        return

    with _CACHE_LOCK:
        prev_price = _CACHE.get("price")
        direction = "flat"
        if prev_price is not None:
            if new_price > prev_price:
                direction = "up"
            elif new_price < prev_price:
                direction = "down"
        _CACHE.update({
            "symbol": _SYMBOL,
            "price": new_price,
            "change_pct": round(percent_change, 3),
            "high_24h": high,
            "low_24h": low,
            "open_24h": open_p,
            "is_market_open": is_open,
            "direction": direction,
            "last_quote_at": last_quote_at,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            "poll_count": _CACHE.get("poll_count", 0) + 1,
            "error": None,
        })


def _run_forever() -> None:
    logger.info(
        "Live ticker worker started (symbol=%s, interval=%.1fs)",
        _SYMBOL, _POLL_INTERVAL_SECONDS,
    )
    backoff = _POLL_INTERVAL_SECONDS
    while True:
        try:
            _poll_once()
            backoff = _POLL_INTERVAL_SECONDS
        except Exception:
            logger.exception("live_ticker iteration crashed")
            backoff = min(backoff * 2, 30.0)
        time.sleep(backoff)


def start_live_ticker_worker() -> None:
    """Launch the background polling thread (idempotent)."""
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    _WORKER = threading.Thread(
        target=_run_forever,
        daemon=True,
        name="twelve-live-ticker",
    )
    _WORKER.start()

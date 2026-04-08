"""Binance USD-M Futures liquidation stream (`@forceOrder`).

Subscribes to wss://fstream.binance.com/ws/<symbol>@forceOrder and records
every force-liquidation event for the configured symbol. Each event has
order side, price, quantity, and execution details.

Liquidation clusters are high-value signals:
  - Cluster of BUY-side force orders (shorts getting liquidated) often
    marks short-term resistance blowoffs / local tops.
  - Cluster of SELL-side force orders (longs getting liquidated) often
    marks capitulation lows.

Stream message shape:
{
  "e": "forceOrder",
  "E": 1568014460893,
  "o": {
    "s": "CLUSDT",
    "S": "SELL",             // liquidation side (what was force-closed)
    "o": "LIMIT",
    "f": "IOC",
    "q": "0.014",            // original quantity
    "p": "9910",             // price
    "ap": "9910",            // average price
    "X": "FILLED",           // order status
    "l": "0.014",            // last filled qty
    "z": "0.014",            // executed (cumulative filled) qty
    "T": 1568014460893       // order trade time (ms)
  }
}
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.binance_metrics import BinanceLiquidation
from shared.redis_streams import publish

logger = logging.getLogger(__name__)

_WS_BASE = "wss://fstream.binance.com/ws"
_STREAM = "liquidations.binance"


def _symbol() -> str:
    return (settings.binance_symbol or "CLUSDT").upper()


def _stream_url() -> str:
    return f"{_WS_BASE}/{_symbol().lower()}@forceOrder"


def _store_liquidation(order: dict) -> dict | None:
    """Parse one forceOrder payload, persist to DB, return a summary dict."""
    try:
        ts = datetime.fromtimestamp(order["T"] / 1000, tz=timezone.utc)
        price = float(order["p"])
        orig_qty = float(order["q"])
        executed_qty = float(order.get("z", order.get("l", orig_qty)))
        avg_price = float(order.get("ap") or price)
        quote_qty = executed_qty * avg_price

        row = BinanceLiquidation(
            symbol=order["s"],
            timestamp=ts,
            side=order["S"],
            price=price,
            orig_qty=orig_qty,
            executed_qty=executed_qty,
            quote_qty_usd=quote_qty,
            avg_price=avg_price,
            order_status=order.get("X"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Malformed forceOrder payload: %s (%s)", order, exc)
        return None

    try:
        with SessionLocal() as session:
            session.add(row)
            session.commit()
    except Exception:
        logger.exception("Failed to persist liquidation")
        return None

    return {
        "symbol": row.symbol,
        "side": row.side,
        "price": row.price,
        "quote_qty_usd": row.quote_qty_usd,
        "timestamp": ts.isoformat(),
    }


def _run_forever() -> None:
    try:
        import websocket  # type: ignore
    except ImportError:
        logger.error("websocket-client not installed — liquidation stream disabled")
        return

    backoff = 1.0
    while True:
        url = _stream_url()
        logger.info("Liquidation WS connecting: %s", url)
        ws = None
        try:
            ws = websocket.WebSocket()
            ws.connect(url, timeout=15)
            backoff = 1.0
            logger.info("Liquidation WS connected to %s", _symbol())

            while True:
                try:
                    raw = ws.recv()
                except Exception:
                    logger.exception("Liquidation WS recv() failed, reconnecting")
                    break
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON WS frame, skipping")
                    continue
                if msg.get("e") != "forceOrder":
                    continue
                order = msg.get("o")
                if not isinstance(order, dict):
                    continue
                summary = _store_liquidation(order)
                if summary is None:
                    continue
                logger.info(
                    "Liquidation: %s %s %s @ %.2f ($%.0f)",
                    summary["symbol"], summary["side"],
                    order.get("q"), summary["price"], summary["quote_qty_usd"],
                )
                try:
                    publish(_STREAM, summary)
                except Exception:
                    logger.exception("Failed to publish liquidation to Redis")
        except Exception:
            logger.exception("Liquidation WS connect/loop crashed")
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

        logger.info("Liquidation WS reconnecting in %.1fs …", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


_WORKER: threading.Thread | None = None


def start_liquidations_ws() -> None:
    """Launch the reconnecting liquidation WS worker as a daemon thread."""
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    _WORKER = threading.Thread(
        target=_run_forever,
        daemon=True,
        name="binance-liquidations-ws",
    )
    _WORKER.start()
    logger.info("Started Binance liquidations WS worker")

"""Cross-asset context via Twelve Data (single paid feed).

Replaces the old Yahoo-based cross_assets.py. We track the same five
reference instruments we used to — DXY / SPX / Gold / BTC / VIX — but
route everything through Twelve Data for consistency and SLA.

Twelve Data Grow does NOT include raw index symbols (DXY, SPX, VIX),
so we use liquid ETF proxies that tightly track the underlying. The
correlation behaviour vs oil is indistinguishable from the real index
for our purposes (we care about direction and regime, not exact level).

Mapping:
  DXY  → UUP      (Invesco DB US Dollar Index Bullish Fund, NYSE)
  SPX  → SPY      (SPDR S&P 500 ETF, NYSE)
  GOLD → XAU/USD  (spot gold vs USD, forex)
  BTC  → BTC/USD  (Coinbase Pro, spot)
  VIX  → VIXY     (ProShares VIX Short-Term Futures ETF, CBOE)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests
from sqlalchemy.dialects.postgresql import insert as pg_insert

from shared.config import settings
from shared.models.base import SessionLocal
from shared.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)

_BASE = "https://api.twelvedata.com"

# Internal label -> Twelve Data symbol
# DXY is now computed from the forex basket (EUR/USD, USD/JPY, etc.)
# rather than an ETF proxy — see compute_real_dxy() below.
SYMBOLS: dict[str, str] = {
    "SPX":  "SPY",
    "GOLD": "XAU/USD",
    "BTC":  "BTC/USD",
    "ETH":  "ETH/USD",
    "SOL":  "SOL/USD",
    "VIX":  "VIXY",
    # Forex basket for real DXY reconstruction
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "GBPUSD": "GBP/USD",
    "USDCAD": "USD/CAD",
    "USDSEK": "USD/SEK",
    "USDCHF": "USD/CHF",
}

# Twelve Data interval -> internal timeframe suffix
_INTERVAL_MAP: dict[str, str] = {
    "1h":   "1h",
    "1day": "1d",
}


def _parse_ts(datetime_str: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(datetime_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable datetime: {datetime_str!r}")


def _fetch_one(label: str, twelve_symbol: str, interval: str, outputsize: int) -> list[dict]:
    if not settings.twelve_api_key:
        return []
    try:
        r = requests.get(
            f"{_BASE}/time_series",
            params={
                "symbol": twelve_symbol,
                "interval": interval,
                "outputsize": outputsize,
                "apikey": settings.twelve_api_key,
                "format": "JSON",
                "timezone": "UTC",
            },
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        logger.error("twelve cross-asset fetch failed (%s/%s): %s", label, interval, exc)
        return []

    if isinstance(payload, dict) and payload.get("status") == "error":
        logger.error(
            "twelve cross-asset API error (%s/%s): %s",
            label, interval, payload.get("message", payload),
        )
        return []

    values = payload.get("values") or []
    tf_label = _INTERVAL_MAP.get(interval, interval)
    records: list[dict] = []
    for row in values:
        try:
            records.append({
                "timestamp": _parse_ts(row["datetime"]),
                "source": "cross_asset",
                "timeframe": f"{label}:{tf_label}",
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]) if row.get("volume") else None,
            })
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("skipping malformed row for %s: %s (%s)", label, row, exc)
    return records


# Official ICE Dollar Index (DXY) weights — unchanged since 1999 rebase
# when EUR replaced the basket's European currencies.
# Formula: DXY = 50.14348112 × Π(pair^weight)
# where EUR/GBP are inverted (USD is the QUOTE, so pair^-weight).
_DXY_CONSTANT = 50.14348112
_DXY_WEIGHTS = {
    # label, weight, sign (+1 if USD is base, -1 if USD is quote)
    "EURUSD": (0.576, -1),  # EUR/USD → USD as quote, inverted
    "USDJPY": (0.136, +1),  # USD/JPY → USD as base
    "GBPUSD": (0.119, -1),  # GBP/USD → USD as quote, inverted
    "USDCAD": (0.091, +1),
    "USDSEK": (0.042, +1),
    "USDCHF": (0.036, +1),
}


def _compute_dxy_from_basket(basket_records: dict[str, list[dict]]) -> list[dict]:
    """Given the fetched forex basket records (label → list of bars),
    compute a synthetic DXY time series aligned on timestamps.

    Returns a list of OHLCV-shaped dicts with source='cross_asset',
    timeframe='DXY:1h', close = computed DXY value. Uses close prices
    only — OHL are set to close too for simplicity since we use DXY
    only for close-based correlations.
    """
    # Collect close values by timestamp for each pair
    by_ts: dict[datetime, dict[str, float]] = {}
    for label, recs in basket_records.items():
        if label not in _DXY_WEIGHTS:
            continue
        for r in recs:
            ts = r["timestamp"]
            by_ts.setdefault(ts, {})[label] = r["close"]

    dxy_records: list[dict] = []
    for ts, closes in sorted(by_ts.items()):
        # Need all 6 pairs at this timestamp for a valid DXY
        if not all(k in closes for k in _DXY_WEIGHTS):
            continue
        product = 1.0
        try:
            for label, (weight, sign) in _DXY_WEIGHTS.items():
                price = closes[label]
                if price <= 0:
                    raise ValueError(f"non-positive {label}={price}")
                # Apply exponent with sign (inverted for EUR, GBP)
                product *= price ** (sign * weight)
            dxy = _DXY_CONSTANT * product
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
        dxy_records.append({
            "timestamp": ts,
            "source": "cross_asset",
            "timeframe": "DXY:1h",
            "open": round(dxy, 4),
            "high": round(dxy, 4),
            "low": round(dxy, 4),
            "close": round(dxy, 4),
            "volume": None,
        })
    return dxy_records


def collect_and_store(interval: str = "1h", outputsize: int = 200) -> None:
    """Fetch all cross-asset symbols, compute real DXY from forex basket,
    and upsert everything under source='cross_asset'."""
    all_records: list[dict] = []
    basket_by_label: dict[str, list[dict]] = {}

    for label, twelve_sym in SYMBOLS.items():
        recs = _fetch_one(label, twelve_sym, interval, outputsize)
        if not recs:
            continue
        all_records.extend(recs)
        basket_by_label[label] = recs
        logger.info(
            "twelve cross-asset: %d bars for %s (%s, %s)",
            len(recs), label, twelve_sym, interval,
        )

    # Synthesize real DXY from the forex basket
    dxy_records = _compute_dxy_from_basket(basket_by_label)
    if dxy_records:
        all_records.extend(dxy_records)
        logger.info("Computed %d real-DXY bars from forex basket", len(dxy_records))

    if not all_records:
        return

    with SessionLocal() as session:
        stmt = pg_insert(OHLCV).values(all_records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "timeframe", "timestamp"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        session.execute(stmt)
        session.commit()
    logger.info("Upserted %d cross-asset rows (twelve, %s)", len(all_records), interval)

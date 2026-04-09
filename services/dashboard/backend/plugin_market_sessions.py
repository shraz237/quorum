"""Market session awareness via Twelve Data /market_state endpoint.

Fetches the open/closed status of ~140 global exchanges and classifies
the current trading regime for sizing decisions. Cached for 5 minutes
per process.

Session classification (for sizing multiplier):
  "us_open"         — NYMEX / NYSE / NASDAQ active → full 1.0× sizing
  "london_open"     — LSE / EUREX active → full 1.0× sizing
  "us_london_both"  — overlap session, best liquidity → 1.0× (could bump to 1.1×)
  "asia_only"       — HKEX / TSE / SSE active, no Western → 0.7× (thin vol)
  "closed"          — no major exchanges open (weekend / major holiday) → 0.5×
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

from shared.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://api.twelvedata.com"
_CACHE: dict | None = None
_CACHE_TS: float = 0.0
_CACHE_TTL_SECONDS = 300  # 5 minutes

# Exchange code prefixes we care about, grouped by region
_US_CODES = {"NYSE", "NASDAQ", "NYMEX", "CBOE", "AMEX"}
_LONDON_CODES = {"LSE", "EUREX", "EURONEXT", "XETR"}
_ASIA_CODES = {"HKEX", "TSE", "SSE", "SZSE", "KRX", "BSE", "NSE", "SGX"}


def _fetch_market_state() -> list[dict]:
    if not settings.twelve_api_key:
        return []
    try:
        r = requests.get(
            f"{_BASE}/market_state",
            params={"apikey": settings.twelve_api_key},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.error("market_state fetch failed: %s", exc)
        return []
    if isinstance(data, dict) and data.get("status") == "error":
        logger.error("market_state API error: %s", data.get("message", ""))
        return []
    return data if isinstance(data, list) else []


def get_market_state(force: bool = False) -> dict:
    """Return a structured market state snapshot with caching."""
    global _CACHE, _CACHE_TS
    now = time.time()
    if not force and _CACHE is not None and (now - _CACHE_TS) < _CACHE_TTL_SECONDS:
        age = round(now - _CACHE_TS, 1)
        return {**_CACHE, "cache_age_seconds": age}

    raw = _fetch_market_state()
    if not raw:
        return {
            "error": "no data",
            "exchanges": [],
            "active_sessions": [],
            "regime": "unknown",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    # Classify
    open_exchanges: list[dict] = []
    all_exchanges: list[dict] = []
    for e in raw:
        code = e.get("code") or e.get("name", "").upper()
        is_open = e.get("is_market_open", False)
        item = {
            "code": code,
            "name": e.get("name") or code,
            "country": e.get("country"),
            "is_open": is_open,
            "time_after_open": e.get("time_after_open"),
            "time_to_open": e.get("time_to_open"),
            "time_to_close": e.get("time_to_close"),
        }
        all_exchanges.append(item)
        if is_open:
            open_exchanges.append(item)

    active_codes = {e["code"] for e in open_exchanges}
    us_open = bool(active_codes & _US_CODES)
    london_open = bool(active_codes & _LONDON_CODES)
    asia_open = bool(active_codes & _ASIA_CODES)

    if us_open and london_open:
        regime = "us_london_overlap"
        sizing_multiplier = 1.1
    elif us_open:
        regime = "us_only"
        sizing_multiplier = 1.0
    elif london_open:
        regime = "london_only"
        sizing_multiplier = 0.95
    elif asia_open:
        regime = "asia_only"
        sizing_multiplier = 0.7
    elif not open_exchanges:
        regime = "all_closed"
        sizing_multiplier = 0.5
    else:
        regime = "misc_open"
        sizing_multiplier = 0.85

    result = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_exchanges": len(all_exchanges),
        "open_count": len(open_exchanges),
        "active_sessions": {
            "us": us_open,
            "london": london_open,
            "asia": asia_open,
        },
        "active_us_exchanges": sorted(list(active_codes & _US_CODES)),
        "active_london_exchanges": sorted(list(active_codes & _LONDON_CODES)),
        "active_asia_exchanges": sorted(list(active_codes & _ASIA_CODES)),
        "regime": regime,
        "sizing_multiplier": sizing_multiplier,
        "cache_age_seconds": 0.0,
    }
    _CACHE = result
    _CACHE_TS = now
    return result

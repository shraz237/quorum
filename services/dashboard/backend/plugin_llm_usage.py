"""LLM usage rollups — today/7d/30d breakdowns for the dashboard panel.

Reads the llm_usage table (populated by shared/llm_usage.py from every
LLM call site) and computes:
  - totals: calls, cost, tokens
  - breakdown by call_site (the most useful lens for "what am I spending on")
  - breakdown by model
  - breakdown by service
  - cache savings: how much we'd have paid without prompt caching
  - hourly sparkline for the last 24h
  - heartbeat skip ratio (from heartbeat_runs, as a related efficiency stat)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func

from shared.models.base import SessionLocal
from shared.models.heartbeat_runs import HeartbeatRun
from shared.models.llm_usage import LlmUsage

logger = logging.getLogger(__name__)


def _start_of_day_utc() -> datetime:
    now = datetime.now(tz=timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _rollup(session, since: datetime, until: datetime | None = None) -> dict:
    """Aggregate rows in [since, until) into totals + breakdowns."""
    q = session.query(LlmUsage).filter(LlmUsage.ts >= since)
    if until is not None:
        q = q.filter(LlmUsage.ts < until)

    rows = q.all()

    if not rows:
        return {
            "total_calls": 0,
            "success_calls": 0,
            "failed_calls": 0,
            "total_cost_usd": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_creation_tokens": 0,
            "cache_savings_usd": 0.0,
            "by_call_site": [],
            "by_model": [],
            "by_service": [],
        }

    total_calls = len(rows)
    success_calls = sum(1 for r in rows if r.success)
    failed_calls = total_calls - success_calls

    total_cost = sum(r.estimated_cost_usd or 0.0 for r in rows)
    total_input = sum(r.input_tokens or 0 for r in rows)
    total_output = sum(r.output_tokens or 0 for r in rows)
    total_cache_read = sum(r.cache_read_tokens or 0 for r in rows)
    total_cache_creation = sum(r.cache_creation_tokens or 0 for r in rows)

    # Cache savings: cache_read tokens are billed at 10% of normal input rate.
    # So each cached token saved 90% of its would-be input cost. We don't
    # know the exact input rate per row here, but we can approximate by
    # looking at the recorded cost vs. what we WOULD have paid if all
    # cache_read tokens had been billed as normal input.
    # Lower-bound estimate: assume Sonnet rates ($3/MTok input) for the
    # cached tokens, giving 0.9 * 3 / 1_000_000 per token saved.
    # This is intentionally conservative.
    cache_savings = (total_cache_read * 0.9 * 3.0) / 1_000_000.0

    # By call_site
    site_agg: dict[str, dict] = {}
    for r in rows:
        key = r.call_site
        bucket = site_agg.setdefault(key, {
            "call_site": key,
            "calls": 0,
            "cost": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "failed": 0,
        })
        bucket["calls"] += 1
        bucket["cost"] += r.estimated_cost_usd or 0.0
        bucket["input_tokens"] += r.input_tokens or 0
        bucket["output_tokens"] += r.output_tokens or 0
        bucket["cache_read_tokens"] += r.cache_read_tokens or 0
        if not r.success:
            bucket["failed"] += 1
    by_site = sorted(
        [{**v, "cost": round(v["cost"], 4)} for v in site_agg.values()],
        key=lambda x: x["cost"],
        reverse=True,
    )

    # By model
    model_agg: dict[str, dict] = {}
    for r in rows:
        key = r.model
        bucket = model_agg.setdefault(key, {"model": key, "calls": 0, "cost": 0.0})
        bucket["calls"] += 1
        bucket["cost"] += r.estimated_cost_usd or 0.0
    by_model = sorted(
        [{**v, "cost": round(v["cost"], 4)} for v in model_agg.values()],
        key=lambda x: x["cost"],
        reverse=True,
    )

    # By service
    svc_agg: dict[str, dict] = {}
    for r in rows:
        key = r.service
        bucket = svc_agg.setdefault(key, {"service": key, "calls": 0, "cost": 0.0})
        bucket["calls"] += 1
        bucket["cost"] += r.estimated_cost_usd or 0.0
    by_service = sorted(
        [{**v, "cost": round(v["cost"], 4)} for v in svc_agg.values()],
        key=lambda x: x["cost"],
        reverse=True,
    )

    return {
        "total_calls": total_calls,
        "success_calls": success_calls,
        "failed_calls": failed_calls,
        "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_creation_tokens": total_cache_creation,
        "cache_savings_usd": round(cache_savings, 4),
        "by_call_site": by_site,
        "by_model": by_model,
        "by_service": by_service,
    }


def _hourly_sparkline(session, hours: int = 24) -> list[dict]:
    """Return per-hour cost totals for the last N hours, oldest first.

    Client-side bucketing — dataset is tiny (a few thousand rows/day max)
    so this is faster than a parameterized GROUP BY in postgres which
    has strict rules about expressions in GROUP BY clauses.
    """
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    rows = (
        session.query(LlmUsage.ts, LlmUsage.estimated_cost_usd)
        .filter(LlmUsage.ts >= since)
        .all()
    )

    buckets: dict[str, dict] = {}
    for ts, cost in rows:
        if ts is None:
            continue
        bucket_hour = ts.replace(minute=0, second=0, microsecond=0)
        key = bucket_hour.isoformat()
        bucket = buckets.setdefault(key, {"hour": key, "cost": 0.0, "calls": 0})
        bucket["cost"] += float(cost or 0.0)
        bucket["calls"] += 1

    # Sort oldest first so the sparkline reads left-to-right
    return [
        {"hour": b["hour"], "cost": round(b["cost"], 4), "calls": b["calls"]}
        for b in sorted(buckets.values(), key=lambda x: x["hour"])
    ]


def _heartbeat_skip_ratio(session, since: datetime) -> dict:
    """How many heartbeat ticks skipped the Opus call via the hash gate?"""
    rows = (
        session.query(HeartbeatRun.decision, func.count(HeartbeatRun.id))
        .filter(
            and_(
                HeartbeatRun.ran_at >= since,
                HeartbeatRun.campaign_id.is_(None),  # tick-summary rows only
            )
        )
        .group_by(HeartbeatRun.decision)
        .all()
    )
    counts = {d: int(c) for d, c in rows}
    skipped = counts.get("skipped_unchanged", 0)
    ran = counts.get("skipped", 0)  # "skipped" is the summary-row decision for a normal run
    total = skipped + ran
    ratio = (skipped / total) if total > 0 else 0.0
    return {
        "skipped_unchanged": skipped,
        "opus_called": ran,
        "total": total,
        "skip_ratio": round(ratio, 3),
    }


def get_llm_usage_rollup() -> dict:
    """Top-level endpoint payload — today + 7d + 30d + hourly + heartbeat."""
    with SessionLocal() as session:
        today_start = _start_of_day_utc()
        yesterday_start = today_start - timedelta(days=1)
        week_start = datetime.now(tz=timezone.utc) - timedelta(days=7)
        month_start = datetime.now(tz=timezone.utc) - timedelta(days=30)

        return {
            "today": _rollup(session, since=today_start),
            "yesterday": _rollup(session, since=yesterday_start, until=today_start),
            "last_7d": _rollup(session, since=week_start),
            "last_30d": _rollup(session, since=month_start),
            "hourly_24h": _hourly_sparkline(session, hours=24),
            "heartbeat_24h": _heartbeat_skip_ratio(
                session, since=datetime.now(tz=timezone.utc) - timedelta(hours=24)
            ),
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        }

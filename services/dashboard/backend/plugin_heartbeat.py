"""Dashboard-side heartbeat control + status.

The actual heartbeat worker runs in services/ai-brain/heartbeat.py. This
plugin only READS the Redis kill-switch flag + timestamps, flips them on
pause/resume, and reads the heartbeat_runs audit table for the UI panel.

Redis keys owned by ai-brain/heartbeat.py:
  heartbeat:enabled      "true" | "false"
  heartbeat:last_run_at  ISO timestamp of the most recent tick start
  heartbeat:next_run_at  ISO timestamp of the next scheduled tick
  heartbeat:running      lock (presence means a tick is in-flight)
"""

from __future__ import annotations

import logging

from shared.models.base import SessionLocal
from shared.models.heartbeat_runs import HeartbeatRun
from shared.redis_streams import get_redis

logger = logging.getLogger(__name__)

REDIS_KEY_ENABLED = "heartbeat:enabled"
REDIS_KEY_LAST_RUN = "heartbeat:last_run_at"
REDIS_KEY_NEXT_RUN = "heartbeat:next_run_at"
REDIS_KEY_LOCK = "heartbeat:running"


def _get_redis_string(key: str) -> str | None:
    """Read a Redis string key, coping with both decode_responses=True and False.

    Different services configure their Redis client differently — dashboard
    uses decode_responses=True (returns str) while ai-brain returns bytes.
    """
    try:
        val = get_redis().get(key)
        if val is None:
            return None
        if isinstance(val, bytes):
            return val.decode("utf-8")
        return str(val)
    except Exception:
        logger.exception("Failed to read %s", key)
        return None


def get_status() -> dict:
    """Return full heartbeat status for the dashboard."""
    enabled_raw = _get_redis_string(REDIS_KEY_ENABLED)
    # Default to True if the key hasn't been written yet (ai-brain sets it on
    # first tick). UI should display as "live" in that case.
    enabled = enabled_raw != "false"

    last_run = _get_redis_string(REDIS_KEY_LAST_RUN)
    next_run = _get_redis_string(REDIS_KEY_NEXT_RUN)
    running = _get_redis_string(REDIS_KEY_LOCK) is not None

    recent = list_recent_decisions(limit=10)

    return {
        "enabled": enabled,
        "last_run_at": last_run,
        "next_run_at": next_run,
        "running_now": running,
        "recent_decisions": recent,
    }


def set_enabled(enabled: bool) -> dict:
    """Flip the kill-switch. Returns the new status."""
    try:
        get_redis().set(REDIS_KEY_ENABLED, "true" if enabled else "false")
    except Exception:
        logger.exception("Failed to set heartbeat:enabled")
        return {"error": "redis write failed"}
    return get_status()


def list_recent_decisions(limit: int = 10) -> list[dict]:
    """Return the last `limit` rows from heartbeat_runs, newest first.

    Filters out tick-summary rows (campaign_id IS NULL) so the UI only shows
    actual per-campaign decisions.
    """
    with SessionLocal() as session:
        rows = (
            session.query(HeartbeatRun)
            .filter(HeartbeatRun.campaign_id.isnot(None))
            .order_by(HeartbeatRun.ran_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "ran_at": r.ran_at.isoformat() if r.ran_at else None,
                "campaign_id": r.campaign_id,
                "decision": r.decision,
                "reason": r.reason,
                "executed": r.executed,
            }
            for r in rows
        ]

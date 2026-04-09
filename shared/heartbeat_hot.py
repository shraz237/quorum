"""Heartbeat hot window helper — arm aggressive monitoring from any service.

When a campaign opens, closes, or hits a meaningful event, callers can
flip the heartbeat loop into "hot" mode where it ticks every 30s instead
of every 5 min. The state lives in a Redis key so any service can set it
without cross-importing heartbeat.py.

Redis contract (owned by services/ai-brain/heartbeat.py):
  heartbeat:hot_until — unix float timestamp until which hot mode is active
"""

from __future__ import annotations

import logging
import time

from shared.redis_streams import get_redis

logger = logging.getLogger(__name__)

REDIS_KEY_HOT_UNTIL = "heartbeat:hot_until"
DEFAULT_HOT_WINDOW_SECONDS = 5 * 60  # 5 minutes


def arm_hot_window(duration_seconds: int = DEFAULT_HOT_WINDOW_SECONDS, reason: str = "") -> None:
    """Set the heartbeat into aggressive 30s-tick mode for `duration_seconds`.

    Silent on failure — this is a nice-to-have monitoring hint, never a
    hard dependency of the caller. Call this whenever a campaign opens
    or closes so Opus aggressively monitors the transition.
    """
    try:
        r = get_redis()
        until = time.time() + max(1, int(duration_seconds))
        r.set(REDIS_KEY_HOT_UNTIL, str(until))
        logger.info(
            "Heartbeat hot window armed for %ds%s",
            duration_seconds,
            f" ({reason})" if reason else "",
        )
    except Exception:
        logger.exception("arm_hot_window failed")

"""Plugin: live watch session tools (start / stop / query).

Exposes PLUGIN_TOOLS (list of Anthropic tool schemas) and execute(name, input).
The orchestrator merges these into the main TOOLS list at startup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anthropic tool schemas
# ---------------------------------------------------------------------------

PLUGIN_TOOLS = [
    {
        "name": "start_live_watch",
        "description": (
            "Start a live monitoring session for the next N minutes. The bot will post a "
            "Telegram message that updates itself in place every cycle_seconds with current "
            "price, score deltas, recent news, and a running verdict. Use when the user says "
            "'watch for X minutes', 'monitor live', 'help me decide on an entry'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "duration_minutes": {
                    "type": "integer",
                    "default": 10,
                    "description": "1-60",
                },
                "focus": {
                    "type": "string",
                    "enum": ["LONG", "SHORT", "EITHER"],
                    "default": "EITHER",
                },
                "cycle_seconds": {
                    "type": "integer",
                    "default": 30,
                    "description": "15-300",
                },
                "question": {
                    "type": "string",
                    "description": "What the user is watching for, e.g. 'should I enter long?'",
                },
            },
        },
    },
    {
        "name": "stop_live_watch",
        "description": "Stop an active watch session early.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "get_active_watch",
        "description": "Return the currently active watch session (if any) with its state.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def execute(name: str, input: dict) -> dict | None:
    """Return result dict, or None if this plugin does not handle *name*."""
    if name == "start_live_watch":
        return _start_live_watch(**input)
    if name == "stop_live_watch":
        return _stop_live_watch(**input)
    if name == "get_active_watch":
        return _get_active_watch()
    return None


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

def _start_live_watch(
    duration_minutes: int = 10,
    focus: str = "EITHER",
    cycle_seconds: int = 30,
    question: str | None = None,
) -> dict:
    # Validate
    duration_minutes = max(1, min(60, int(duration_minutes)))
    cycle_seconds = max(15, min(300, int(cycle_seconds)))
    focus = focus.upper() if focus else "EITHER"
    if focus not in ("LONG", "SHORT", "EITHER"):
        focus = "EITHER"

    from shared.models.base import SessionLocal
    from shared.models.watch_sessions import WatchSession
    from sqlalchemy import desc

    now = datetime.now(tz=timezone.utc)

    with SessionLocal() as session:
        # First force-expire any stale sessions the worker missed
        stale = (
            session.query(WatchSession)
            .filter(
                WatchSession.status == "active",
                WatchSession.expires_at <= now,
            )
            .all()
        )
        for s in stale:
            s.status = "expired"
            s.ended_at = now
        if stale:
            session.commit()

        # Reject duplicate — one active session at a time
        existing = (
            session.query(WatchSession)
            .filter(
                WatchSession.status == "active",
                WatchSession.expires_at > now,
            )
            .order_by(desc(WatchSession.created_at))
            .first()
        )
        if existing is not None:
            remaining = max(0, int((existing.expires_at - now).total_seconds()))
            return {
                "error": "already_active",
                "session_id": existing.id,
                "focus": existing.focus,
                "remaining_seconds": remaining,
                "expires_at_iso": existing.expires_at.isoformat(),
                "message": (
                    f"Watch #{existing.id} already active ({existing.focus}, "
                    f"{remaining}s remaining). Stop it first with stop_live_watch, "
                    f"or wait for it to expire."
                ),
            }

        expires_at = now + timedelta(minutes=duration_minutes)
        watch = WatchSession(
            created_at=now,
            expires_at=expires_at,
            status="active",
            focus=focus,
            cycle_seconds=cycle_seconds,
            question=question,
        )
        session.add(watch)
        session.commit()
        session.refresh(watch)
        session_id = watch.id

    logger.info(
        "Started live watch session #%s: focus=%s duration=%dm cycle=%ds",
        session_id, focus, duration_minutes, cycle_seconds,
    )
    return {
        "session_id": session_id,
        "status": "active",
        "focus": focus,
        "duration_minutes": duration_minutes,
        "cycle_seconds": cycle_seconds,
        "expires_at_iso": expires_at.isoformat(),
        "question": question,
        "message": (
            f"Live watch session #{session_id} started. "
            f"Watching {focus} for {duration_minutes} minute(s), "
            f"updating every {cycle_seconds}s via Telegram."
        ),
    }


def _stop_live_watch(session_id: int) -> dict:
    from shared.models.base import SessionLocal
    from shared.models.watch_sessions import WatchSession

    now = datetime.now(tz=timezone.utc)
    with SessionLocal() as session:
        row = session.get(WatchSession, session_id)
        if row is None:
            return {
                "stopped": False,
                "session_id": session_id,
                "error": "session not found",
            }
        if row.status != "active":
            return {
                "stopped": False,
                "session_id": session_id,
                "error": f"session is already {row.status}",
            }
        row.status = "stopped"
        row.ended_at = now
        session.commit()

    logger.info("Stopped live watch session #%s", session_id)
    return {
        "stopped": True,
        "session_id": session_id,
        "ended_at_iso": now.isoformat(),
    }


def _get_active_watch() -> dict:
    from shared.models.base import SessionLocal
    from shared.models.watch_sessions import WatchSession
    from sqlalchemy import desc

    now = datetime.now(tz=timezone.utc)

    with SessionLocal() as session:
        # Belt-and-suspenders: force-expire any stale rows the worker missed
        stale = (
            session.query(WatchSession)
            .filter(
                WatchSession.status == "active",
                WatchSession.expires_at <= now,
            )
            .all()
        )
        for s in stale:
            s.status = "expired"
            s.ended_at = now
        if stale:
            session.commit()

        row = (
            session.query(WatchSession)
            .filter(
                WatchSession.status == "active",
                WatchSession.expires_at > now,
            )
            .order_by(desc(WatchSession.created_at))
            .first()
        )
        if row is None:
            return {"active": False, "session": None}

        remaining_seconds = max(0, int((row.expires_at - now).total_seconds()))

        return {
            "active": True,
            "session": {
                "session_id": row.id,
                "status": row.status,
                "focus": row.focus,
                "cycle_seconds": row.cycle_seconds,
                "question": row.question,
                "created_at_iso": row.created_at.isoformat(),
                "expires_at_iso": row.expires_at.isoformat(),
                "remaining_seconds": remaining_seconds,
                "tick_count": row.tick_count,
                "last_tick_at_iso": row.last_tick_at.isoformat() if row.last_tick_at else None,
                "last_price": row.last_price,
                "last_unified_score": row.last_unified_score,
                "last_verdict": row.last_verdict,
            },
        }

"""Dashboard-side theses read/write adapter.

Wraps the shared.theses helpers into a shape suited for the
/api/theses REST surface. Keeps the dashboard decoupled from
ai-brain's background workers — those are the only things that
actually flip status from pending → triggered → resolved. This
module only reads the table and adds rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from shared.theses import (
    cancel_thesis,
    create_thesis,
    domain_stats,
    list_theses,
)

logger = logging.getLogger(__name__)


def get_theses_payload(domain: str | None = None) -> dict:
    """Return the full payload for the dashboard Theses tab.

    Splits rows into pending / triggered / resolved sections. Computes
    per-domain stats so the tab can show hit rate and hypothetical P/L.
    """
    # Load all recent rows in one pass — we're limited by N=200 so
    # this is cheap, and saves the per-section queries
    all_rows = list_theses(domain=domain, limit=200)

    pending: list[dict] = []
    triggered: list[dict] = []
    resolved: list[dict] = []
    other: list[dict] = []
    for r in all_rows:
        status = r.get("status")
        if status == "pending":
            pending.append(r)
        elif status == "triggered":
            triggered.append(r)
        elif status == "resolved":
            resolved.append(r)
        else:
            other.append(r)

    # Stats always computed per domain — if caller asked for a specific
    # domain we return just that one; otherwise return both.
    if domain in ("campaign", "scalp"):
        stats = {domain: domain_stats(domain, days=30)}
    else:
        stats = {
            "campaign": domain_stats("campaign", days=30),
            "scalp": domain_stats("scalp", days=30),
        }

    return {
        "domain_filter": domain,
        "pending": pending,
        "triggered": triggered,
        "resolved": resolved[:30],  # cap resolved to 30 most recent
        "other": other[:20],
        "stats": stats,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def create_from_form(payload: dict) -> dict:
    """POST /api/theses handler — validates + inserts a new thesis.

    Accepts the same shape as the chat tool but drives from a form,
    so created_by becomes user_form.
    """
    required = ("title", "thesis_text", "trigger_type", "trigger_params", "planned_action")
    for field in required:
        if field not in payload:
            return {"error": f"missing required field: {field}"}

    expires_at = None
    expires_in_hours = payload.get("expires_in_hours")
    if expires_in_hours is not None:
        try:
            expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=float(expires_in_hours))
        except (TypeError, ValueError):
            pass

    new_id = create_thesis(
        created_by="user_form",
        domain=payload.get("domain", "campaign"),
        title=payload["title"],
        thesis_text=payload["thesis_text"],
        reasoning=payload.get("reasoning"),
        trigger_type=payload["trigger_type"],
        trigger_params=payload["trigger_params"],
        planned_action=payload["planned_action"],
        planned_entry=payload.get("planned_entry"),
        planned_stop_loss=payload.get("planned_stop_loss"),
        planned_take_profit=payload.get("planned_take_profit"),
        planned_size_margin=payload.get("planned_size_margin"),
        expires_at=expires_at,
    )
    if new_id is None:
        return {"error": "failed to create thesis (validation or DB)"}

    # Also publish a thesis_created event so Telegram gets a confirmation
    try:
        from shared.redis_streams import publish
        publish(
            "position.event",
            {
                "type": "thesis_created",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "thesis_id": new_id,
                "domain": payload.get("domain", "campaign"),
                "title": payload["title"],
                "thesis_text": payload["thesis_text"],
                "trigger_type": payload["trigger_type"],
                "trigger_params": payload["trigger_params"],
                "planned_action": payload["planned_action"],
                "planned_entry": payload.get("planned_entry"),
                "planned_stop_loss": payload.get("planned_stop_loss"),
                "planned_take_profit": payload.get("planned_take_profit"),
                "created_by": "user_form",
            },
        )
    except Exception:
        logger.exception("Failed to publish thesis_created")

    return {"thesis_id": new_id, "status": "pending"}


def cancel(thesis_id: int, reason: str = "user_cancelled_dashboard") -> dict:
    ok = cancel_thesis(thesis_id, reason=reason)
    return {"cancelled": ok, "thesis_id": thesis_id}

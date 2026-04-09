"""Theses background workers — trigger watcher + outcome resolver.

Two daemon threads run in the ai-brain service:

  theses_watcher_loop()  — polls pending theses every 30 s, evaluates
                           triggers, fires thesis_triggered Telegram
                           events and flips rows to triggered state.

  theses_resolver_loop() — polls triggered theses every 5 min, checks
                           if their outcome can be resolved (TP/SL hit
                           or resolution window elapsed), fires
                           thesis_resolved Telegram events and flips
                           rows to resolved state.

Both are silent-on-failure — any crash is logged and the loop continues.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from shared.models.base import SessionLocal
from shared.models.theses import Thesis
from shared.redis_streams import publish
from shared.theses import (
    evaluate_resolution,
    evaluate_trigger,
    mark_expired,
    mark_resolved,
    mark_triggered,
)

logger = logging.getLogger(__name__)

WATCHER_INTERVAL_SECONDS = 30
RESOLVER_INTERVAL_SECONDS = 5 * 60

STREAM_POSITION = "position.event"


def _is_test_thesis(thesis: Thesis) -> bool:
    """Return True if this thesis came from a smoke-test source.

    Convention: any created_by starting with 'smoke' (e.g. 'smoke_test',
    'smoke_watcher_test') is treated as a test thesis. Every published
    event for this thesis will carry is_test=True so Telegram renders
    a loud 🧪 TEST marker in the title — user cannot confuse a test
    with a real thesis lifecycle event.
    """
    if thesis is None or not thesis.created_by:
        return False
    return thesis.created_by.lower().startswith("smoke")


def _publish_trigger(thesis: Thesis, snapshot: dict) -> None:
    try:
        payload = {
            "type": "thesis_triggered",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thesis_id": thesis.id,
            "domain": thesis.domain,
            "title": thesis.title,
            "thesis_text": thesis.thesis_text,
            "trigger_type": thesis.trigger_type,
            "trigger_snapshot": snapshot,
            "planned_action": thesis.planned_action,
            "planned_entry": thesis.planned_entry,
            "planned_stop_loss": thesis.planned_stop_loss,
            "planned_take_profit": thesis.planned_take_profit,
            "planned_size_margin": thesis.planned_size_margin,
            "created_by": thesis.created_by,
        }
        if _is_test_thesis(thesis):
            payload["is_test"] = True
        publish(STREAM_POSITION, payload)
        logger.info("Published thesis_triggered for #%s", thesis.id)
    except Exception:
        logger.exception("Failed to publish thesis_triggered for #%s", thesis.id)


def _publish_resolved(thesis: Thesis, outcome_payload: dict) -> None:
    try:
        payload = {
            "type": "thesis_resolved",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thesis_id": thesis.id,
            "domain": thesis.domain,
            "title": thesis.title,
            "planned_action": thesis.planned_action,
            **outcome_payload,
        }
        if _is_test_thesis(thesis):
            payload["is_test"] = True
        publish(STREAM_POSITION, payload)
        logger.info("Published thesis_resolved for #%s: %s", thesis.id, outcome_payload.get("outcome"))
    except Exception:
        logger.exception("Failed to publish thesis_resolved for #%s", thesis.id)


def _scan_pending() -> None:
    """One pass of the trigger watcher."""
    try:
        with SessionLocal() as session:
            pending = (
                session.query(Thesis)
                .filter(Thesis.status == "pending")
                .order_by(Thesis.created_at.asc())
                .all()
            )
            # Detach rows by accessing the fields we need while the
            # session is open; mark_triggered opens its own session later.
            rows = [
                (
                    r.id,
                    r.status,
                    r.expires_at,
                    r.trigger_type,
                    dict(r.trigger_params or {}),
                    r.created_at,
                )
                for r in pending
            ]
    except Exception:
        logger.exception("theses watcher: pending scan failed")
        return

    if not rows:
        return

    logger.info("theses watcher: %d pending theses", len(rows))

    # We need the full Thesis row for evaluate_trigger; re-fetch per row.
    for (tid, _status, _exp, _tt, _params, _created) in rows:
        try:
            with SessionLocal() as session:
                thesis = session.query(Thesis).filter(Thesis.id == tid).first()
                if thesis is None or thesis.status != "pending":
                    continue

                # Check expiry up-front
                if thesis.expires_at is not None and datetime.now(tz=timezone.utc) >= thesis.expires_at:
                    mark_expired(tid)
                    logger.info("theses watcher: #%s expired", tid)
                    continue

                fired, snapshot = evaluate_trigger(thesis)
        except Exception:
            logger.exception("theses watcher: eval_trigger failed for #%s", tid)
            continue

        if fired:
            if mark_triggered(tid, snapshot):
                # Re-load with the triggered state so the published payload
                # has triggered_at / triggered_price
                try:
                    with SessionLocal() as session:
                        fresh = session.query(Thesis).filter(Thesis.id == tid).first()
                        if fresh is not None:
                            _publish_trigger(fresh, snapshot)
                except Exception:
                    logger.exception("theses watcher: publish load failed for #%s", tid)


def _scan_triggered() -> None:
    """One pass of the outcome resolver."""
    try:
        with SessionLocal() as session:
            rows = (
                session.query(Thesis)
                .filter(Thesis.status == "triggered")
                .order_by(Thesis.triggered_at.asc())
                .all()
            )
            ids = [r.id for r in rows]
    except Exception:
        logger.exception("theses resolver: triggered scan failed")
        return

    if not ids:
        return

    logger.info("theses resolver: %d triggered theses to check", len(ids))

    for tid in ids:
        try:
            with SessionLocal() as session:
                thesis = session.query(Thesis).filter(Thesis.id == tid).first()
                if thesis is None or thesis.status != "triggered":
                    continue
                result = evaluate_resolution(thesis)
        except Exception:
            logger.exception("theses resolver: eval_resolution failed for #%s", tid)
            continue

        if result is None:
            continue

        ready, payload = result
        if not ready:
            continue

        if mark_resolved(tid, payload):
            try:
                with SessionLocal() as session:
                    fresh = session.query(Thesis).filter(Thesis.id == tid).first()
                    if fresh is not None:
                        _publish_resolved(fresh, payload)
            except Exception:
                logger.exception("theses resolver: publish load failed for #%s", tid)


def run_watcher_loop() -> None:
    """Background loop — scan pending theses every WATCHER_INTERVAL_SECONDS."""
    logger.info("Theses watcher starting (interval=%ds)", WATCHER_INTERVAL_SECONDS)
    time.sleep(20)  # let other workers boot first
    while True:
        try:
            _scan_pending()
        except Exception:
            logger.exception("theses watcher: scan crashed")
        time.sleep(WATCHER_INTERVAL_SECONDS)


def run_resolver_loop() -> None:
    """Background loop — resolve triggered theses every RESOLVER_INTERVAL_SECONDS."""
    logger.info("Theses resolver starting (interval=%ds)", RESOLVER_INTERVAL_SECONDS)
    time.sleep(45)  # offset from watcher so they don't contend
    while True:
        try:
            _scan_triggered()
        except Exception:
            logger.exception("theses resolver: scan crashed")
        time.sleep(RESOLVER_INTERVAL_SECONDS)

"""WTI market hours — is the market open right now?

WTI crude oil futures (NYMEX CL) trade nearly 24 hours on weekdays:
  Sunday  17:00 CT  (23:00 UTC) → Friday 17:00 CT (22:00 UTC*)

  * CT = US Central Time. During CDT (summer, UTC-5): open Sun 22:00 UTC.
    During CST (winter, UTC-6): open Sun 23:00 UTC.
    We use a conservative window: treat as CLOSED from Friday 22:00 UTC
    through Sunday 22:00 UTC. This gives a ~48h weekend window where the
    bot saves tokens by not running heartbeat, scalper, or news polling.

Also closed on some US holidays (simplified: Christmas Day, New Year's Day,
Independence Day). Not worth being precise — the staleness check in
get_current_price() catches any holiday we miss because the data-collector
won't insert new bars.

This module is deliberately simple — a single function that returns True/False.
Every worker that should go quiet during off-hours calls is_market_open()
at the top of its loop iteration and sleeps if False.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Friday close at 22:00 UTC, Sunday open at 22:00 UTC (conservative)
# Mon-Thu: always open (24h sessions with a brief 17:00-17:01 CT daily halt
# that we don't bother modeling — the 5-min staleness check covers it)
# Close early at 21:00 UTC on Friday (one hour before CME halt) because:
# 1. Twelve Data feed often stops updating before the official close
# 2. Last-hour liquidity is thin, spreads widen, signals degrade
# 3. No point running Opus on dying volume
WEEKEND_CLOSE_HOUR_UTC = 20  # Friday 20:00 UTC (10pm Poland, 3pm CT)
WEEKEND_OPEN_HOUR_UTC = 22   # Sunday 22:00 UTC (midnight Poland)


def is_market_open() -> bool:
    """Return True if WTI futures are likely trading right now.

    False during the weekend window (Friday 22:00 UTC → Sunday 22:00 UTC).
    Conservative — we'd rather miss the first few minutes of Sunday's
    open than waste tokens trading stale weekend data.
    """
    now = datetime.now(tz=timezone.utc)
    weekday = now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    hour = now.hour

    # Saturday: always closed
    if weekday == 5:
        return False

    # Friday after 22:00 UTC: closed
    if weekday == 4 and hour >= WEEKEND_CLOSE_HOUR_UTC:
        return False

    # Sunday before 22:00 UTC: closed
    if weekday == 6 and hour < WEEKEND_OPEN_HOUR_UTC:
        return False

    # Everything else: open (Mon-Thu 24h, Fri until 22:00, Sun from 22:00)
    return True


def market_status() -> dict:
    """Return a descriptive status for dashboards and logs."""
    now = datetime.now(tz=timezone.utc)
    open_now = is_market_open()
    return {
        "open": open_now,
        "utc_time": now.isoformat(),
        "weekday": now.strftime("%A"),
        "note": "trading" if open_now else "closed (weekend)",
    }

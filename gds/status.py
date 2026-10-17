"""Sync health for consumers (FR-12, `get_sync_status`)."""

from typing import Any

from gds.config import settings
from gds.db import connect


def sync_status() -> dict[str, Any]:
    with connect() as conn:
        health = conn.execute(
            "select * from garmin.v_sync_health where label = %s", (settings().account_label,)
        ).fetchone()
        if not health:
            return {"status": "no account configured"}
        runs = conn.execute(
            """select id, trigger, started_at, finished_at, status, units_done, units_empty, units_failed,
                      requests, error
               from garmin.sync_runs where account_id = %s order by started_at desc limit 5""",
            (health["account_id"],),
        ).fetchall()
        coverage = conn.execute(
            """select min(date) as first_day, max(date) as last_day, count(*) as days
               from garmin.v_daily_wellness where account_id = %s""",
            (health["account_id"],),
        ).fetchone()
        failing = conn.execute(
            """select domain, scope_key, attempts, last_error from garmin.work_units
               where account_id = %s and status = 'failed' order by scope_key desc limit 10""",
            (health["account_id"],),
        ).fetchall()
    health.pop("account_id", None)
    return {"health": health, "coverage": coverage, "recent_runs": runs, "failed_units": failing}

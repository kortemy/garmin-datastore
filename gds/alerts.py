"""Failure alerts (FR-13): email via Resend, deduped to one per condition per 24 h.

Without RESEND_API_KEY the alert is still recorded in garmin.alerts (delivered = false).
"""

import json
import logging
import urllib.request
import uuid

import psycopg

from gds.config import settings

log = logging.getLogger(__name__)


def _send_email(subject: str, text: str) -> bool:
    s = settings()
    if not (s.resend_api_key and s.alert_email):
        return False
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({"from": s.alert_from, "to": [s.alert_email], "subject": subject, "text": text}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {s.resend_api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001 - alerting must never break the tick
        log.warning("alert email failed: %s", e)
        return False


def raise_alert(conn: psycopg.Connection, acct: uuid.UUID | None, condition: str, message: str) -> None:
    recent = conn.execute(
        """select 1 from garmin.alerts
           where account_id is not distinct from %s and condition = %s and created_at > now() - interval '24 hours'""",
        (acct, condition),
    ).fetchone()
    if recent:
        return
    delivered = _send_email(f"[garmin-datastore] {condition}", message)
    conn.execute(
        "insert into garmin.alerts (account_id, condition, message, delivered) values (%s, %s, %s, %s)",
        (acct, condition, message, delivered),
    )

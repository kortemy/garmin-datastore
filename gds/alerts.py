"""Failure alerts (FR-13): email via Resend, deduped to one per condition per 24 h.

Without RESEND_API_KEY the alert is still recorded in garmin.alerts (delivered = false).
"""

import json
import logging
import time
import urllib.error
import urllib.request
import uuid

import psycopg

from gds.config import settings

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


def send_email(subject: str, text: str, idempotency_key: str | None = None) -> str | None:
    """Send one plain-text alert. Returns Resend's email id, or None if not configured or failed.

    Retries once on 429/5xx (honouring Retry-After), never on other 4xx.
    """
    s = settings()
    if not (s.resend_api_key and s.alert_email):
        return None
    body = json.dumps({"from": s.alert_from, "to": [s.alert_email], "subject": subject, "text": text}).encode()
    headers = {
        "Authorization": f"Bearer {s.resend_api_key}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key or uuid.uuid4().hex,
    }
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(RESEND_URL, data=body, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                email_id = json.loads(resp.read() or b"{}").get("id")
                log.info("alert email sent: %s", email_id)
                return email_id
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or e.code >= 500
            detail = e.read().decode(errors="replace")[:300]
            if retryable and attempt == 1:
                time.sleep(min(float(e.headers.get("Retry-After") or 2), 10))
                continue
            log.warning("alert email failed: HTTP %s %s", e.code, detail)
            return None
        except Exception as e:  # noqa: BLE001 - alerting must never break the tick
            if attempt == 1:
                time.sleep(2)
                continue
            log.warning("alert email failed: %s", e)
            return None
    return None


def raise_alert(conn: psycopg.Connection, acct: uuid.UUID | None, condition: str, message: str) -> None:
    recent = conn.execute(
        """select 1 from garmin.alerts
           where account_id is not distinct from %s and condition = %s and created_at > now() - interval '24 hours'""",
        (acct, condition),
    ).fetchone()
    if recent:
        return
    # One key per condition per day: a retried tick can't send the same alert twice.
    key = f"{acct}-{condition}-{time.strftime('%Y%m%d')}"
    email_id = send_email(f"[garmin-datastore] {condition}", message, idempotency_key=key)
    conn.execute(
        "insert into garmin.alerts (account_id, condition, message, delivered) values (%s, %s, %s, %s)",
        (acct, condition, message + (f"\n(resend id: {email_id})" if email_id else ""), email_id is not None),
    )

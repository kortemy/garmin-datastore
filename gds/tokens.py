"""Encrypted Garmin token storage in garmin.garmin_tokens (design §7)."""

import base64
import datetime as dt
import json
import uuid

import psycopg

from gds import crypto
from gds.config import settings


def ensure_account(conn: psycopg.Connection, label: str) -> uuid.UUID:
    row = conn.execute(
        """insert into garmin.accounts (label) values (%s)
           on conflict (label) do update set label = excluded.label
           returning id""",
        (label,),
    ).fetchone()
    return row["id"]


def account_id(conn: psycopg.Connection, label: str) -> uuid.UUID:
    row = conn.execute("select id from garmin.accounts where label = %s", (label,)).fetchone()
    if not row:
        raise RuntimeError(f"No account '{label}'. Run `gds auth` first.")
    return row["id"]


def access_expiry(token_json: str) -> dt.datetime | None:
    """Expiry of the DI access token (JWT `exp` claim), or None if unreadable."""
    try:
        part = json.loads(token_json)["di_token"].split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part))["exp"]
        return dt.datetime.fromtimestamp(float(exp), tz=dt.UTC)
    except Exception:  # noqa: BLE001
        return None


def refresh_token_of(token_json: str) -> str | None:
    try:
        return json.loads(token_json).get("di_refresh_token")
    except Exception:  # noqa: BLE001
        return None


def load(conn: psycopg.Connection, acct: uuid.UUID) -> tuple[str, int]:
    row = conn.execute(
        "select ciphertext, nonce, version from garmin.garmin_tokens where account_id = %s", (acct,)
    ).fetchone()
    if not row:
        raise RuntimeError("No Garmin token stored. Run `gds auth` first.")
    return crypto.decrypt(bytes(row["ciphertext"]), bytes(row["nonce"]), settings().token_enc_key), row["version"]


def save(
    conn: psycopg.Connection,
    acct: uuid.UUID,
    token_json: str,
    event: str,
    refresh_rotated: bool | None = None,
) -> None:
    ciphertext, nonce = crypto.encrypt(token_json, settings().token_enc_key)
    expires = access_expiry(token_json)
    conn.execute(
        """insert into garmin.garmin_tokens (account_id, ciphertext, nonce, access_expires_at)
           values (%s, %s, %s, %s)
           on conflict (account_id) do update
             set ciphertext = excluded.ciphertext, nonce = excluded.nonce,
                 access_expires_at = excluded.access_expires_at,
                 version = garmin.garmin_tokens.version + 1, updated_at = now()""",
        (acct, ciphertext, nonce, expires),
    )
    conn.execute(
        """insert into garmin.token_events (account_id, event, refresh_rotated, access_expires_at)
           values (%s, %s, %s, %s)""",
        (acct, event, refresh_rotated, expires),
    )

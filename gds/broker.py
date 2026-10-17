"""Single-flight Garmin token broker (design §7.2).

Garmin rotates the refresh token on every refresh (M0), so two processes refreshing the
same token at once could leave one holding a dead token. The broker refreshes only under
a row lock, and only when the access token has < 20 min left. Ticks run ≤ 5 min, so the
library's own auto-refresh (15 min before expiry) never fires mid-run.
"""

import datetime as dt
import uuid

import psycopg
from garminconnect import Garmin

from gds import crypto, tokens
from gds.config import settings
from gds.source import AuthFailed, map_error

REFRESH_MARGIN = dt.timedelta(minutes=20)


def open_client(conn: psycopg.Connection, acct: uuid.UUID) -> tuple[Garmin, str]:
    """Return a logged-in client and the token JSON it started from."""
    key = settings().token_enc_key
    with conn.transaction():
        row = conn.execute(
            "select ciphertext, nonce from garmin.garmin_tokens where account_id = %s for update", (acct,)
        ).fetchone()
        if not row:
            raise AuthFailed("no token stored; run `gds auth`")
        token = crypto.decrypt(bytes(row["ciphertext"]), bytes(row["nonce"]), key)
        expires = tokens.access_expiry(token)
        if expires is None or expires - dt.datetime.now(dt.UTC) < REFRESH_MARGIN:
            fresh = Garmin()
            fresh.client.loads(token)
            try:
                fresh.client._refresh_di_token()  # private API, isolated here
            except Exception as e:  # noqa: BLE001
                raise map_error(e) from e
            new = fresh.client.dumps()
            rotated = tokens.refresh_token_of(new) != tokens.refresh_token_of(token)
            tokens.save(conn, acct, new, event="broker_refresh", refresh_rotated=rotated)
            token = new

    client = Garmin()
    try:
        client.login(token)
    except Exception as e:  # noqa: BLE001
        raise map_error(e) from e
    return client, token


def persist_if_changed(conn: psycopg.Connection, acct: uuid.UUID, client: Garmin, started_from: str) -> None:
    """Belt and braces: save the token if the library refreshed it during the run."""
    current = client.client.dumps()
    if current != started_from:
        rotated = tokens.refresh_token_of(current) != tokens.refresh_token_of(started_from)
        with conn.transaction():
            tokens.save(conn, acct, current, event="auto_refresh", refresh_rotated=rotated)

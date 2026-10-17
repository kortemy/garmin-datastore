"""Garmin client backed by the encrypted token in the database.

M0 version: loads the token, logs in with it inline, and persists it again if the
library refreshed it. The single-flight broker from design §7.2 replaces this in M1.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from garminconnect import Garmin

from gds import tokens
from gds.config import settings
from gds.db import connect


@contextmanager
def garmin_session(event: str = "auto_refresh") -> Iterator[Garmin]:
    with connect() as conn:
        acct = tokens.account_id(conn, settings().account_label)
        token_json, _ = tokens.load(conn, acct)

    g = Garmin()
    g.login(token_json)  # garminconnect accepts inline token JSON
    try:
        yield g
    finally:
        current = g.client.dumps()
        if current != token_json:
            rotated = tokens.refresh_token_of(current) != tokens.refresh_token_of(token_json)
            with connect() as conn:
                tokens.save(conn, acct, current, event, refresh_rotated=rotated)

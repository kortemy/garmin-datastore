"""Postgres access over Supabase's transaction pooler."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from gds.config import settings


@contextmanager
def connect(url: str | None = None, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Connection that commits on success. With autocommit=True, use `conn.transaction()`
    blocks for atomic units (the sync engine commits each work unit on its own)."""
    # The transaction pooler doesn't support prepared statements.
    conn = psycopg.connect(
        url or settings().database_url, prepare_threshold=None, row_factory=dict_row, autocommit=autocommit
    )
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()

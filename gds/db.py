"""Postgres access over Supabase's transaction pooler."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from gds.config import settings


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    # The transaction pooler doesn't support prepared statements.
    conn = psycopg.connect(url or settings().database_url, prepare_threshold=None, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

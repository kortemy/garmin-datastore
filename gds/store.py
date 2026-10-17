"""Idempotent writes (FR-8): typed-table upserts on natural keys, raw payloads by unit."""

import gzip
import hashlib
import json
import uuid
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

# Natural key of each typed table (account_id is always first).
TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "daily_summary": ("account_id", "calendar_date"),
    "sleep": ("account_id", "calendar_date"),
    "hrv": ("account_id", "calendar_date"),
    "training_readiness": ("account_id", "reading_utc"),
    "training_status": ("account_id", "calendar_date"),
    "intraday_series": ("account_id", "calendar_date", "metric"),
}


def encode_raw(payload: Any) -> tuple[bytes, str]:
    """Canonical JSON bytes and their SHA-256, so unchanged payloads are detectable."""
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return data, hashlib.sha256(data).hexdigest()


def upsert_sql(table: str, columns: list[str]) -> sql.Composed:
    keys = TABLE_KEYS[table]
    updates = [c for c in columns if c not in keys]
    set_clause = sql.SQL(", ").join(
        [sql.SQL("{c} = excluded.{c}").format(c=sql.Identifier(c)) for c in updates]
        + [sql.SQL("updated_at = now()")]
    )
    return sql.SQL("insert into garmin.{t} ({cols}) values ({vals}) on conflict ({keys}) do update set {set}").format(
        t=sql.Identifier(table),
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        vals=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
        keys=sql.SQL(", ").join(map(sql.Identifier, keys)),
        set=set_clause,
    )


def _adapt(v: Any) -> Any:
    return Jsonb(v) if isinstance(v, dict) else v


def upsert_rows(conn: psycopg.Connection, acct: uuid.UUID, table: str, rows: list[dict[str, Any]]) -> int:
    """Upsert rows; rows with different column sets (partial updates) are grouped separately,
    and only the columns a row provides are overwritten."""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(tuple(sorted(r)), []).append(r)
    with conn.cursor() as cur:
        for cols, group in groups.items():
            columns = ["account_id", *cols]
            cur.executemany(
                upsert_sql(table, columns),
                [(acct, *(_adapt(r[c]) for c in cols)) for r in group],
            )
    return len(rows)


def save_raw(
    conn: psycopg.Connection, acct: uuid.UUID, domain: str, scope_key: str, data: bytes, digest: str
) -> int:
    gz = gzip.compress(data, compresslevel=6)
    conn.execute(
        """insert into garmin.raw_objects
               (account_id, domain, scope_key, content_hash, bytes_raw, bytes_gz, payload_gz, fetched_at)
           values (%s, %s, %s, %s, %s, %s, %s, now())
           on conflict (account_id, domain, scope_key) do update
             set content_hash = excluded.content_hash, bytes_raw = excluded.bytes_raw,
                 bytes_gz = excluded.bytes_gz, payload_gz = excluded.payload_gz, fetched_at = now()""",
        (acct, domain, scope_key, digest, len(data), len(gz), gz),
    )
    return len(gz)


def load_raw(conn: psycopg.Connection, acct: uuid.UUID, domain: str, scope_key: str) -> Any:
    row = conn.execute(
        "select payload_gz from garmin.raw_objects where account_id = %s and domain = %s and scope_key = %s",
        (acct, domain, scope_key),
    ).fetchone()
    return json.loads(gzip.decompress(bytes(row["payload_gz"]))) if row else None

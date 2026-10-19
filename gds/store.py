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
    "activities": ("account_id", "activity_id"),
    "activity_laps": ("account_id", "activity_id", "lap_index"),
    "activity_zones": ("account_id", "activity_id", "zone_type", "zone"),
    "activity_exercise_sets": ("account_id", "activity_id", "set_index"),
    "activity_weather": ("account_id", "activity_id"),
    "activity_files": ("account_id", "activity_id", "format"),
    "weigh_ins": ("account_id", "sample_pk"),
    "blood_pressure": ("account_id", "measured_utc"),
    "performance_metrics": ("account_id", "calendar_date", "metric"),
    "entities": ("account_id", "kind", "entity_id"),
    "calendar_items": ("account_id", "item_type", "item_id"),
    "body_battery_events": ("account_id", "event_start_utc", "event_type"),
}


_COLUMN_TYPES: dict[str, dict[str, str]] = {}


def _column_types(conn: psycopg.Connection, table: str) -> dict[str, str]:
    """Column → SQL type name (e.g. 'timestamp with time zone') of a garmin table, cached."""
    if table not in _COLUMN_TYPES:
        rows = conn.execute(
            """select a.attname, format_type(a.atttypid, a.atttypmod) as type
               from pg_attribute a where a.attrelid = %s::regclass and a.attnum > 0 and not a.attisdropped""",
            (f"garmin.{table}",),
        ).fetchall()
        _COLUMN_TYPES[table] = {r["attname"]: r["type"] for r in rows}
    return _COLUMN_TYPES[table]


def replace_missing(
    conn: psycopg.Connection, acct: uuid.UUID, table: str, scope: dict[str, Any], keep: list[dict[str, Any]],
    between: tuple[str, Any, Any] | None = None,
) -> int:
    """Delete rows of `table` within `scope` (column = value, and/or `between` = (column, low,
    high) inclusive) whose natural key isn't in `keep` (a full snapshot of that scope, so
    anything absent was deleted in Garmin)."""
    if not scope and not between:
        raise ValueError("replace_missing needs a scope")
    keys = [k for k in TABLE_KEYS[table] if k != "account_id" and k not in scope]
    conds = [sql.SQL("t.{} = %s").format(sql.Identifier(c)) for c in scope]
    params: list[Any] = list(scope.values())
    if between:
        conds.append(sql.SQL("t.{} between %s and %s").format(sql.Identifier(between[0])))
        params += [between[1], between[2]]
    if keep:
        # Compare keys with their own types (one typed array per key column), not as text:
        # a timestamptz's text form differs between Python and Postgres.
        types = _column_types(conn, table)
        aliases = [sql.Identifier(f"k{i}") for i in range(len(keys))]
        conds.append(sql.SQL("not exists (select 1 from unnest({arrays}) as k({aliases}) where {match})").format(
            arrays=sql.SQL(", ").join(sql.SQL("%s::{}[]").format(sql.SQL(types[k])) for k in keys),
            aliases=sql.SQL(", ").join(aliases),
            match=sql.SQL(" and ").join(sql.SQL("t.{} = k.{}").format(sql.Identifier(k), a)
                                        for k, a in zip(keys, aliases, strict=True)),
        ))
        params += [[r[k] for r in keep] for k in keys]
    q = sql.SQL("delete from garmin.{t} as t where t.account_id = %s and {where}").format(
        t=sql.Identifier(table), where=sql.SQL(" and ").join(conds)
    )
    return conn.execute(q, (acct, *params)).rowcount


def encode_raw(payload: Any) -> tuple[bytes, str]:
    """Canonical bytes and their SHA-256, so unchanged payloads are detectable.
    JSON payloads are canonicalised; binary payloads (FIT zips) are hashed as-is."""
    if isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
    else:
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
    """dicts and lists of dicts → jsonb; lists of scalars stay Postgres arrays."""
    if isinstance(v, dict) or (isinstance(v, list) and any(isinstance(x, dict) for x in v)):
        return Jsonb(v)
    return v


def ensure_units(conn: psycopg.Connection, acct: uuid.UUID, units: list[dict[str, Any]]) -> int:
    """Create work units a normaliser asked for; `requeue` ones are reset to pending."""
    if not units:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """insert into garmin.work_units (account_id, domain, scope_key, priority)
               values (%s, %s, %s, %s)
               on conflict (account_id, domain, scope_key) do update
                 set status = 'pending', priority = greatest(garmin.work_units.priority, excluded.priority),
                     attempts = 0, next_attempt_at = now(), updated_at = now()
                 where %s and garmin.work_units.status <> 'pending'""",
            [(acct, u["domain"], u["scope_key"], u.get("priority", 10), bool(u.get("requeue"))) for u in units],
        )
    return len(units)


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

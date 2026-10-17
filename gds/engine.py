"""Sync engine (design §6): lease → plan → run work units within a time budget → log.

A tick is safe to run at any moment: a held lease makes it exit at once, every unit
commits on its own, and anything unfinished is picked up by the next tick.
"""

import datetime as dt
import hashlib
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from gds import broker, store
from gds.alerts import raise_alert
from gds.config import settings
from gds.db import connect
from gds.domains import REGISTRY, daily_domains, domains_in
from gds.source import AuthFailed, Blocked, RateLimited, Source, SourceError, Transient

log = logging.getLogger(__name__)

PRIORITY_MANUAL = 200
PRIORITY_INCREMENTAL = 100
MAX_ATTEMPTS = 6
SAFETY_MARGIN_S = 20  # stop picking new units this close to the budget


@dataclass
class TickStats:
    done: int = 0
    unchanged: int = 0
    empty: int = 0
    failed: int = 0
    per_domain: dict[str, int] = field(default_factory=dict)


def backoff(attempts: int) -> dt.timedelta:
    """5 min, 10, 20, 40, 80 … capped at 6 h."""
    return dt.timedelta(minutes=min(5 * 2 ** max(attempts - 1, 0), 360))


# ---------------------------------------------------------------- lease


def acquire_lease(conn: psycopg.Connection, name: str, holder: str, ttl_s: float) -> bool:
    row = conn.execute(
        """insert into garmin.leases (name, holder, expires_at)
           values (%s, %s, now() + make_interval(secs => %s))
           on conflict (name) do update set holder = excluded.holder, expires_at = excluded.expires_at
             where garmin.leases.expires_at < now()
           returning holder""",
        (name, holder, ttl_s),
    ).fetchone()
    return bool(row and row["holder"] == holder)


def release_lease(conn: psycopg.Connection, name: str, holder: str) -> None:
    conn.execute("delete from garmin.leases where name = %s and holder = %s", (name, holder))


# ---------------------------------------------------------------- planning


def account_today(acct: dict[str, Any]) -> dt.date:
    return dt.datetime.now(ZoneInfo(acct["home_timezone"] or "UTC")).date()


def month_key(d: dt.date, shift: int = 0) -> str:
    y, m = divmod(d.year * 12 + d.month - 1 + shift, 12)
    return f"{y:04d}-{m + 1:02d}"


def months_between(start: dt.date, end_key: str) -> list[str]:
    keys, i = [], 0
    while (k := month_key(start, i)) <= end_key:
        keys.append(k)
        i += 1
    return keys


def _registry_signature() -> str:
    return hashlib.sha1("|".join(sorted(f"{d.name}:{d.scope}:{d.months_ahead}" for d in REGISTRY.values()))
                        .encode()).hexdigest()[:12]


def _create_units(conn: psycopg.Connection, acct: dict[str, Any], start: dt.date, today: dt.date) -> int:
    """Insert every unit that should exist (idempotent). Only runs when the day, the
    backfill start or the domain registry changed, so ordinary ticks skip it."""
    acct_id = acct["id"]
    daily = daily_domains()
    created = conn.execute(
        """insert into garmin.work_units (account_id, domain, scope_key, priority)
           select %s, d.name, to_char(day, 'YYYY-MM-DD'), d.priority
           from generate_series(%s::date, %s::date, interval '1 day') as day
           cross join unnest(%s::text[], %s::int[]) as d(name, priority)
           on conflict do nothing""",
        (acct_id, start, today, [d.name for d in daily], [d.priority for d in daily]),
    ).rowcount
    keyed: list[tuple[str, str, int]] = []
    for d in domains_in("range"):
        keyed += [(d.name, m, d.priority) for m in months_between(start, month_key(today, d.months_ahead))]
    keyed += [(d.name, "latest", d.priority) for d in domains_in("snapshot")]
    # Activity index: "all" walks the full history (first run, then weekly to pick up
    # edits), "recent" checks the newest page on every incremental pass.
    keyed += [("activity_index", "all", REGISTRY["activity_index"].priority),
              ("activity_index", "recent", PRIORITY_INCREMENTAL)]
    created += conn.execute(
        """insert into garmin.work_units (account_id, domain, scope_key, priority)
           select %s, u.domain, u.scope_key, u.priority
           from unnest(%s::text[], %s::text[], %s::int[]) as u(domain, scope_key, priority)
           on conflict do nothing""",
        (acct_id, [k[0] for k in keyed], [k[1] for k in keyed], [k[2] for k in keyed]),
    ).rowcount
    return created


def plan(conn: psycopg.Connection, acct: dict[str, Any]) -> dict[str, Any]:
    """Make sure every unit exists, re-queue snapshot/range units that are due for a
    refresh, and queue the recent window when an incremental pass is due."""
    s = settings()
    today = account_today(acct)
    window_start = today - dt.timedelta(days=s.recent_window_days)
    start = min(acct["backfill_start"] or window_start, window_start)
    names = [d.name for d in daily_domains()]
    signature = f"{today}|{start}|{_registry_signature()}"
    with conn.transaction():
        created = 0
        if acct.get("planned_signature") != signature:
            created = _create_units(conn, acct, start, today)
            conn.execute("update garmin.accounts set planned_signature = %s where id = %s", (signature, acct["id"]))
        # Snapshots and the live months of range domains are re-fetched every refresh_hours.
        refreshing = [d for d in REGISTRY.values() if d.refresh_hours and d.scope in ("snapshot", "range")]
        live = ["latest"] + [month_key(today, i) for i in (-1, 0, 1, 2)]
        refreshed = conn.execute(
            """update garmin.work_units w
               set status = 'pending', attempts = 0, next_attempt_at = now(), updated_at = now()
               from unnest(%s::text[], %s::float8[]) as r(domain, hours)
               where w.account_id = %s and w.domain = r.domain and w.scope_key = any(%s)
                 and w.status <> 'pending'
                 and (w.last_fetched_at is null or w.last_fetched_at < now() - r.hours * interval '1 hour')""",
            ([d.name for d in refreshing], [d.refresh_hours for d in refreshing], acct["id"], live),
        ).rowcount
        conn.execute(
            """update garmin.work_units
               set status = 'pending', priority = %s, attempts = 0, next_attempt_at = now(), updated_at = now()
               where account_id = %s and domain = 'activity_index' and scope_key = 'all' and status <> 'pending'
                 and (last_fetched_at is null or last_fetched_at < now() - interval '7 days')""",
            (REGISTRY["activity_index"].priority, acct["id"]),
        )
        due = acct["last_incremental_at"] is None or (
            dt.datetime.now(dt.UTC) - acct["last_incremental_at"] >= dt.timedelta(hours=s.incremental_every_h)
        )
        queued = 0
        if due:
            queued = conn.execute(
                """update garmin.work_units
                   set status = 'pending', priority = %s, attempts = 0, next_attempt_at = now(), updated_at = now()
                   where account_id = %s
                     and ((domain = any(%s) and scope_key >= %s)
                          or (domain = 'activity_index' and scope_key = 'recent'))""",
                (PRIORITY_INCREMENTAL, acct["id"], names, window_start.isoformat()),
            ).rowcount
            conn.execute("update garmin.accounts set last_incremental_at = now() where id = %s", (acct["id"],))
    return {"units_created": created, "incremental": due, "units_requeued": queued, "units_refreshed": refreshed}


def request_resync(
    conn: psycopg.Connection, acct_id: uuid.UUID, domain: str | None, start: str | None, end: str | None
) -> int:
    """FR-9: queue matching units at top priority; the next tick processes them."""
    return conn.execute(
        """update garmin.work_units
           set status = 'pending', priority = %s, attempts = 0, next_attempt_at = now(), updated_at = now()
           where account_id = %s
             and (%s::text is null or domain = %s)
             and (%s::text is null or scope_key >= %s)
             and (%s::text is null or scope_key <= %s)""",
        (PRIORITY_MANUAL, acct_id, domain, domain, start, start, end, end),
    ).rowcount


# ---------------------------------------------------------------- units


def _next_units(conn: psycopg.Connection, acct_id: uuid.UUID, limit: int = 25) -> list[dict[str, Any]]:
    return conn.execute(
        """select domain, scope_key, attempts, content_hash from garmin.work_units
           where account_id = %s and status = 'pending' and next_attempt_at <= now()
           order by priority desc, scope_key desc
           limit %s""",
        (acct_id, limit),
    ).fetchall()


def _mark(conn: psycopg.Connection, acct_id: uuid.UUID, unit: dict[str, Any], status: str, **extra: Any) -> None:
    conn.execute(
        """update garmin.work_units
           set status = %s, priority = least(priority, 10), attempts = %s, last_error = %s,
               content_hash = coalesce(%s, content_hash), last_fetched_at = coalesce(%s, last_fetched_at),
               next_attempt_at = %s, updated_at = now()
           where account_id = %s and domain = %s and scope_key = %s""",
        (
            status,
            extra.get("attempts", 0),
            extra.get("error"),
            extra.get("content_hash"),
            extra.get("fetched_at"),
            extra.get("next_attempt_at", dt.datetime.now(dt.UTC)),
            acct_id,
            unit["domain"],
            unit["scope_key"],
        ),
    )


def apply_rows(conn: psycopg.Connection, acct_id: uuid.UUID, rows: dict[str, Any]) -> int:
    """Write a normaliser's output: upserts, requested work units, then replace-scopes."""
    written = 0
    for table, table_rows in rows.items():
        if table == "work_units":
            store.ensure_units(conn, acct_id, table_rows)
        elif table != "_replace":
            written += store.upsert_rows(conn, acct_id, table, table_rows)
    for spec in rows.get("_replace", []):
        keep = [r for r in rows.get(spec["table"], []) if all(r.get(c) == v for c, v in spec["scope"].items())]
        store.replace_missing(conn, acct_id, spec["table"], spec["scope"], keep)
    return written


def process_unit(conn: psycopg.Connection, src: Source, acct_id: uuid.UUID, unit: dict[str, Any]) -> str:
    """Fetch → hash → (if changed) store raw + normalised rows atomically. Returns the outcome."""
    domain = REGISTRY.get(unit["domain"])
    if domain is None:  # domain removed from the registry
        with conn.transaction():
            _mark(conn, acct_id, unit, "failed", error="unknown domain", attempts=MAX_ATTEMPTS)
        return "failed"

    now = dt.datetime.now(dt.UTC)
    try:
        raw = domain.fetch(src, unit["scope_key"])
    except (RateLimited, Blocked, AuthFailed):
        raise  # account-level: the tick stops
    except SourceError as e:
        return _fail(conn, acct_id, unit, str(e))

    if raw in (None, [], {}) or raw == b"":
        with conn.transaction():
            _mark(conn, acct_id, unit, "empty", fetched_at=now)
        return "empty"

    data, digest = store.encode_raw(raw)
    if digest == unit["content_hash"]:
        with conn.transaction():
            _mark(conn, acct_id, unit, "done", fetched_at=now)
        return "unchanged"

    try:
        rows = domain.normalize(raw, unit["scope_key"]) if domain.normalize else {}
        normalize_error = None
    except Exception as e:  # noqa: BLE001 - a mapping bug must not lose the raw payload
        rows, normalize_error = {}, f"normalize: {type(e).__name__}: {e}"[:300]

    with conn.transaction():
        if not domain.binary:  # binary payloads (FIT zips) are stored by their own table
            store.save_raw(conn, acct_id, unit["domain"], unit["scope_key"], data, digest)
        apply_rows(conn, acct_id, rows)
        if normalize_error:
            # Raw is saved; keep the hash empty so a fixed normaliser reprocesses it.
            attempts = unit["attempts"] + 1
            _mark(
                conn, acct_id, unit, "failed" if attempts >= MAX_ATTEMPTS else "pending",
                attempts=attempts, error=normalize_error, fetched_at=now,
                next_attempt_at=now + backoff(attempts),
            )
            return "failed"
        typed = {t: r for t, r in rows.items() if t not in ("work_units", "_replace")}
        status = "empty" if domain.normalize and not typed else "done"
        _mark(conn, acct_id, unit, status, content_hash=digest, fetched_at=now)
    return status


def _fail(conn: psycopg.Connection, acct_id: uuid.UUID, unit: dict[str, Any], error: str) -> str:
    attempts = unit["attempts"] + 1
    with conn.transaction():
        _mark(
            conn, acct_id, unit, "failed" if attempts >= MAX_ATTEMPTS else "pending",
            attempts=attempts, error=error[:300],
            next_attempt_at=dt.datetime.now(dt.UTC) + backoff(attempts),
        )
    return "failed"


def renormalize(conn: psycopg.Connection, acct_id: uuid.UUID, domain_name: str) -> dict[str, int]:
    """Rebuild typed rows for one domain from stored raw payloads (no Garmin calls).
    Use after adding or fixing a normaliser."""
    domain = REGISTRY[domain_name]
    if domain.normalize is None or domain.binary:
        return {"units": 0, "rows": 0}
    keys = [r["scope_key"] for r in conn.execute(
        "select scope_key from garmin.raw_objects where account_id = %s and domain = %s", (acct_id, domain_name)
    ).fetchall()]
    total_rows = 0
    for key in keys:
        raw = store.load_raw(conn, acct_id, domain_name, key)
        with conn.transaction():
            total_rows += apply_rows(conn, acct_id, domain.normalize(raw, key))
    return {"units": len(keys), "rows": total_rows}


# ---------------------------------------------------------------- tick


def _load_account(conn: psycopg.Connection, label: str) -> dict[str, Any] | None:
    return conn.execute("select * from garmin.accounts where label = %s", (label,)).fetchone()


def _set_account_status(
    conn: psycopg.Connection, acct_id: uuid.UUID, status: str, reason: str | None, cooldown: dt.timedelta | None = None
) -> None:
    conn.execute(
        """update garmin.accounts set status = %s, status_reason = %s,
               cooldown_until = case when %s::interval is null then null else now() + %s::interval end
           where id = %s""",
        (status, reason, cooldown, cooldown, acct_id),
    )


def run_tick(trigger: str) -> dict[str, Any]:
    s = settings()
    started = time.monotonic()
    deadline = started + s.tick_budget_s - SAFETY_MARGIN_S
    holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    stats = TickStats()

    with connect(autocommit=True) as conn:
        acct = _load_account(conn, s.account_label)
        if acct is None:
            return {"status": "skipped", "reason": f"no account '{s.account_label}'"}
        lease = f"tick:{acct['id']}"
        if not acquire_lease(conn, lease, holder, s.tick_budget_s + 60):
            return {"status": "skipped", "reason": "another tick is running"}

        run_id = conn.execute(
            "insert into garmin.sync_runs (account_id, trigger) values (%s, %s) returning id", (acct["id"], trigger)
        ).fetchone()["id"]
        status, error, details, src = "ok", None, {}, None
        try:
            if acct["status"] == "needs_reauth":
                status, error = "skipped", "account needs re-auth (run `gds auth`)"
            elif acct["cooldown_until"] and acct["cooldown_until"] > dt.datetime.now(dt.UTC):
                status, error = "skipped", f"cooling down until {acct['cooldown_until']:%H:%M} UTC"
            else:
                if acct["status"] == "cooldown":
                    _set_account_status(conn, acct["id"], "active", None)
                details["plan"] = plan(conn, acct)
                client, token = broker.open_client(conn, acct["id"])
                src = Source(client, s.request_interval_s)
                try:
                    while time.monotonic() < deadline:
                        units = _next_units(conn, acct["id"])
                        if not units:
                            break
                        for unit in units:
                            if time.monotonic() >= deadline:
                                break
                            outcome = process_unit(conn, src, acct["id"], unit)
                            setattr(stats, outcome, getattr(stats, outcome) + 1)
                            stats.per_domain[unit["domain"]] = stats.per_domain.get(unit["domain"], 0) + 1
                finally:
                    broker.persist_if_changed(conn, acct["id"], client, token)
                if stats.failed:
                    status = "partial"
        except AuthFailed as e:
            status, error = "failed", f"auth: {e}"
            _set_account_status(conn, acct["id"], "needs_reauth", str(e)[:200])
            raise_alert(conn, acct["id"], "needs_reauth",
                        "Garmin no longer accepts the stored token.\n\n"
                        "Re-authenticate on your PC:\n  uv run python -m gds.cli auth\n\n"
                        f"Detail: {e}")
        except RateLimited as e:
            status, error = "partial", f"rate limited: {e}"
            _set_account_status(conn, acct["id"], "cooldown", "429 from Garmin", dt.timedelta(minutes=15))
        except Blocked as e:
            status, error = "partial", f"blocked: {e}"
            _set_account_status(conn, acct["id"], "cooldown", "403 from Garmin", dt.timedelta(hours=1))
            recent_blocks = conn.execute(
                """select count(*) as n from garmin.sync_runs
                   where account_id = %s and error like 'blocked:%%' and started_at > now() - interval '6 hours'""",
                (acct["id"],),
            ).fetchone()["n"]
            if recent_blocks >= 2:
                raise_alert(conn, acct["id"], "garmin_blocking",
                            f"Garmin has blocked requests from the worker {recent_blocks + 1} times in 6 hours.")
        except Transient as e:
            status, error = "failed", f"transient: {e}"
        except Exception as e:  # noqa: BLE001
            log.exception("tick failed")
            status, error = "failed", f"{type(e).__name__}: {e}"[:500]
        finally:
            details["per_domain"] = stats.per_domain
            details["unchanged"] = stats.unchanged
            details["seconds"] = round(time.monotonic() - started, 1)
            conn.execute(
                """update garmin.sync_runs set finished_at = now(), status = %s, error = %s,
                       units_done = %s, units_empty = %s, units_failed = %s, requests = %s, details = %s
                   where id = %s""",
                (status, error, stats.done + stats.unchanged, stats.empty, stats.failed,
                 src.requests if src else 0, Jsonb(details), run_id),
            )
            release_lease(conn, lease, holder)

    return {
        "status": status,
        "error": error,
        "run_id": run_id,
        "done": stats.done,
        "unchanged": stats.unchanged,
        "empty": stats.empty,
        "failed": stats.failed,
        "requests": src.requests if src else 0,
        **details,
    }

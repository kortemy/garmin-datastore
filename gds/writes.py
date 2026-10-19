"""Write tools: changes to Garmin made on the owner's request (design §8.3, requirements §4.6).

Every write goes through `execute`:
  1. a plan is built and validated (DB reads only: ownership, current values, ids);
  2. `dry_run=True` stops here and returns the preview Claude shows for confirmation (FR-17);
  3. the idempotency key is claimed in `write_log` (a retried call returns the first result);
  4. the Garmin call runs on a broker client, errors mapped like the sync's;
  5. the outcome is logged (FR-19) and the affected work units are re-synced: run inline once,
     then queued again for the next tick in case Garmin's read side lags.

Ownership (FR-18): reschedule / unschedule / delete / undo only touch entities whose
`write_log` row says `created_by_system`. Nothing here deletes data the system didn't create.
"""

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from gds import broker, engine, store, workout_builder
from gds.config import settings
from gds.db import connect
from gds.domains import activity_domains
from gds.source import AuthFailed, Invalid, Source, SourceError
from gds.tools import ToolError

log = logging.getLogger(__name__)

PENDING_GRACE = dt.timedelta(minutes=5)
REQUEUE_DELAY = dt.timedelta(minutes=2)


@dataclass
class Ctx:
    conn: psycopg.Connection
    acct_id: uuid.UUID
    tz: ZoneInfo

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)


@dataclass
class Outcome:
    response: Any
    entity_kind: str | None = None
    entity_id: str | None = None
    entity_date: dt.date | None = None
    created_by_system: bool = False
    undo: dict[str, Any] | None = None
    resync: list[tuple[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    # Runs in the DB after the log row is written (e.g. mark the undone write).
    after: Callable[[psycopg.Connection, int], None] | None = None


class Unclear(ToolError):
    """Garmin may have applied the write but the response was unexpected: the key stays
    reserved (status pending) so a retry can't write twice."""


@dataclass
class Plan:
    preview: str
    run: Callable[[Source], Outcome]
    details: dict[str, Any] = field(default_factory=dict)  # extra dry-run info (e.g. built workout)


# ---------------------------------------------------------------- executor


def _account(conn: psycopg.Connection) -> dict[str, Any]:
    row = conn.execute("select id, home_timezone, status from garmin.accounts where label = %s",
                       (settings().account_label,)).fetchone()
    if not row:
        raise ToolError("No Garmin account is configured for this deployment.")
    if row["status"] == "needs_reauth":
        raise ToolError("Garmin no longer accepts the stored login; the owner must run `gds auth` before writing.")
    return row


def _timezone(conn: psycopg.Connection, acct: dict[str, Any]) -> ZoneInfo:
    """Garmin's own timezone setting (an IANA name, which Garmin's APIs expect) when synced,
    else the configured home timezone."""
    for name in (store.load_raw(conn, acct["id"], "s_user_timezone", "latest"), acct["home_timezone"]):
        if isinstance(name, str) and name:
            try:
                return ZoneInfo(name)
            except (KeyError, ValueError):
                continue
    return ZoneInfo("UTC")


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, str | int | float | bool) or obj is None:
        return obj
    return str(obj)


def execute(tool: str, key: str, inputs: dict[str, Any], make_plan: Callable[[Ctx], Plan],
            dry_run: bool = False) -> dict[str, Any]:
    key = (key or "").strip()
    if not dry_run and not 8 <= len(key) <= 200:
        raise ToolError("idempotency_key: a unique string of 8-200 characters (e.g. a UUID) is required. "
                        "Use a new key for each distinct write and the same key when retrying it.")
    inputs = _jsonable({k: v for k, v in inputs.items() if v is not None})
    with connect(autocommit=True) as conn:
        acct = _account(conn)
        ctx = Ctx(conn, acct["id"], _timezone(conn, acct))
        if not dry_run:
            replay = _claim(conn, acct["id"], key, tool, inputs)
            if replay is not None:
                return replay
        try:
            plan = make_plan(ctx)
        except ToolError as e:
            if not dry_run:
                _finish(conn, acct["id"], key, "failed", error=str(e))
            raise
        if dry_run:
            return {"dry_run": True, "preview": plan.preview, **plan.details,
                    "next": "Show this preview to the user and call again with dry_run=false once they confirm."}
        conn.execute("update garmin.write_log set preview = %s where account_id = %s and idempotency_key = %s",
                     (plan.preview, acct["id"], key))

        try:
            client, token = broker.open_client(conn, acct["id"])
        except SourceError as e:
            _finish(conn, acct["id"], key, "failed", error=f"{type(e).__name__}: {e}")
            raise ToolError(_explain(e)) from e
        src = Source(client, min_interval_s=0.3)
        try:
            try:
                outcome = plan.run(src)
            except Unclear as e:
                conn.execute("update garmin.write_log set error = %s where account_id = %s and idempotency_key = %s",
                             (str(e)[:1000], acct["id"], key))
                raise
            except (SourceError, ToolError) as e:
                _finish(conn, acct["id"], key, "failed", error=f"{type(e).__name__}: {e}")
                raise ToolError(_explain(e)) from e
            write_id = _finish(conn, acct["id"], key, "ok", outcome=outcome)
            if outcome.after:
                outcome.after(conn, write_id)
            synced = _resync(conn, src, acct["id"], outcome.resync)
            conn.execute("update garmin.write_log set resync = %s where id = %s", (Jsonb(synced), write_id))
        finally:
            broker.persist_if_changed(conn, acct["id"], client, token)

    result: dict[str, Any] = {"status": "ok", "write_id": write_id, "written": plan.preview}
    if outcome.entity_kind:
        result |= {"entity_kind": outcome.entity_kind, "entity_id": outcome.entity_id}
    result |= outcome.extra
    result["synced"] = synced
    return result


def _explain(e: Exception) -> str:
    if isinstance(e, ToolError):
        return str(e)
    if isinstance(e, AuthFailed):
        return "Garmin rejected the stored login; nothing was written. The owner must run `gds auth`."
    if isinstance(e, Invalid):
        return f"Garmin's client rejected the input, nothing was sent: {e}"
    return f"Garmin returned an error; the write most likely did not happen: {e}"


def _claim(conn: psycopg.Connection, acct_id: uuid.UUID, key: str, tool: str,
           inputs: dict[str, Any]) -> dict[str, Any] | None:
    """Reserve the idempotency key. Returns the earlier result when the key was already used."""
    with conn.transaction():
        row = conn.execute(
            "select * from garmin.write_log where account_id = %s and idempotency_key = %s for update",
            (acct_id, key),
        ).fetchone()
        if row is None:
            conn.execute(
                """insert into garmin.write_log (account_id, idempotency_key, tool, input)
                   values (%s, %s, %s, %s)""",
                (acct_id, key, tool, Jsonb(inputs)),
            )
            return None
        if row["tool"] != tool or row["input"] != inputs:
            raise ToolError("This idempotency_key was already used for a different write. Use a new key.")
        if row["status"] == "ok":
            return {"status": "ok", "replayed": True, "write_id": row["id"], "written": row["preview"],
                    "entity_kind": row["entity_kind"], "entity_id": row["entity_id"],
                    "note": "Already written earlier with this idempotency_key; nothing was sent again."}
        if row["status"] == "pending":
            if row["created_at"] > dt.datetime.now(dt.UTC) - PENDING_GRACE:
                raise ToolError("This write is still in progress; wait a moment and check the result.")
            raise ToolError("An earlier attempt with this key never finished, so its outcome is unknown. Check "
                            "Garmin (e.g. get_body / get_calendar) and use a new key if it is missing.")
        conn.execute(  # failed before: Garmin did not take it, so a retry is safe
            """update garmin.write_log set status = 'pending', error = null, created_at = now(), finished_at = null
               where id = %s""",
            (row["id"],),
        )
    return None


def _finish(conn: psycopg.Connection, acct_id: uuid.UUID, key: str, status: str,
            outcome: Outcome | None = None, error: str | None = None) -> int:
    o = outcome or Outcome(None)
    return conn.execute(
        """update garmin.write_log
           set status = %s, error = %s, garmin_response = %s, entity_kind = %s, entity_id = %s, entity_date = %s,
               created_by_system = %s, undo = %s, finished_at = now()
           where account_id = %s and idempotency_key = %s
           returning id""",
        (status, (error or "")[:1000] or None, Jsonb(_jsonable(o.response)) if o.response is not None else None,
         o.entity_kind, o.entity_id, o.entity_date, o.created_by_system,
         Jsonb(_jsonable(o.undo)) if o.undo else None, acct_id, key),
    ).fetchone()["id"]


def _resync(conn: psycopg.Connection, src: Source, acct_id: uuid.UUID,
            units: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """FR-19: refresh what the write touched now, and once more on the next tick."""
    done = []
    for domain, scope_key in dict.fromkeys(units):
        store.ensure_units(conn, acct_id, [{"domain": domain, "scope_key": scope_key,
                                            "priority": engine.PRIORITY_MANUAL, "requeue": True}])
        unit = conn.execute(
            """select domain, scope_key, attempts, content_hash from garmin.work_units
               where account_id = %s and domain = %s and scope_key = %s""",
            (acct_id, domain, scope_key),
        ).fetchone()
        try:
            outcome = engine.process_unit(conn, src, acct_id, unit) if unit else "missing"
        except Exception as e:  # noqa: BLE001 - the write succeeded; the next tick retries the sync
            log.warning("inline resync of %s/%s failed: %s", domain, scope_key, e)
            outcome = f"queued ({type(e).__name__})"
        conn.execute(
            """update garmin.work_units set status = 'pending', priority = %s, attempts = 0,
                   next_attempt_at = now() + %s, updated_at = now()
               where account_id = %s and domain = %s and scope_key = %s""",
            (engine.PRIORITY_MANUAL, REQUEUE_DELAY, acct_id, domain, scope_key),
        )
        done.append({"domain": domain, "scope_key": scope_key, "outcome": outcome})
    return done


# ---------------------------------------------------------------- shared helpers


def _month(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _day(value: str | None, ctx: Ctx, field_name: str = "date") -> dt.date:
    if not value:
        return ctx.now().date()
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError as e:
        raise ToolError(f"{field_name}: use YYYY-MM-DD, got {value!r}") from e


def _moment(date: str | None, time: str | None, ctx: Ctx, allow_future_days: int = 0) -> dt.datetime:
    """Local date + HH:MM → aware datetime in the account's timezone (default: now)."""
    now = ctx.now()
    if not date and not time:
        return now.replace(microsecond=0)
    day = _day(date, ctx)
    if time:
        try:
            t = dt.time.fromisoformat(time.strip())
        except ValueError as e:
            raise ToolError(f"time: use HH:MM (24 h, local), got {time!r}") from e
    else:
        t = now.time() if day == now.date() else dt.time(8, 0)
    moment = dt.datetime.combine(day, t.replace(microsecond=0), ctx.tz)
    if moment > now + dt.timedelta(days=allow_future_days, minutes=5):
        raise ToolError(f"{moment:%Y-%m-%d %H:%M} is in the future.")
    if moment < now - dt.timedelta(days=3650):
        raise ToolError(f"{moment:%Y-%m-%d} is more than 10 years ago.")
    return moment


def _fmt_local(m: dt.datetime) -> str:
    return f"{m:%a %Y-%m-%d %H:%M} ({m.tzname()})"


def _num(value: Any, field_name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not lo <= value <= hi:
        raise ToolError(f"{field_name}: a number between {lo:g} and {hi:g}, got {value!r}")
    return float(value)


def _owned(ctx: Ctx, kind: str, entity_id: str) -> dict[str, Any]:
    """The write that created this entity, if this system created it (FR-18)."""
    row = ctx.conn.execute(
        """select * from garmin.write_log
           where account_id = %s and entity_kind = %s and entity_id = %s and created_by_system and status = 'ok'
           order by id desc limit 1""",
        (ctx.acct_id, kind, str(entity_id)),
    ).fetchone()
    if not row:
        raise ToolError(f"{kind.replace('_', ' ').capitalize()} {entity_id} was not created by this system, so it "
                        "won't be changed or removed here. The owner can do that in Garmin Connect.")
    if row["undone_by"]:
        raise ToolError(f"{kind.replace('_', ' ').capitalize()} {entity_id} was already removed (write "
                        f"{row['undone_by']}).")
    return row


def _mark_undone(original_id: int) -> Callable[[psycopg.Connection, int], None]:
    def after(conn: psycopg.Connection, write_id: int) -> None:
        conn.execute("update garmin.write_log set undone_by = %s where id = %s", (write_id, original_id))
    return after


def _first(obj: Any, *keys: str) -> Any:
    """First non-null value of `keys` in a (possibly nested-once) response dict."""
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if obj.get(k) is not None:
            return obj[k]
    for v in obj.values():
        if isinstance(v, dict):
            for k in keys:
                if v.get(k) is not None:
                    return v[k]
    return None


def _raw_snapshot(ctx: Ctx, domain: str) -> Any:
    return store.load_raw(ctx.conn, ctx.acct_id, domain, "latest")


# ---------------------------------------------------------------- W-1 weight


def _ms(m: dt.datetime) -> int:
    return int(m.timestamp() * 1000)


def _find_weigh_in(src: Source, day: dt.date, gmt_ms: int, weight_kg: float) -> dict[str, Any] | None:
    data = src.call("get_daily_weigh_ins", day.isoformat()) or {}
    best = None
    for w in data.get("dateWeightList") or []:
        ts = w.get("timestampGMT") or w.get("date")
        if not isinstance(ts, int | float) or w.get("weight") is None:
            continue
        if abs(ts - gmt_ms) <= 120_000 and abs(w["weight"] / 1000 - weight_kg) < 0.05:
            if best is None or abs(ts - gmt_ms) < abs(best["timestampGMT"] - gmt_ms):
                best = w
    return best


def log_weight(key: str, weight_kg: float, date: str | None = None, time: str | None = None,
               body_fat_pct: float | None = None, body_water_pct: float | None = None,
               muscle_mass_kg: float | None = None, bone_mass_kg: float | None = None,
               visceral_fat_rating: float | None = None, metabolic_age: int | None = None,
               dry_run: bool = False) -> dict[str, Any]:
    inputs = locals().copy()
    inputs.pop("key")
    inputs.pop("dry_run")

    def plan(ctx: Ctx) -> Plan:
        w = _num(weight_kg, "weight_kg", 20, 400)
        comp = {
            "percent_fat": body_fat_pct is not None and _num(body_fat_pct, "body_fat_pct", 2, 75),
            "percent_hydration": body_water_pct is not None and _num(body_water_pct, "body_water_pct", 20, 80),
            "muscle_mass": muscle_mass_kg is not None and _num(muscle_mass_kg, "muscle_mass_kg", 5, 200),
            "bone_mass": bone_mass_kg is not None and _num(bone_mass_kg, "bone_mass_kg", 0.5, 20),
            "visceral_fat_rating": visceral_fat_rating is not None and _num(visceral_fat_rating,
                                                                            "visceral_fat_rating", 1, 60),
            "metabolic_age": metabolic_age is not None and _num(metabolic_age, "metabolic_age", 10, 120),
        }
        comp = {k: v for k, v in comp.items() if v is not False}
        moment = _moment(date, time, ctx)
        last = ctx.conn.execute(
            """select weight_kg, calendar_date from garmin.weigh_ins
               where account_id = %s and weight_kg is not null order by measured_utc desc limit 1""",
            (ctx.acct_id,),
        ).fetchone()
        parts = [f"Weigh-in {w:g} kg at {_fmt_local(moment)}"]
        labels = {"percent_fat": "body fat %", "percent_hydration": "body water %", "muscle_mass": "muscle kg",
                  "bone_mass": "bone kg", "visceral_fat_rating": "visceral fat", "metabolic_age": "metabolic age"}
        parts += [f"{labels[k]} {v:g}" for k, v in comp.items()]
        preview = ", ".join(parts)
        if last:
            delta = w - float(last["weight_kg"])
            preview += f" (last weigh-in {float(last['weight_kg']):g} kg on {last['calendar_date']}, {delta:+.1f} kg)"
            if abs(delta) > 0.1 * float(last["weight_kg"]):
                preview += " — more than 10% different from the last weigh-in; double-check the number"

        def run(src: Source) -> Outcome:
            gmt = moment.astimezone(dt.UTC)
            if comp:
                resp = src.write("add_body_composition", moment.isoformat(), w, **comp)
            else:
                resp = src.write("add_weigh_in_with_timestamps", w, "kg", moment.isoformat(),
                                 gmt.replace(tzinfo=None).isoformat())
            found = _find_weigh_in(src, moment.date(), _ms(gmt), w)
            sample_pk = _first(resp, "samplePk") or (found or {}).get("samplePk")
            return Outcome(
                resp, "weigh_in", str(sample_pk) if sample_pk else None, moment.date(), True,
                undo={"date": moment.date().isoformat(), "gmt_ms": _ms(gmt), "weight_kg": w},
                resync=[("body_composition", _month(moment.date()))],
            )

        return Plan(preview, run)

    return execute("log_weight", key, inputs, plan, dry_run)


# ---------------------------------------------------------------- W-2 / W-3 workouts


def create_workout(key: str, spec: dict[str, Any], schedule_date: str | None = None,
                   dry_run: bool = False) -> dict[str, Any]:
    workout, summary = workout_builder.build(spec)
    preview = f"Create workout in the Garmin library:\n{summary}"
    if schedule_date:
        day = _check_schedule_date(schedule_date)
        preview += f"\nand schedule it on {day:%a %Y-%m-%d}"

    def plan(ctx: Ctx) -> Plan:
        def run(src: Source) -> Outcome:
            resp = src.write("upload_workout", workout)
            wid = _first(resp, "workoutId")
            if not wid:
                raise Unclear(f"Garmin accepted the upload but returned no workout id: {str(resp)[:300]}")
            return Outcome(resp, "workout", str(wid), None, True,
                           resync=[("workouts", "latest"), ("workout_detail", str(wid))],
                           extra={"workout_id": wid})
        return Plan(preview, run, {"garmin_workout": workout})

    result = execute("create_workout", key, {"spec": spec, "schedule_date": schedule_date}, plan, dry_run)
    if dry_run or not schedule_date or not result.get("entity_id"):
        return result
    result["schedule"] = schedule_workout(f"{key}:schedule", int(result["entity_id"]), schedule_date)
    return result


def _check_schedule_date(value: str) -> dt.date:
    try:
        day = dt.date.fromisoformat(value[:10])
    except ValueError as e:
        raise ToolError(f"date: use YYYY-MM-DD, got {value!r}") from e
    today = dt.date.today()
    if not today - dt.timedelta(days=7) <= day <= today + dt.timedelta(days=366):
        raise ToolError(f"{day} is outside the schedulable window (last week to a year ahead).")
    return day


def _workout_name(ctx: Ctx, workout_id: int) -> str | None:
    row = ctx.conn.execute(
        "select name from garmin.entities where account_id = %s and kind = 'workout' and entity_id = %s",
        (ctx.acct_id, str(workout_id)),
    ).fetchone()
    if row:
        return row["name"]
    made = ctx.conn.execute(
        """select input->'spec'->>'name' as name from garmin.write_log
           where account_id = %s and entity_kind = 'workout' and entity_id = %s and status = 'ok'""",
        (ctx.acct_id, str(workout_id)),
    ).fetchone()
    return made["name"] if made else None


def schedule_workout(key: str, workout_id: int, date: str, dry_run: bool = False) -> dict[str, Any]:
    day = _check_schedule_date(date)

    def plan(ctx: Ctx) -> Plan:
        name = _workout_name(ctx, workout_id)
        preview = f"Schedule workout {workout_id}{f' ({name})' if name else ''} on {day:%a %Y-%m-%d}"

        def run(src: Source) -> Outcome:
            resp = src.write("schedule_workout", workout_id, day.isoformat())
            sid = _first(resp, "workoutScheduleId", "scheduleId", "id")
            if not sid:
                raise Unclear(f"Garmin accepted the schedule but returned no id: {str(resp)[:300]}")
            return Outcome(resp, "scheduled_workout", str(sid), day, True,
                           undo={"workout_id": workout_id}, resync=[("calendar_month", _month(day))],
                           extra={"scheduled_workout_id": sid, "date": day.isoformat()})
        return Plan(preview, run)

    return execute("schedule_workout", key, {"workout_id": workout_id, "date": date}, plan, dry_run)


def reschedule_workout(key: str, scheduled_workout_id: int, new_date: str, dry_run: bool = False) -> dict[str, Any]:
    day = _check_schedule_date(new_date)

    def plan(ctx: Ctx) -> Plan:
        origin = _owned(ctx, "scheduled_workout", str(scheduled_workout_id))
        old = origin["entity_date"]
        name = _workout_name(ctx, origin["undo"]["workout_id"]) if origin["undo"] else None
        preview = (f"Move scheduled workout {scheduled_workout_id}{f' ({name})' if name else ''} "
                   f"from {old:%a %Y-%m-%d} to {day:%a %Y-%m-%d}")

        def run(src: Source) -> Outcome:
            resp = src.write("reschedule_workout", scheduled_workout_id, day.isoformat())

            def after(conn: psycopg.Connection, _: int) -> None:
                conn.execute("update garmin.write_log set entity_date = %s where id = %s", (day, origin["id"]))
            return Outcome(resp, "scheduled_workout", str(scheduled_workout_id), day, False,
                           undo={"date": old.isoformat()},
                           resync=[("calendar_month", _month(old)), ("calendar_month", _month(day))], after=after)
        return Plan(preview, run)

    return execute("reschedule_workout", key, {"scheduled_workout_id": scheduled_workout_id, "new_date": new_date},
                   plan, dry_run)


def _unschedule_plan(ctx: Ctx, scheduled_workout_id: int) -> Plan:
    origin = _owned(ctx, "scheduled_workout", str(scheduled_workout_id))
    day = origin["entity_date"]
    name = _workout_name(ctx, origin["undo"]["workout_id"]) if origin["undo"] else None
    preview = (f"Remove scheduled workout {scheduled_workout_id}{f' ({name})' if name else ''} on "
               f"{day:%a %Y-%m-%d} from the calendar (the workout stays in the library)")

    def run(src: Source) -> Outcome:
        resp = src.write("unschedule_workout", scheduled_workout_id)
        return Outcome(resp, "scheduled_workout", str(scheduled_workout_id), day,
                       resync=[("calendar_month", _month(day))], after=_mark_undone(origin["id"]))
    return Plan(preview, run)


def unschedule_workout(key: str, scheduled_workout_id: int, dry_run: bool = False) -> dict[str, Any]:
    return execute("unschedule_workout", key, {"scheduled_workout_id": scheduled_workout_id},
                   lambda ctx: _unschedule_plan(ctx, scheduled_workout_id), dry_run)


def _delete_workout_plan(ctx: Ctx, workout_id: int) -> Plan:
    origin = _owned(ctx, "workout", str(workout_id))
    name = _workout_name(ctx, workout_id)
    months = [r["m"] for r in ctx.conn.execute(
        """select distinct to_char(entity_date, 'YYYY-MM') as m from garmin.write_log
           where account_id = %s and entity_kind = 'scheduled_workout' and created_by_system and undone_by is null
             and status = 'ok' and (undo->>'workout_id')::bigint = %s""",
        (ctx.acct_id, workout_id),
    ).fetchall() if r["m"]]
    preview = f"Delete workout {workout_id}{f' ({name})' if name else ''} from the Garmin library"
    if months:
        preview += " (Garmin also removes its scheduled dates)"

    def run(src: Source) -> Outcome:
        resp = src.write("delete_workout", workout_id)
        mark = _mark_undone(origin["id"])

        def after(conn: psycopg.Connection, write_id: int) -> None:
            mark(conn, write_id)
            _forget_workout_detail(conn, ctx.acct_id, workout_id)
        return Outcome(resp, "workout", str(workout_id),
                       resync=[("workouts", "latest")] + [("calendar_month", m) for m in months], after=after)
    return Plan(preview, run)


def _forget_workout_detail(conn: psycopg.Connection, acct_id: uuid.UUID, workout_id: int) -> None:
    """The workouts list replaces its own rows; the per-workout detail must be dropped explicitly."""
    with conn.transaction():
        conn.execute("delete from garmin.entities where account_id = %s and kind = 'workout_detail' and entity_id = %s",
                     (acct_id, str(workout_id)))
        for table in ("raw_objects", "work_units"):
            conn.execute(f"delete from garmin.{table} where account_id = %s and domain = 'workout_detail' "  # noqa: S608
                         "and scope_key = %s", (acct_id, str(workout_id)))


def delete_workout(key: str, workout_id: int, dry_run: bool = False) -> dict[str, Any]:
    return execute("delete_workout", key, {"workout_id": workout_id},
                   lambda ctx: _delete_workout_plan(ctx, workout_id), dry_run)


# ---------------------------------------------------------------- W-4 hydration, W-5 blood pressure


def log_hydration(key: str, ml: float, date: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    def plan(ctx: Ctx) -> Plan:
        amount = _num(ml, "ml", -5000, 5000)
        if amount == 0:
            raise ToolError("ml: must not be 0")
        day = _day(date, ctx)
        if day > ctx.now().date():
            raise ToolError(f"{day} is in the future.")
        row = ctx.conn.execute(
            "select hydration_ml from garmin.daily_summary where account_id = %s and calendar_date = %s",
            (ctx.acct_id, day),
        ).fetchone()
        current = float(row["hydration_ml"]) if row and row.get("hydration_ml") is not None else None
        verb = "Add" if amount > 0 else "Subtract"
        preview = f"{verb} {abs(amount):g} ml of water on {day:%a %Y-%m-%d}"
        if current is not None:
            preview += f" (synced total so far {current:g} ml → {current + amount:g} ml)"

        def run(src: Source) -> Outcome:
            now = ctx.now()
            stamp = dt.datetime.combine(day, now.time() if day == now.date() else dt.time(12, 0))
            resp = src.write("add_hydration_data", amount, stamp.strftime("%Y-%m-%dT%H:%M:%S.000"), day.isoformat())
            return Outcome(resp, "hydration", None, day, True, undo={"ml": -amount, "date": day.isoformat()},
                           resync=[("hydration", day.isoformat())])
        return Plan(preview, run)

    return execute("log_hydration", key, {"ml": ml, "date": date}, plan, dry_run)


def _find_bp(src: Source, day: dt.date, local: dt.datetime, sys_: int, dia: int) -> dict[str, Any] | None:
    data = src.call("get_blood_pressure", day.isoformat(), day.isoformat()) or {}
    stamp = local.strftime("%Y-%m-%dT%H:%M")
    for summary in data.get("measurementSummaries") or []:
        for m in summary.get("measurements") or []:
            if (m.get("systolic") == sys_ and m.get("diastolic") == dia
                    and str(m.get("measurementTimestampLocal", "")).startswith(stamp)):
                return m
    return None


def log_blood_pressure(key: str, systolic: int, diastolic: int, pulse: int | None = None, date: str | None = None,
                       time: str | None = None, notes: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    def plan(ctx: Ctx) -> Plan:
        for name, value, lo, hi in (("systolic", systolic, 70, 260), ("diastolic", diastolic, 40, 150)):
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ToolError(f"{name}: a whole number {lo}-{hi}, got {value!r}")
        if pulse is not None and (isinstance(pulse, bool) or not isinstance(pulse, int) or not 20 <= pulse <= 250):
            raise ToolError(f"pulse: a whole number 20-250, got {pulse!r}")
        if diastolic >= systolic:
            raise ToolError("diastolic must be lower than systolic")
        moment = _moment(date, time, ctx)
        preview = (f"Blood pressure {systolic}/{diastolic}{f', pulse {pulse}' if pulse else ''} at "
                   f"{_fmt_local(moment)}{f' — note: {notes}' if notes else ''}")

        def run(src: Source) -> Outcome:
            resp = src.write("set_blood_pressure", systolic, diastolic, pulse, moment.isoformat(), notes or "")
            found = _find_bp(src, moment.date(), moment, systolic, diastolic)
            version = _first(resp, "version") or (found or {}).get("version")
            return Outcome(resp, "blood_pressure", str(version) if version else None, moment.date(), True,
                           undo={"date": moment.date().isoformat(), "local": moment.strftime("%Y-%m-%dT%H:%M"),
                                 "systolic": systolic, "diastolic": diastolic},
                           resync=[("blood_pressure", _month(moment.date()))],
                           extra={"garmin_measurement": found} if found else {})
        return Plan(preview, run)

    return execute("log_blood_pressure", key,
                   {"systolic": systolic, "diastolic": diastolic, "pulse": pulse, "date": date, "time": time,
                    "notes": notes}, plan, dry_run)


# ---------------------------------------------------------------- W-6 activity edits, W-7 manual activities


def _activity_type(ctx: Ctx, type_key: str) -> dict[str, Any]:
    types = _raw_snapshot(ctx, "s_activity_types") or []
    key = type_key.strip().lower()
    for t in types:
        if isinstance(t, dict) and t.get("typeKey") == key:
            return t
    keys = [t.get("typeKey") for t in types if isinstance(t, dict) and t.get("typeKey")]
    import difflib  # noqa: PLC0415 - only needed on the error path
    close = difflib.get_close_matches(key, keys, n=6, cutoff=0.5)
    raise ToolError(f"Unknown activity type {type_key!r}." + (f" Did you mean: {', '.join(close)}?" if close else ""))


def _gear_items(ctx: Ctx) -> list[dict[str, Any]]:
    rows = ctx.conn.execute("select data from garmin.entities where account_id = %s and kind = 'gear'",
                            (ctx.acct_id,)).fetchall()
    return [r["data"] for r in rows]


def _gear(ctx: Ctx, gear_uuid: str) -> dict[str, Any]:
    for g in _gear_items(ctx):
        if g.get("uuid") == gear_uuid:
            return g
    names = ", ".join(f"{g.get('brand', '')} {g.get('model', '')} ({g.get('uuid')})".strip() for g in _gear_items(ctx))
    raise ToolError(f"No gear with uuid {gear_uuid!r}. Known gear: {names or 'none'}.")


def _gear_label(g: dict[str, Any]) -> str:
    return (g.get("name") or f"{g.get('brand', '')} {g.get('model', '')}").strip()


RPE_FEEL = {0: "very weak", 25: "weak", 50: "normal", 75: "strong", 100: "very strong"}


def update_activity(key: str, activity_id: int, name: str | None = None, description: str | None = None,
                    activity_type: str | None = None, rpe: int | None = None, feel: int | None = None,
                    add_gear_uuid: str | None = None, remove_gear_uuid: str | None = None,
                    dry_run: bool = False) -> dict[str, Any]:
    inputs = locals().copy()
    inputs.pop("key")
    inputs.pop("dry_run")

    def plan(ctx: Ctx) -> Plan:
        act = ctx.conn.execute(
            """select activity_id, name, description, type_key, start_local, rpe, feel, gear
               from garmin.activities where account_id = %s and activity_id = %s""",
            (ctx.acct_id, activity_id),
        ).fetchone()
        if not act:
            raise ToolError(f"Activity {activity_id} isn't in the database (yet). Check list_activities.")
        fields: dict[str, Any] = {}
        undo: dict[str, Any] = {}
        changes: list[str] = []
        if name is not None:
            if not name.strip() or len(name) > 255:
                raise ToolError("name: 1-255 characters")
            fields["activityName"] = name.strip()
            undo["activityName"] = act["name"]
            changes.append(f"name {act['name']!r} → {name.strip()!r}")
        if description is not None:
            if len(description) > 4000:
                raise ToolError("description: at most 4000 characters")
            fields["description"] = description
            undo["description"] = act["description"] or ""
            changes.append(f"description → {description!r}" + (" (replaces the current one)" if act["description"]
                                                                  else ""))
        if activity_type is not None:
            t = _activity_type(ctx, activity_type)
            fields["activityTypeDTO"] = {"typeId": t["typeId"], "typeKey": t["typeKey"],
                                         "parentTypeId": t.get("parentTypeId")}
            if act["type_key"]:
                try:
                    old = _activity_type(ctx, act["type_key"])
                    undo["activityTypeDTO"] = {"typeId": old["typeId"], "typeKey": old["typeKey"],
                                               "parentTypeId": old.get("parentTypeId")}
                except ToolError:
                    pass  # an obsolete type key: the change just can't be undone automatically
            changes.append(f"type {act['type_key']} → {t['typeKey']}")
        summary: dict[str, Any] = {}
        if rpe is not None:
            if isinstance(rpe, bool) or not isinstance(rpe, int) or not 1 <= rpe <= 10:
                raise ToolError("rpe: a whole number 1-10")
            summary["directWorkoutRpe"] = rpe * 10  # Garmin stores RPE × 10
            changes.append(f"RPE {act['rpe'] // 10 if act['rpe'] else '–'} → {rpe}")
        if feel is not None:
            if feel not in RPE_FEEL:
                raise ToolError("feel: 0 (very weak), 25, 50 (normal), 75 or 100 (very strong)")
            summary["directWorkoutFeel"] = feel
            changes.append(f"feel → {RPE_FEEL[feel]}")
        if summary:
            fields["summaryDTO"] = summary
            undo["summaryDTO"] = {"directWorkoutRpe": act["rpe"], "directWorkoutFeel": act["feel"]}
        gear_ops: list[tuple[str, str]] = []
        for op, uuid_ in (("remove", remove_gear_uuid), ("add", add_gear_uuid)):
            if uuid_:
                g = _gear(ctx, uuid_)
                gear_ops.append((op, uuid_))
                changes.append(f"{op} gear {_gear_label(g)}")
        if not changes:
            raise ToolError("Nothing to change: give name, description, activity_type, rpe, feel or a gear uuid.")
        undo["gear"] = [("add" if op == "remove" else "remove", u) for op, u in reversed(gear_ops)]
        when = f"{act['start_local']:%a %Y-%m-%d %H:%M}" if act["start_local"] else ""
        preview = f"Update activity {activity_id} ({act['name']}, {when}): " + "; ".join(changes)

        def run(src: Source) -> Outcome:
            responses: dict[str, Any] = {}
            if fields:
                responses["activity"] = src.write("update_activity", activity_id, fields)
            for op, uuid_ in gear_ops:
                method = "add_gear_to_activity" if op == "add" else "remove_gear_from_activity"
                responses[f"{op}_gear"] = src.write(method, uuid_, activity_id)
            aid = str(activity_id)
            resync = [("activity_summary", aid), ("activity_index", "recent")]
            if gear_ops:
                resync += [("activity_gear", aid), ("gear", "latest")]
            return Outcome(responses, "activity", aid, act["start_local"].date() if act["start_local"] else None,
                           False, undo=undo, resync=resync)
        return Plan(preview, run)

    return execute("update_activity", key, inputs, plan, dry_run)


def create_manual_activity(key: str, activity_type: str, start: str, duration_min: float, name: str,
                           distance_km: float | None = None, description: str | None = None,
                           dry_run: bool = False) -> dict[str, Any]:
    inputs = locals().copy()
    inputs.pop("key")
    inputs.pop("dry_run")

    def plan(ctx: Ctx) -> Plan:
        t = _activity_type(ctx, activity_type)
        try:
            local = dt.datetime.fromisoformat(start.strip()).replace(tzinfo=None, second=0, microsecond=0)
        except ValueError as e:
            raise ToolError(f"start: local 'YYYY-MM-DDTHH:MM', got {start!r}") from e
        if local.replace(tzinfo=ctx.tz) > ctx.now():
            raise ToolError("start is in the future.")
        minutes = _num(duration_min, "duration_min", 1, 24 * 60)
        km = _num(distance_km, "distance_km", 0, 1000) if distance_km is not None else 0.0
        if not name.strip():
            raise ToolError("name: required")
        payload = {
            "activityTypeDTO": {"typeId": t["typeId"], "typeKey": t["typeKey"], "parentTypeId": t.get("parentTypeId")},
            "accessControlRuleDTO": {"typeId": 2, "typeKey": "private"},
            "timeZoneUnitDTO": {"unitKey": ctx.tz.key},
            "activityName": name.strip()[:255],
            "metadataDTO": {"autoCalcCalories": True},
            "summaryDTO": {"startTimeLocal": local.strftime("%Y-%m-%dT%H:%M:%S.000"), "distance": km * 1000,
                           "duration": minutes * 60},
        }
        if description:
            payload["description"] = description[:4000]
        preview = (f"Create manual {t['typeKey']} activity {name.strip()!r} on {local:%a %Y-%m-%d %H:%M}, "
                   f"{minutes:g} min{f', {km:g} km' if km else ''} (private, calories auto-calculated)")

        def run(src: Source) -> Outcome:
            resp = src.write("create_manual_activity_from_json", payload)
            aid = _first(resp, "activityId")
            if not aid:
                raise Unclear(f"Garmin accepted the activity but returned no id: {str(resp)[:300]}")
            return Outcome(resp, "activity", str(aid), local.date(), True,
                           resync=[("activity_index", "recent")], extra={"activity_id": aid})
        return Plan(preview, run)

    return execute("create_manual_activity", key, inputs, plan, dry_run)


# ---------------------------------------------------------------- W-8 gear


GEAR_ACTIONS = {"create", "retire", "activate", "set_defaults"}


def manage_gear(key: str, action: str, gear_uuid: str | None = None, brand: str | None = None,
                model: str | None = None, name: str | None = None, gear_type: str = "SHOES",
                first_use_date: str | None = None, max_distance_km: float | None = None,
                default_for: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    inputs = locals().copy()
    inputs.pop("key")
    inputs.pop("dry_run")
    if action not in GEAR_ACTIONS:
        raise ToolError(f"action: one of {', '.join(sorted(GEAR_ACTIONS))}")

    def plan(ctx: Ctx) -> Plan:
        types = [_activity_type(ctx, k)["typeKey"] for k in (default_for or [])]
        if action == "create":
            if not brand or not model:
                raise ToolError("create: brand and model are required")
            first = _day(first_use_date, ctx, "first_use_date")
            label = name or f"{brand} {model}"
            preview = f"Add gear {label!r} ({gear_type.upper()}: {brand} {model}), first used {first}"
            if max_distance_km:
                preview += f", retire at {max_distance_km:g} km"
            if types:
                preview += ", default for " + ", ".join(types)

            def run(src: Source) -> Outcome:
                resp = src.write("create_gear", gear_type.upper(), brand, model, label, first.isoformat(),
                                 max_usage_distance_km=max_distance_km, activity_type_keys=types or None)
                gid = _first(resp, "uuid")
                return Outcome(resp, "gear", str(gid) if gid else None, first, True, resync=[("gear", "latest")],
                               extra={"gear_uuid": gid})
            return Plan(preview, run)

        if not gear_uuid:
            raise ToolError(f"{action}: gear_uuid is required (see get_entities kind='gear')")
        g = _gear(ctx, gear_uuid)
        if action in ("retire", "activate"):
            status = "RETIRED" if action == "retire" else "ACTIVE"
            if g.get("status") == status:
                raise ToolError(f"{_gear_label(g)} is already {status.lower()}.")
            preview = f"{action.capitalize()} gear {_gear_label(g)} (now {g.get('status', '?').lower()})"

            def run(src: Source) -> Outcome:
                resp = src.write("set_gear_status", gear_uuid, status)
                return Outcome(resp, "gear", gear_uuid, undo={"status": g.get("status")},
                               resync=[("gear", "latest")])
            return Plan(preview, run)

        preview = (f"Make {_gear_label(g)} the default gear for: {', '.join(types)}" if types
                   else f"Remove {_gear_label(g)} as default gear for all activity types")

        def run(src: Source) -> Outcome:
            resp = src.write("update_gear", gear_uuid, activity_type_keys=types)
            return Outcome(resp, "gear", gear_uuid, resync=[("gear", "latest")])
        return Plan(preview, run)

    return execute("manage_gear", key, inputs, plan, dry_run)


# ---------------------------------------------------------------- undo


ACTIVITY_TABLES = ["activities", "activity_laps", "activity_zones", "activity_exercise_sets", "activity_weather",
                   "activity_files"]


def _forget_activity(conn: psycopg.Connection, acct_id: uuid.UUID, activity_id: int) -> None:
    """Drop a deleted activity's rows, raw payloads and units (the index doesn't see deletions)."""
    with conn.transaction():
        for table in ACTIVITY_TABLES:
            conn.execute(f"delete from garmin.{table} where account_id = %s and activity_id = %s",  # noqa: S608
                         (acct_id, activity_id))
        domains = [d.name for d in activity_domains()]
        for table in ("raw_objects", "work_units"):
            conn.execute(f"delete from garmin.{table} where account_id = %s and domain = any(%s) and scope_key = %s",  # noqa: S608
                         (acct_id, domains, str(activity_id)))


UNDOABLE = {"log_weight", "log_hydration", "log_blood_pressure", "create_workout", "schedule_workout",
            "create_manual_activity", "update_activity"}


def undo_write(key: str, write_id: int, dry_run: bool = False) -> dict[str, Any]:
    """Reverse a write this system made: delete what it created, or restore what it changed."""

    def plan(ctx: Ctx) -> Plan:
        w = ctx.conn.execute("select * from garmin.write_log where account_id = %s and id = %s",
                             (ctx.acct_id, write_id)).fetchone()
        if not w or w["status"] != "ok":
            raise ToolError(f"Write {write_id} doesn't exist or didn't succeed.")
        if w["tool"] not in UNDOABLE:
            raise ToolError(f"{w['tool']} writes can't be undone here; change it back with the matching tool.")
        if w["undone_by"]:
            raise ToolError(f"Write {write_id} was already undone by write {w['undone_by']}.")
        u = w["undo"] or {}
        what = f"write {write_id} ({w['preview']})"

        if w["tool"] == "schedule_workout":
            return _unschedule_plan(ctx, int(w["entity_id"]))
        if w["tool"] == "create_workout":
            return _delete_workout_plan(ctx, int(w["entity_id"]))

        if w["tool"] == "log_hydration":
            day = dt.date.fromisoformat(u["date"])

            def run(src: Source) -> Outcome:
                resp = src.write("add_hydration_data", u["ml"], f"{u['date']}T12:00:00.000", u["date"])
                return Outcome(resp, "hydration", None, day, resync=[("hydration", u["date"])],
                               after=_mark_undone(w["id"]))
            return Plan(f"Undo {what}: log {u['ml']:+g} ml on {u['date']}", run)

        if w["tool"] == "log_weight":
            day = dt.date.fromisoformat(u["date"])

            def run(src: Source) -> Outcome:
                pk = w["entity_id"] or (_find_weigh_in(src, day, u["gmt_ms"], u["weight_kg"]) or {}).get("samplePk")
                if not pk:
                    raise ToolError("Couldn't find that weigh-in in Garmin any more; nothing deleted.")
                resp = src.write("delete_weigh_in", str(pk), u["date"])
                return Outcome(resp, "weigh_in", str(pk), day, resync=[("body_composition", _month(day))],
                               after=_mark_undone(w["id"]))
            return Plan(f"Undo {what}: delete that weigh-in", run)

        if w["tool"] == "log_blood_pressure":
            day = dt.date.fromisoformat(u["date"])

            def run(src: Source) -> Outcome:
                version = w["entity_id"]
                if not version:
                    local = dt.datetime.fromisoformat(u["local"])
                    version = (_find_bp(src, day, local, u["systolic"], u["diastolic"]) or {}).get("version")
                if not version:
                    raise ToolError("Couldn't find that reading in Garmin any more; nothing deleted.")
                resp = src.write("delete_blood_pressure", str(version), u["date"])
                return Outcome(resp, "blood_pressure", str(version), day, resync=[("blood_pressure", _month(day))],
                               after=_mark_undone(w["id"]))
            return Plan(f"Undo {what}: delete that reading", run)

        if w["tool"] == "create_manual_activity":
            aid = w["entity_id"]
            day = w["entity_date"]

            def run(src: Source) -> Outcome:
                resp = src.write("delete_activity", aid)
                mark = _mark_undone(w["id"])

                def after(conn: psycopg.Connection, write_id: int) -> None:
                    mark(conn, write_id)
                    _forget_activity(conn, ctx.acct_id, int(aid))
                resync = [("calendar_month", _month(day))] if day else []
                return Outcome(resp, "activity", aid, resync=resync, after=after)
            return Plan(f"Undo {what}: delete manual activity {aid}", run)

        # update_activity: restore the previous values
        aid = w["entity_id"]
        fields = {k: v for k, v in u.items() if k != "gear"}  # summaryDTO nulls clear RPE / feel

        def run(src: Source) -> Outcome:
            responses: dict[str, Any] = {}
            if fields:
                responses["activity"] = src.write("update_activity", int(aid), fields)
            for op, gear_uuid in u.get("gear", []):
                method = "add_gear_to_activity" if op == "add" else "remove_gear_from_activity"
                responses[f"{op}_gear"] = src.write(method, gear_uuid, int(aid))
            return Outcome(responses, "activity", aid,
                           resync=[("activity_summary", aid), ("activity_index", "recent"), ("activity_gear", aid)],
                           after=_mark_undone(w["id"]))
        return Plan(f"Undo {what}: restore the previous values", run)

    return execute("undo_write", key, {"write_id": write_id}, plan, dry_run)


def recent_writes(limit: int = 20) -> dict[str, Any]:
    """The write log, newest first (for 'what did you change?' questions)."""
    with connect() as conn:
        acct = _account(conn)
        rows = conn.execute(
            """select id, created_at, tool, status, preview, error, entity_kind, entity_id, entity_date,
                      created_by_system, undone_by
               from garmin.write_log where account_id = %s order by id desc limit %s""",
            (acct["id"], max(1, min(limit, 100))),
        ).fetchall()
    return {"writes": [{k: v for k, v in r.items() if v is not None} for r in rows]}

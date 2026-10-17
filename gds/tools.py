"""Read tools for the Claude connector (design §8.2).

Every query runs as the read-only `gds_reader` role inside a read-only transaction with a
statement timeout, scoped to this deployment's account. Results are compact dicts with
units in field names.
"""

import datetime as dt
import io
import re
import zipfile
from decimal import Decimal
from typing import Any

import psycopg
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError

from gds.config import settings
from gds.db import connect

MAX_ROWS = 500
MAX_DAYS = 400
STATEMENT_TIMEOUT = "5s"


class ToolError(MCPToolError, ValueError):
    """An anticipated failure (bad arguments, missing data); the message is shown to Claude
    as a tool error instead of being logged as a crash."""


# ---------------------------------------------------------------- plumbing


def _account_id(conn: psycopg.Connection) -> Any:
    row = conn.execute("select id from garmin.accounts where label = %s", (settings().account_label,)).fetchone()
    if not row:
        raise ToolError("No Garmin account is configured for this deployment.")
    return row["id"]


def read(sql: str, params: dict[str, Any] | None = None, limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """Run a query as gds_reader (read-only, 5 s timeout). `%(account)s` is bound to the account."""
    with connect() as conn:
        conn.execute("set transaction read only")
        acct = _account_id(conn)
        conn.execute("set local role gds_reader")
        conn.execute(f"set local statement_timeout = '{STATEMENT_TIMEOUT}'")
        cur = conn.execute(sql, {"account": acct, **(params or {})})
        rows = cur.fetchmany(limit + 1) if cur.description else []
    if len(rows) > limit:
        rows = rows[:limit]
        rows.append({"_truncated": f"more than {limit} rows; narrow the query"})
    return [_clean(r) for r in rows]


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    """Drop the account id and nulls; Postgres numerics (Decimal) become floats."""
    row.pop("account_id", None)
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items() if v is not None}


def _compact(obj: Any) -> Any:
    """Recursively drop None/empty values from a JSON document (Garmin payloads are mostly nulls)."""
    if isinstance(obj, dict):
        out = {k: _compact(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v not in (None, [], {}, "")}
    if isinstance(obj, list):
        return [_compact(v) for v in obj]
    return obj


def parse_day(value: str | None, default: dt.date) -> dt.date:
    if not value:
        return default
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError as e:
        raise ToolError(f"Dates must be YYYY-MM-DD, got {value!r}") from e


def date_range(start: str | None, end: str | None, default_days: int = 7,
               max_days: int = MAX_DAYS) -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    e = parse_day(end, today)
    s = parse_day(start, e - dt.timedelta(days=default_days - 1))
    if s > e:
        raise ToolError("start must be on or before end")
    if (e - s).days > max_days:
        raise ToolError(f"At most {max_days} days per call; use get_trend for longer periods.")
    return s, e


# ---------------------------------------------------------------- wellness


def daily_wellness(start: str | None, end: str | None, fields: list[str] | None) -> dict[str, Any]:
    s, e = date_range(start, end)
    rows = read("""select * from garmin.v_daily_wellness
                   where account_id = %(account)s and date between %(s)s and %(e)s order by date""",
                {"s": s, "e": e})
    if fields:
        keep = set(fields) | {"date"}
        rows = [{k: v for k, v in r.items() if k in keep} for r in rows]
    return {"start": s, "end": e, "days": rows}


def sleep(start: str | None, end: str | None) -> dict[str, Any]:
    s, e = date_range(start, end)
    nights = read("""select * from garmin.sleep
                     where account_id = %(account)s and calendar_date between %(s)s and %(e)s
                     order by calendar_date""", {"s": s, "e": e})
    stages = read("""select calendar_date, ts_local, value from garmin.v_intraday_points
                     where account_id = %(account)s and metric = 'sleep_stage'
                       and calendar_date between %(s)s and %(e)s order by ts_local""", {"s": s, "e": e},
                  limit=5000)
    names = {0: "deep", 1: "light", 2: "rem", 3: "awake"}
    by_day: dict[Any, list] = {}
    for p in stages:
        by_day.setdefault(p["calendar_date"], []).append([p["ts_local"].strftime("%H:%M"),
                                                          names.get(int(p["value"]), p["value"])])
    for n in nights:
        n["stage_timeline_local"] = by_day.get(n["calendar_date"], [])
    return {"nights": nights, "note": "stage_timeline_local = [start HH:MM, stage] segments"}


INTRADAY_METRICS = {"hr", "stress", "body_battery", "respiration", "spo2", "steps", "floors", "hrv", "sleep_stage"}


def intraday(metric: str, date: str, bucket_minutes: int) -> dict[str, Any]:
    if metric not in INTRADAY_METRICS:
        raise ToolError(f"metric must be one of {sorted(INTRADAY_METRICS)}")
    if not 1 <= bucket_minutes <= 240:
        raise ToolError("bucket_minutes must be between 1 and 240")
    day = parse_day(date, dt.date.today())
    rows = read("""select bucket_local, value, samples
                   from garmin.intraday_bucketed(%(account)s, %(metric)s, %(day)s, %(bucket)s)""",
                {"metric": metric, "day": day, "bucket": bucket_minutes}, limit=1440)
    return {
        "metric": metric,
        "date": day,
        "bucket_minutes": bucket_minutes,
        "aggregation": "sum" if metric in ("steps", "floors") else "average",
        "points": [[r["bucket_local"].strftime("%H:%M"), round(r["value"], 1)] for r in rows if "value" in r],
    }


# ---------------------------------------------------------------- activities


def list_activities(start: str | None, end: str | None, activity_type: str | None, limit: int) -> dict[str, Any]:
    s, e = date_range(start, end, default_days=30)
    rows = read("""select * from garmin.v_activities
                   where account_id = %(account)s and start_local::date between %(s)s and %(e)s
                     and (%(type)s::text is null or type_key ilike '%%' || %(type)s || '%%')
                   order by start_utc desc""",
                {"s": s, "e": e, "type": activity_type}, limit=max(1, min(limit, 200)))
    return {"start": s, "end": e, "activities": rows}


ACTIVITY_PARTS = {"laps", "zones", "sets", "weather"}


def activity(activity_id: int, include: list[str] | None) -> dict[str, Any]:
    parts = set(include or ACTIVITY_PARTS)
    if parts - ACTIVITY_PARTS:
        raise ToolError(f"include must be a subset of {sorted(ACTIVITY_PARTS)}")
    params = {"id": activity_id}
    found = read("select * from garmin.activities where account_id = %(account)s and activity_id = %(id)s", params)
    if not found:
        raise ToolError(f"No activity {activity_id}")
    out: dict[str, Any] = {"activity": found[0]}
    if "laps" in parts:
        out["laps"] = read("""select * from garmin.activity_laps where account_id = %(account)s
                              and activity_id = %(id)s order by lap_index""", params)
    if "zones" in parts:
        out["zones"] = read("""select zone_type, zone, low_boundary, seconds from garmin.activity_zones
                               where account_id = %(account)s and activity_id = %(id)s
                               order by zone_type, zone""", params)
    if "sets" in parts:
        out["exercise_sets"] = read("""select set_index, set_type, duration_s, reps, weight_raw, category,
                                              exercise_name, probability
                                       from garmin.activity_exercise_sets
                                       where account_id = %(account)s and activity_id = %(id)s
                                       order by set_index""", params)
    if "weather" in parts:
        w = read("""select * from garmin.activity_weather where account_id = %(account)s
                    and activity_id = %(id)s""", params)
        out["weather"] = w[0] if w else None
    return out


STREAM_FIELDS = {
    "heart_rate": "heart_rate_bpm",
    "enhanced_speed": "speed_mps",
    "power": "power_w",
    "enhanced_altitude": "altitude_m",
    "distance": "distance_m",
    "cadence": "cadence",
    "vertical_oscillation": "vertical_oscillation_mm",
    "stance_time": "ground_contact_ms",
    "step_length": "step_length_mm",
    "temperature": "temperature_c",
}


def activity_streams(activity_id: int, fields: list[str] | None, max_points: int) -> dict[str, Any]:
    """Per-second samples parsed from the stored FIT file, downsampled to max_points."""
    import fitdecode

    wanted = {k: v for k, v in STREAM_FIELDS.items() if not fields or v in fields or k in fields}
    if not wanted:
        raise ToolError(f"fields must be among {sorted(STREAM_FIELDS.values())}")
    with connect() as conn:  # FIT bytes aren't granted to the reader role; this query is fixed
        acct = _account_id(conn)
        row = conn.execute(
            """select f.content, a.type_key from garmin.activity_files f
               join garmin.activities a using (account_id, activity_id)
               where f.account_id = %s and f.activity_id = %s and f.format = 'original_zip'""",
            (acct, activity_id),
        ).fetchone()
    if not row:
        raise ToolError(f"No FIT file stored for activity {activity_id}")
    with zipfile.ZipFile(io.BytesIO(bytes(row["content"]))) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".fit")), None)
        data = z.read(name) if name else None
    if not data:
        raise ToolError("The stored file contains no FIT data")

    running = "run" in (row["type_key"] or "")
    samples: list[dict[str, Any]] = []
    start = None
    with fitdecode.FitReader(io.BytesIO(data)) as reader:
        for frame in reader:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA or frame.name != "record":
                continue
            ts = frame.get_value("timestamp", fallback=None)
            if ts is None:
                continue
            start = start or ts
            s: dict[str, Any] = {"t_s": int((ts - start).total_seconds())}
            for src, out in wanted.items():
                v = frame.get_value(src, fallback=None)
                if src == "cadence" and v is not None and running:
                    # Running cadence in FIT is per leg: steps/min = 2 × (cadence + fractional).
                    v = 2 * (v + (frame.get_value("fractional_cadence", fallback=0) or 0))
                if isinstance(v, (int, float)):
                    s[out] = round(float(v), 2)
            samples.append(s)
    step = max(1, -(-len(samples) // max(10, min(max_points, 5000))))
    return {
        "activity_id": activity_id,
        "samples_total": len(samples),
        "every_nth": step,
        "cadence_unit": "steps/min" if running else "rpm",
        "samples": samples[::step],
    }


# ---------------------------------------------------------------- trends, body, performance


TRENDS: dict[str, tuple[str, str, str, str]] = {
    # metric: (source, date column, value expression, default aggregation)
    "resting_hr": ("garmin.v_daily_wellness", "date", "resting_hr", "avg"),
    "hrv": ("garmin.v_daily_wellness", "date", "avg_overnight_hrv", "avg"),
    "sleep_score": ("garmin.v_daily_wellness", "date", "sleep_score", "avg"),
    "sleep_hours": ("garmin.v_daily_wellness", "date", "sleep_seconds / 3600.0", "avg"),
    "readiness": ("garmin.v_daily_wellness", "date", "readiness_score", "avg"),
    "steps": ("garmin.v_daily_wellness", "date", "steps", "avg"),
    "stress": ("garmin.v_daily_wellness", "date", "avg_stress", "avg"),
    "body_battery_high": ("garmin.v_daily_wellness", "date", "body_battery_high", "avg"),
    "acute_load": ("garmin.v_daily_wellness", "date", "acute_load", "avg"),
    "chronic_load": ("garmin.v_daily_wellness", "date", "chronic_load", "avg"),
    "vo2max": ("garmin.v_daily_wellness", "date", "vo2max", "max"),
    "training_load": ("garmin.v_activities", "start_local::date", "training_load", "sum"),
    "activity_minutes": ("garmin.v_activities", "start_local::date", "duration_min", "sum"),
    "running_km": ("garmin.v_activities", "start_local::date",
                   "case when type_key like '%%running%%' then distance_km end", "sum"),
    "weight_kg": ("garmin.v_body", "date", "weight_kg", "avg"),
}
PERIODS = {"day", "week", "month"}
AGGS = {"avg", "min", "max", "sum", "count"}


def trend(metric: str, start: str | None, end: str | None, period: str, agg: str | None) -> dict[str, Any]:
    if metric not in TRENDS:
        raise ToolError(f"metric must be one of {sorted(TRENDS)}")
    if period not in PERIODS:
        raise ToolError(f"period must be one of {sorted(PERIODS)}")
    source, date_col, expr, default_agg = TRENDS[metric]
    agg = agg or default_agg
    if agg not in AGGS:
        raise ToolError(f"agg must be one of {sorted(AGGS)}")
    today = dt.date.today()
    e = parse_day(end, today)
    s = parse_day(start, e - dt.timedelta(days=90))
    # Identifiers come from the whitelists above, never from the caller.
    rows = read(
        f"""select date_trunc('{period}', {date_col})::date as period_start,
                   round(({agg}({expr}))::numeric, 2) as value, count({expr}) as n
            from {source}
            where account_id = %(account)s and {date_col} between %(s)s and %(e)s
            group by 1 order by 1""",
        {"s": s, "e": e},
    )
    return {"metric": metric, "period": period, "agg": agg, "start": s, "end": e, "points": rows}


def body(start: str | None, end: str | None) -> dict[str, Any]:
    s, e = date_range(start, end, default_days=365, max_days=3650)  # sparse data: allow long ranges
    return {"measurements": read("""select * from garmin.v_body where account_id = %(account)s
                                    and date between %(s)s and %(e)s order by measured_utc""",
                                 {"s": s, "e": e})}


def performance(metric: str | None, start: str | None, end: str | None) -> dict[str, Any]:
    if not metric:
        return {"latest": read("""select metric, value, calendar_date from garmin.v_latest_performance
                                  where account_id = %(account)s order by metric""")}
    s, e = date_range(start, end, default_days=365)
    return {"metric": metric, "history": read(
        """select calendar_date, value, detail from garmin.performance_metrics
           where account_id = %(account)s and metric = %(metric)s and calendar_date between %(s)s and %(e)s
           order by calendar_date""", {"metric": metric, "s": s, "e": e})}


ENTITY_KINDS = {"device", "gear", "gear_stats", "personal_record", "goal", "badge", "workout", "workout_detail",
                "training_plan", "course", "profile", "profile_settings", "hr_zones", "power_zones", "event"}


LARGE_KINDS = {"workout_detail", "badge", "workout", "training_plan", "course", "event"}


def entities(kind: str, entity_id: str | None) -> dict[str, Any]:
    if kind not in ENTITY_KINDS:
        raise ToolError(f"kind must be one of {sorted(ENTITY_KINDS)}")
    rows = read("""select entity_id, name, data, updated_at from garmin.entities
                   where account_id = %(account)s and kind = %(kind)s
                     and (%(eid)s::text is null or entity_id = %(eid)s) order by name nulls last, entity_id""",
                {"kind": kind, "eid": entity_id}, limit=100)
    if entity_id is None and kind in LARGE_KINDS and len(rows) > 1:
        # Listing: ids and names only; ask for one entity_id to get its full document.
        return {"kind": kind, "note": "pass entity_id for the full document",
                "items": [{k: r.get(k) for k in ("entity_id", "name", "updated_at")} for r in rows]}
    for r in rows:
        r["data"] = _compact(r.get("data"))
    return {"kind": kind, "items": rows}


def calendar_items(start: str | None, end: str | None, item_type: str | None) -> dict[str, Any]:
    today = dt.date.today()
    s = parse_day(start, today - dt.timedelta(days=7))
    e = parse_day(end, today + dt.timedelta(days=21))
    return {"items": read(
        """select item_date, item_type, title, sport_type_key, duration_s, distance_m, workout_id, item_id
           from garmin.calendar_items
           where account_id = %(account)s and item_date between %(s)s and %(e)s
             and (%(type)s::text is null or item_type = %(type)s)
           order by item_date, item_type""", {"s": s, "e": e, "type": item_type})}


# ---------------------------------------------------------------- SQL escape hatch

QUERYABLE = [
    ("v_daily_wellness", "one row per day: readiness, sleep, HRV, RHR, stress, Body Battery, steps, load"),
    ("v_activities", "activities with pace, HR, training effect/load, RPE, weather, gear"),
    ("v_body", "weigh-ins and blood pressure"),
    ("v_latest_performance", "latest value per performance metric"),
    ("v_intraday_points", "intraday samples (metric, ts_utc, ts_local, value); filter by metric + calendar_date"),
    ("v_sync_health", "sync status and backfill progress"),
    ("v_domain_coverage", "per-domain sync coverage"),
    ("daily_summary", "daily totals and stress/Body Battery details"),
    ("sleep", "nightly sleep detail"),
    ("hrv", "nightly HRV"),
    ("training_readiness", "every intraday readiness reading (is_morning flags the wake-up one)"),
    ("training_status", "daily training status, load, ACWR, load balance, VO2max"),
    ("activities", "full activity summaries"),
    ("activity_laps", "per-lap metrics"),
    ("activity_zones", "time in HR/power zones per activity"),
    ("activity_exercise_sets", "strength sets"),
    ("activity_weather", "weather per outdoor activity (°F as reported, temp_c derived)"),
    ("weigh_ins", "weight and body composition"),
    ("blood_pressure", "blood pressure readings"),
    ("performance_metrics", "dated scalar metrics (race predictions, LT, FTP, fitness age, VO2max history...)"),
    ("body_battery_events", "Body Battery events (sleep, activities, stress)"),
    ("calendar_items", "Garmin calendar: activities, naps, scheduled workouts"),
    ("entities", "devices, gear, PRs, goals, badges, workouts, plans, profile (jsonb data)"),
    ("intraday_series", "raw intraday arrays (ts_offsets_s, vals); prefer v_intraday_points"),
]


def describe_schema() -> dict[str, Any]:
    cols = read("""select table_name, column_name, data_type from information_schema.columns
                   where table_schema = 'garmin' and table_name = any(%(names)s)
                     and column_name <> 'account_id'
                   order by table_name, ordinal_position""",
                {"names": [n for n, _ in QUERYABLE]}, limit=2000)
    by_table: dict[str, list[str]] = {}
    for c in cols:
        by_table.setdefault(c["table_name"], []).append(f"{c['column_name']} {c['data_type']}")
    return {
        "schema": "garmin",
        "notes": "Query with the schema prefix (garmin.v_daily_wellness). Single account; no need to filter "
                 "by account_id. Read-only, 5 s timeout, 500 rows max.",
        "tables": [{"name": n, "description": d, "columns": by_table.get(n, [])} for n, d in QUERYABLE],
    }


_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|grant|revoke|truncate|copy|call|do|"
                        r"vacuum|analyze|lock|set|reset|listen|notify|prepare|execute|refresh)\b", re.I)


def query_sql(sql: str) -> dict[str, Any]:
    """Read-only SQL. Safety comes from the database (read-only transaction, reader role with
    SELECT on curated tables only, statement timeout); the keyword check just gives a clearer error."""
    text = sql.strip().rstrip(";")
    if ";" in text:
        raise ToolError("One statement at a time.")
    if not re.match(r"^\s*(select|with)\b", text, re.I) or _FORBIDDEN.search(re.sub(r"'[^']*'", "''", text)):
        raise ToolError("Only SELECT / WITH queries are allowed.")
    try:
        rows = read(text.replace("%", "%%"))
    except psycopg.errors.QueryCanceled as e:
        raise ToolError("Query took longer than 5 s; narrow it (date filters, fewer rows).") from e
    except psycopg.Error as e:
        raise ToolError(f"SQL error: {str(e).splitlines()[0]}") from e
    return {"row_count": len(rows), "rows": rows}

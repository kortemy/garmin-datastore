"""Monthly range domains (scope "range", key "YYYY-MM").

Backfilled month by month; the current and previous month (plus future months for the
calendar) are re-fetched daily. Endpoints that reject a whole month are fetched in
7-day chunks.
"""

import calendar
import datetime as dt
from typing import Any

from gds.domains import Domain, Rows, register
from gds.domains._util import get, num, offset_min, parse_date, parse_ts, to_int
from gds.source import Source, Transient

CHUNK_DAYS = 7
DAILY_REFRESH_H = 20


def month_bounds(key: str, cap_today: bool = True) -> tuple[dt.date, dt.date]:
    y, m = map(int, key.split("-"))
    start, end = dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])
    if cap_today:
        end = min(end, dt.date.today())
    return start, end


def _chunks(src: Source, method: str, start: dt.date, end: dt.date) -> dict[str, Any]:
    out, cur = [], start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=CHUNK_DAYS - 1), end)
        out.append({"start": cur.isoformat(), "end": stop.isoformat(),
                    "data": src.call(method, cur.isoformat(), stop.isoformat())})
        cur = stop + dt.timedelta(days=1)
    return {"chunks": out}


def range_fetch(method: str, chunked: bool = False, *extra: Any):
    def fetch(src: Source, key: str) -> Any:
        start, end = month_bounds(key)
        if start > dt.date.today():
            return None
        if chunked:
            return _chunks(src, method, start, end)
        try:
            return src.call(method, start.isoformat(), end.isoformat(), *extra)
        except Transient as e:
            if "400" not in str(e):
                raise
            return _chunks(src, method, start, end)  # endpoint has a shorter max window

    return fetch


# ---------------------------------------------------------------- typed


def _grams_to_kg(v: Any) -> float | None:
    v = num(v)
    return round(v / 1000, 2) if v is not None else None


def _weigh_ins(raw: Any, key: str) -> Rows:
    items = raw.get("dateWeightList") if isinstance(raw, dict) else None
    rows = []
    for w in items or []:
        if not isinstance(w, dict) or to_int(w.get("samplePk")) is None:
            continue
        rows.append(
            {
                "sample_pk": to_int(w.get("samplePk")),
                "measured_utc": parse_ts(w.get("timestampGMT")),
                "calendar_date": parse_date(w["calendarDate"]) if w.get("calendarDate") else None,
                "weight_kg": _grams_to_kg(w.get("weight")),
                "bmi": num(w.get("bmi")),
                "body_fat_pct": num(w.get("bodyFat")),
                "body_water_pct": num(w.get("bodyWater")),
                "bone_mass_kg": _grams_to_kg(w.get("boneMass")),
                "muscle_mass_kg": _grams_to_kg(w.get("muscleMass")),
                "visceral_fat": num(w.get("visceralFat")),
                "metabolic_age": num(w.get("metabolicAge")),
                "physique_rating": num(w.get("physiqueRating")),
                "source_type": w.get("sourceType"),
            }
        )
    return _month_snapshot("weigh_ins", rows, raw, key, "dateWeightList")


def _month_snapshot(table: str, rows: list[dict[str, Any]], raw: Any, key: str, list_field: str) -> Rows:
    """Rows of a full month listing, plus a replace scope so readings deleted in Garmin
    (or undone by a write tool) disappear here too. Only when the payload really is a listing."""
    if not isinstance(raw, dict) or list_field not in raw:
        return {table: rows} if rows else {}
    start, end = month_bounds(key, cap_today=False)
    return {table: rows, "_replace": [{"table": table, "between": ("calendar_date", start, end)}]}


def _blood_pressure(raw: Any, key: str) -> Rows:
    rows = []
    for summary in (raw.get("measurementSummaries") if isinstance(raw, dict) else None) or []:
        for m in (summary.get("measurements") if isinstance(summary, dict) else None) or []:
            ts = parse_ts(m.get("measurementTimestampGMT")) if isinstance(m, dict) else None
            if ts is None:
                continue
            local = m.get("measurementTimestampLocal")
            rows.append(
                {
                    "measured_utc": ts,
                    "calendar_date": parse_date(str(local)[:10]) if local else ts.date(),
                    "utc_offset_min": offset_min(m.get("measurementTimestampGMT"), local),
                    "systolic": to_int(m.get("systolic")),
                    "diastolic": to_int(m.get("diastolic")),
                    "pulse": to_int(m.get("pulse")),
                    "category": m.get("categoryName") or m.get("category"),
                    "notes": m.get("notes"),
                    "source_type": m.get("sourceType"),
                    "sample_version": to_int(m.get("version")),
                }
            )
    return _month_snapshot("blood_pressure", rows, raw, key, "measurementSummaries")


def metric_row(day: Any, metric: str, value: Any, detail: Any = None) -> dict[str, Any] | None:
    try:
        date = day if isinstance(day, dt.date) else parse_date(str(day)[:10])
    except ValueError:
        return None
    return {"calendar_date": date, "metric": metric, "value": num(value), "detail": detail or None}


def _endurance(raw: Any, key: str) -> Rows:
    groups = raw.get("groupMap") if isinstance(raw, dict) else None
    rows = [
        r for day, g in (groups or {}).items()
        if isinstance(g, dict) and num(g.get("groupAverage")) is not None
        and (r := metric_row(day, "endurance_score", g.get("groupAverage"),
                             {"max": g.get("groupMax"), "contributors": g.get("enduranceContributorDTOList")}))
    ]
    return {"performance_metrics": rows} if rows else {}


def _hill(raw: Any, key: str) -> Rows:
    rows = []
    for h in (raw.get("hillScoreDTOList") if isinstance(raw, dict) else None) or []:
        if not isinstance(h, dict) or not h.get("calendarDate"):
            continue
        for metric, field in (("hill_score", "overallScore"), ("hill_strength", "strengthScore"),
                              ("hill_endurance", "enduranceScore")):
            if num(h.get(field)) is not None and (r := metric_row(h["calendarDate"], metric, h.get(field))):
                rows.append(r)
    return {"performance_metrics": rows} if rows else {}


def _vo2max_history(raw: Any, key: str) -> Rows:
    rows = []
    for item in raw if isinstance(raw, list) else []:
        for metric, part in (("vo2max_running", "generic"), ("vo2max_cycling", "cycling")):
            p = get(item, part, default={})
            if isinstance(p, dict) and num(p.get("vo2MaxPreciseValue") or p.get("vo2MaxValue")) is not None:
                r = metric_row(p.get("calendarDate"), metric, p.get("vo2MaxPreciseValue") or p.get("vo2MaxValue"))
                if r:
                    rows.append(r)
    return {"performance_metrics": rows} if rows else {}


def _fetch_calendar(src: Source, key: str) -> Any:
    y, m = map(int, key.split("-"))
    return src.call("get_scheduled_workouts", y, m)


# Calendar items mix units by type (observed): activities report duration in ms and
# distance in cm; naps report duration in seconds. Unknown types keep only the raw `data`.
CALENDAR_UNITS = {"activity": (1000, 100), "nap": (1, None)}


def _calendar_item_id(i: dict[str, Any]) -> str | None:
    """Most items have a numeric id; naps only have their start time."""
    if i.get("id") is not None:
        return str(i["id"])
    for k in ("wellnessActivityUuid", "napStartTimeLocal"):
        if i.get(k):
            return f"{i.get('itemType')}:{i[k]}"
    return None


def _calendar(raw: Any, key: str) -> Rows:
    items = raw.get("calendarItems") if isinstance(raw, dict) else None
    rows = []
    for i in items or []:
        if not isinstance(i, dict) or not str(i.get("date", "")).startswith(key) or not _calendar_item_id(i):
            continue  # the month grid also shows neighbouring months' days
        item_type = i.get("itemType") or "unknown"
        dur_div, dist_div = CALENDAR_UNITS.get(item_type, (None, None))
        duration, distance = num(i.get("duration")), num(i.get("distance"))
        rows.append(
            {
                "item_type": item_type,
                "item_id": _calendar_item_id(i),
                "month": key,
                "item_date": parse_date(i["date"]),
                "title": i.get("title"),
                "sport_type_key": i.get("sportTypeKey"),
                "duration_s": duration / dur_div if duration is not None and dur_div else None,
                "distance_m": distance / dist_div if distance is not None and dist_div else None,
                "workout_id": to_int(i.get("workoutId")),
                "training_plan_id": to_int(i.get("trainingPlanId")),
                "data": i,
            }
        )
    # Full listing of the month: anything no longer there was removed in Garmin.
    return {"calendar_items": rows, "_replace": [{"table": "calendar_items", "scope": {"month": key}}]}


def _events(raw: Any, key: str) -> Rows:
    rows = [
        {"kind": "event", "entity_id": str(e["id"]), "name": e.get("eventName"), "data": e}
        for e in raw if isinstance(e, dict) and e.get("id") is not None
    ] if isinstance(raw, list) else []
    return {"entities": rows} if rows else {}


# ---------------------------------------------------------------- registry

# Typed monthly domains are cheap (~one request per month) and high value, so they run
# ahead of the daily backfill.
TYPED_PRIORITY = 40
TYPED = [
    ("body_composition", range_fetch("get_body_composition"), _weigh_ins, 0),
    ("blood_pressure", range_fetch("get_blood_pressure"), _blood_pressure, 0),
    ("endurance_score", range_fetch("get_endurance_score", False, "daily"), _endurance, 0),
    ("hill_score", range_fetch("get_hill_score"), _hill, 0),
    ("vo2max_history", range_fetch("get_max_metrics_range"), _vo2max_history, 0),
    ("calendar_month", _fetch_calendar, _calendar, 2),
    ("calendar_events", range_fetch("get_calendar_events"), _events, 0),
]
for _name, _fetch, _norm, _ahead in TYPED:
    register(Domain(_name, "range", _fetch, _norm, priority=TYPED_PRIORITY, refresh_hours=DAILY_REFRESH_H,
                    months_ahead=_ahead))

# Raw-only range endpoints (aggregates and histories; full payloads kept, FR-2).
RAW_RANGE = [
    "get_daily_summaries", "get_training_readiness_range", "get_weekly_training_status", "get_sleep_daily",
    "get_hrv_data_range", "get_training_load_activities", "get_fastest_splits", "get_daily_steps",
    "get_daily_intensity_minutes_stats", "get_daily_respiration_stats", "get_calories_daily", "get_biometric_stats",
    "get_rhr_daily", "get_cycling_ability_range", "get_weigh_ins", "get_weekly_intensity_minutes",
    "get_progress_summary_between_dates", "get_weekly_wellness_stats", "get_weekly_activity_stats",
    "get_sleep_stats_overall", "get_acclimation_stats", "get_weight_snapshot", "get_monthly_wellness_stats",
    "get_calorie_summary", "get_power_to_weight_range", "get_functional_threshold_power_range",
    "get_critical_swim_speed_range", "get_power_curve", "get_stats_averages", "get_glucose_data_range",
    "get_weight_goal", "get_available_metrics", "get_menstrual_calendar_data", "get_daily_hydration_stats",
    "get_daily_last_blood_pressure", "get_daily_last_weight", "get_health_snapshots_range",
    "get_heat_altitude_acclimation_range", "get_running_economy_range", "get_running_tolerance",
    "get_scheduled_workouts_by_period",
]
# These reject a full month ("difference in days" limit), so always fetch in 7-day chunks.
RAW_RANGE_CHUNKED = [
    "get_daily_body_battery_stats", "get_daily_floors_stats", "get_daily_heart_rate_stats", "get_daily_stats",
    "get_daily_stress_stats",
]
for _m in RAW_RANGE:
    register(Domain(f"r_{_m.removeprefix('get_')}", "range", range_fetch(_m), priority=3,
                    refresh_hours=DAILY_REFRESH_H))
for _m in RAW_RANGE_CHUNKED:
    register(Domain(f"r_{_m.removeprefix('get_')}", "range", range_fetch(_m, True), priority=2,
                    refresh_hours=DAILY_REFRESH_H))

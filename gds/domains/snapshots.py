"""Snapshot domains (scope "snapshot", key "latest") and per-entity details (scope "entity").

Snapshots are re-fetched daily. Lists of things (devices, gear, PRs, goals, badges,
workouts, plans, courses) become `entities` rows, replaced per kind so deletions in
Garmin disappear here too. Current scalar values (race predictions, lactate threshold,
FTP, fitness age) become dated `performance_metrics`.
"""

from typing import Any

from gds.domains import Domain, Rows, register
from gds.domains._util import num
from gds.domains.ranges import metric_row

DAILY_H = 20
WEEKLY_H = 24 * 7


def _snap(method: str):
    return lambda src, key: src.call(method)


def _entity_list(kind: str, items: Any, id_of, name_of, children=None) -> Rows:
    if not isinstance(items, list):
        return {}
    rows, units = [], []
    for item in items:
        if not isinstance(item, dict) or (eid := id_of(item)) in (None, ""):
            continue
        rows.append({"kind": kind, "entity_id": str(eid), "name": name_of(item), "data": item})
        if children:
            units += [{"domain": d, "scope_key": str(k), "priority": 25, "requeue": True}
                      for d, k in children(item) if k]
    result: Rows = {"entities": rows, "_replace": [{"table": "entities", "scope": {"kind": kind}}]}
    if units:
        result["work_units"] = units
    return result


def _single(kind: str, raw: Any, name: str | None = None) -> Rows:
    if not isinstance(raw, dict) or not raw:
        return {}
    return {"entities": [{"kind": kind, "entity_id": "current", "name": name, "data": raw}]}


# ---------------------------------------------------------------- entity lists

def _devices(raw: Any, key: str) -> Rows:
    return _entity_list("device", raw, lambda d: d.get("deviceId"),
                        lambda d: d.get("productDisplayName") or d.get("displayName"))


def _gear(raw: Any, key: str) -> Rows:
    return _entity_list(
        "gear", raw, lambda g: g.get("uuid"),
        lambda g: " ".join(x for x in (g.get("brand"), g.get("model")) if x) or g.get("displayName"),
        children=lambda g: [("gear_stats", g.get("uuid"))],
    )


def _personal_records(raw: Any, key: str) -> Rows:
    return _entity_list("personal_record", raw, lambda r: r.get("typeId"),
                        lambda r: r.get("prTypeLabelKey") or f"type {r.get('typeId')}")


def _goals(raw: Any, key: str) -> Rows:
    return _entity_list("goal", raw, lambda g: g.get("userGoalPk"),
                        lambda g: g.get("goalName") or g.get("userGoalType"))


def _badges(raw: Any, key: str) -> Rows:
    return _entity_list("badge", raw, lambda b: b.get("badgeId"), lambda b: b.get("badgeName"))


def _workouts(raw: Any, key: str) -> Rows:
    return _entity_list("workout", raw, lambda w: w.get("workoutId"), lambda w: w.get("workoutName"),
                        children=lambda w: [("workout_detail", w.get("workoutId"))])


def _training_plans(raw: Any, key: str) -> Rows:
    plans = raw.get("trainingPlanList") if isinstance(raw, dict) else raw
    return _entity_list("training_plan", plans, lambda p: p.get("trainingPlanId"), lambda p: p.get("name"))


def _courses(raw: Any, key: str) -> Rows:
    items = raw.get("coursesForUser") if isinstance(raw, dict) else raw
    return _entity_list("course", items, lambda c: c.get("courseId"), lambda c: c.get("courseName"))


# ---------------------------------------------------------------- performance snapshots

def _race_predictions(raw: Any, key: str) -> Rows:
    if not isinstance(raw, dict) or not raw.get("calendarDate"):
        return {}
    day = raw["calendarDate"]
    rows = [r for metric, field in (("race_5k_s", "time5K"), ("race_10k_s", "time10K"),
                                    ("race_half_marathon_s", "timeHalfMarathon"), ("race_marathon_s", "timeMarathon"))
            if num(raw.get(field)) is not None and (r := metric_row(day, metric, raw.get(field)))]
    return {"performance_metrics": rows} if rows else {}


def _lactate_threshold(raw: Any, key: str) -> Rows:
    if not isinstance(raw, dict):
        return {}
    rows = []
    shr = raw.get("speed_and_heart_rate") or {}
    if shr.get("calendarDate"):
        if num(shr.get("heartRate")) is not None:
            rows.append(metric_row(shr["calendarDate"], "lactate_threshold_hr_bpm", shr.get("heartRate")))
        if num(shr.get("speed")) is not None:
            # Unit isn't documented; observed 0.3 for a ~5:30/km threshold, i.e. it looks like m/s ÷ 10.
            rows.append(metric_row(shr["calendarDate"], "lactate_threshold_speed_raw", shr.get("speed")))
    pw = raw.get("power") or {}
    if pw.get("calendarDate") and num(pw.get("functionalThresholdPower")) is not None:
        rows.append(metric_row(pw["calendarDate"], f"ftp_{(pw.get('sport') or 'running').lower()}_w",
                               pw.get("functionalThresholdPower"), {"power_to_weight": pw.get("powerToWeight")}))
    rows = [r for r in rows if r]
    return {"performance_metrics": rows} if rows else {}


def _fitness_age_details(raw: Any, key: str) -> Rows:
    if not isinstance(raw, dict) or num(raw.get("fitnessAge")) is None or not raw.get("lastUpdated"):
        return {}
    day = raw["lastUpdated"]
    rows = [metric_row(day, "fitness_age", raw.get("fitnessAge"), {"components": raw.get("components")}),
            metric_row(day, "fitness_age_achievable", raw.get("achievableFitnessAge"))]
    return {"performance_metrics": [r for r in rows if r and r["value"] is not None]}


# ---------------------------------------------------------------- per-entity details

def _gear_stats(raw: Any, key: str) -> Rows:
    if not isinstance(raw, dict) or not raw:
        return {}
    return {"entities": [{"kind": "gear_stats", "entity_id": key, "name": None, "data": raw}]}


def _workout_detail(raw: Any, key: str) -> Rows:
    if not isinstance(raw, dict) or not raw.get("workoutId"):
        return {}
    return {"entities": [{"kind": "workout_detail", "entity_id": key, "name": raw.get("workoutName"), "data": raw}]}


def _detail(method: str):
    return lambda src, key: src.call(method, key)


# ---------------------------------------------------------------- registry

TYPED = [
    ("devices", "get_devices", _devices),
    ("gear", "get_gear_list", _gear),
    ("personal_records", "get_personal_record", _personal_records),
    ("goals", "get_user_goals", _goals),
    ("badges", "get_earned_badges", _badges),
    ("workouts", "get_workouts", _workouts),
    ("training_plans", "get_training_plans", _training_plans),
    ("courses", "get_courses", _courses),
    ("race_predictions", "get_race_predictions", _race_predictions),
    ("lactate_threshold", "get_lactate_threshold", _lactate_threshold),
    ("fitness_age_details", "get_fitness_age_details", _fitness_age_details),
    ("profile", "get_user_profile", lambda raw, key: _single("profile", raw)),
    ("profile_settings", "get_userprofile_settings", lambda raw, key: _single("profile_settings", raw)),
    ("hr_zones", "get_heart_rate_zones", lambda raw, key: (
        {"entities": [{"kind": "hr_zones", "entity_id": "current", "name": None, "data": {"zones": raw}}]}
        if raw else {})),
    ("power_zones", "get_power_zones", lambda raw, key: (
        {"entities": [{"kind": "power_zones", "entity_id": "current", "name": None, "data": {"zones": raw}}]}
        if raw else {})),
]
for _name, _method, _norm in TYPED:
    register(Domain(_name, "snapshot", _snap(_method), _norm, priority=30, refresh_hours=DAILY_H))

register(Domain("gear_stats", "entity", _detail("get_gear_stats"), _gear_stats, priority=25))
register(Domain("workout_detail", "entity", _detail("get_workout_by_id"), _workout_detail, priority=25))

# Raw-only snapshots (FR-2). Those that were empty for this account are re-checked weekly.
RAW_DAILY = [
    "get_available_badges", "get_daily_sleep", "get_usage_indicators", "get_available_virtual_challenges",
    "get_primary_training_device", "get_all_devices", "get_historical_devices", "get_sleep_stats_weekly",
    "get_activity_types", "get_in_progress_badges", "get_daily_movement", "get_weekly_calories_stats",
    "get_workout_types", "get_fitness_age_weekly", "get_weekly_floors_stats", "get_personal_record_types",
    "get_last_activity", "get_weekly_heart_rate_stats", "get_calendar_week", "get_latest_earned_badges",
    "get_privacy_settings", "get_spo2_acclimation_data", "get_pace_bands", "get_golf_club_stats",
    "get_badge_attributes", "get_active_devices", "get_monthly_steps", "get_strength_benchmarks",
    "get_user_profile_base", "get_current_biometrics", "get_max_metrics_latest", "get_weekly_weight",
    "get_user_statistics", "get_personal_information", "get_activity_event_types", "get_gear_types",
    "get_hydration_all_data", "get_active_training_plans", "get_device_last_used", "get_heart_rate_zones_v2",
    "get_first_weight", "get_latest_weight", "get_favorite_activities", "get_lifetime_totals", "get_cycling_ftp",
    "get_effective_goals", "get_blood_pressure_day", "get_steps_goal", "get_weekly_blood_pressure",
    "get_device_messages", "get_nutrition_status", "get_week_average_resting_hr", "get_workout_count",
    "get_workout_sport_types", "get_golf_summary", "get_user_gear_types", "get_user_timezone", "get_unit_system",
]
RAW_WEEKLY = [
    "get_active_adhoc_challenges", "get_badges_for_day", "get_completed_virtual_challenges", "get_course_favorites",
    "get_critical_swim_speed", "get_cycling_ability", "get_device_alarms", "get_goals", "get_health_snapshots",
    "get_heat_altitude_acclimation", "get_jetlag_status", "get_jetlag_trips", "get_naps", "get_next_scheduled_workout",
    "get_one_rep_max", "get_personal_record_candidates", "get_pregnancy_summary", "get_primary_event",
    "get_running_economy", "get_sensors", "get_stamina_curve", "get_upcoming_events", "get_weekly_hydration_stats",
]
for _m in RAW_DAILY:
    register(Domain(f"s_{_m.removeprefix('get_')}", "snapshot", _snap(_m), priority=20, refresh_hours=DAILY_H))
for _m in RAW_WEEKLY:
    register(Domain(f"s_{_m.removeprefix('get_')}", "snapshot", _snap(_m), priority=19, refresh_hours=WEEKLY_H))

# Not synced: get_activities (covered by activity_index), get_sync_timestamp (changes every sync,
# no information), get_full_name (duplicate of profile), get_ecg_list / get_hourly_intensity_minutes
# (rejected for any input on this account).

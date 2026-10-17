"""Build committed test fixtures from captured payloads (scratch/payloads, git-ignored).

Scrubs account/device identifiers and truncates long arrays, keeping each payload's
structure. Health values are kept, so review before making the repo public (FR-24).

    uv run python scripts/make_fixtures.py 2026-10-15
"""

import json
import sys
from pathlib import Path

SRC = Path("scratch/payloads")
OUT = Path("tests/fixtures/daily")
ACT_SRC = SRC / "activities"
ACT_OUT = Path("tests/fixtures/activities")
SCRUB = {"userProfilePK", "userProfilePk", "userProfileId", "userId", "deviceId", "uuid",
         "userDailySummaryId", "id", "imageURL", "deviceName", "ownerId", "ownerDisplayName", "ownerFullName",
         "ownerProfileImageUrlSmall", "ownerProfileImageUrlMedium", "ownerProfileImageUrlLarge", "userRoles",
         "activityUUID", "polyline", "geoPolylineDTO", "weatherStationDTO", "gearPk",
         "displayname", "fullname", "profileImageUrlLarge", "profileImageUrlMedium", "profileImageUrlSmall"}
# Coordinates reveal where you live/train: replace with fixed placeholders.
COORDS = {"startLatitude": 45.0, "startLongitude": 20.0, "endLatitude": 45.0, "endLongitude": 20.0,
          "latitude": 45.0, "longitude": 20.0}
# Free-text fields that can name places.
PLACEHOLDERS = {"activityName": "Test Activity", "locationName": "Test City"}
MAX_LIST = 30

ACTIVITY_FILES = {
    "summary": "get_activity",
    "laps": "get_activity_splits",
    "hr_zones": "get_activity_hr_in_timezones",
    "power_zones": "get_activity_power_in_timezones",
    "exercise_sets": "get_activity_exercise_sets",
    "weather": "get_activity_weather",
    "gear": "get_activity_gear",
}

DOMAINS = {
    "summary": "get_user_summary",
    "sleep": "get_sleep_data",
    "hrv": "get_hrv_data",
    "readiness": {"readings": "get_training_readiness", "morning": "get_morning_training_readiness"},
    "training_status": {"status": "get_training_status", "max_metrics": "get_max_metrics"},
    "stress": "get_stress_data",
    "heart_rate": "get_heart_rates",
    "respiration": "get_respiration_data",
    "steps": "get_steps_data",
    "floors": "get_floors",
    "spo2": "get_spo2_data",
    "hydration": "get_hydration_data",
}


def scrub(o):
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if k in SCRUB:
                continue
            if k in COORDS and isinstance(v, (int, float)):
                v = COORDS[k]
            if k in PLACEHOLDERS and isinstance(v, str):
                v = PLACEHOLDERS[k]
            out["0" if k.isdigit() else k] = scrub(v)
        return out
    if isinstance(o, list):
        return [scrub(v) for v in o[:MAX_LIST]]
    return o


def make_activity_fixtures() -> None:
    ACT_OUT.mkdir(parents=True, exist_ok=True)
    page = json.loads((ACT_SRC / "get_activities__page0.json").read_text(encoding="utf-8"))
    picked = []
    for t in ("running", "strength_training", "soccer"):
        item = next((a for a in page if (a.get("activityType") or {}).get("typeKey") == t), None)
        if item:
            picked.append(item)
    (ACT_OUT / "index.json").write_text(json.dumps(scrub(picked), indent=1), encoding="utf-8")
    for t in ("running", "strength_training", "soccer"):
        for name, method in ACTIVITY_FILES.items():
            f = ACT_SRC / f"{method}__{t}.json"
            if f.exists():
                payload = scrub(json.loads(f.read_text(encoding="utf-8")))
                (ACT_OUT / f"{t}__{name}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print("wrote activity fixtures")


M3_SRC = SRC / "m3"
M3_OUT = Path("tests/fixtures/m3")
M3_FILES = {
    "body_composition": "x_body_composition_all", "blood_pressure": "x_blood_pressure_2026-09",
    "endurance_score": "x_endurance_score_daily_2026-09", "hill_score": "x_hill_score_2026-09",
    "vo2max_history": "get_max_metrics_range", "calendar_month": "x_scheduled_workouts_2026-10",
    "calendar_events": "get_calendar_events", "devices": "get_devices", "gear": "x_gear_list",
    "personal_records": "get_personal_record", "goals": "get_user_goals", "badges": "get_earned_badges",
    "workouts": "get_workouts", "training_plans": "get_training_plans", "race_predictions": "get_race_predictions",
    "lactate_threshold": "get_lactate_threshold", "fitness_age_details": "get_fitness_age_details",
    "gear_stats": "x_gear_stats_first", "workout_detail": "x_workout_first",
}
# Entity ids are kept (tests need them, they aren't sensitive alone); people/places are not.
M3_SCRUB = {"ownerDisplayName", "ownerFullName", "fullName", "displayName", "displayname", "fullname",
            "profileImgNameLarge", "profileImgNameMedium", "profileImgNameSmall", "ownerProfileImageUrlSmall",
            "ownerProfileImageUrlMedium", "ownerProfileImageUrlLarge", "profileImageUrlLarge",
            "profileImageUrlMedium", "profileImageUrlSmall", "serialNumber", "unitId", "emailAddress", "url",
            "registrationUrl", "imageUrl", "imageURL", "userProfilePK", "userProfilePk", "userProfileId",
            "userId", "ownerId", "location", "latitude", "longitude"}
M3_PLACEHOLDERS = {"activityName": "Test Activity", "eventName": "Test Event", "title": "Test Item",
                   "locationName": "Test City"}


def scrub_m3(o):
    if isinstance(o, dict):
        return {k: (M3_PLACEHOLDERS[k] if k in M3_PLACEHOLDERS and isinstance(v, str) else scrub_m3(v))
                for k, v in o.items() if k not in M3_SCRUB}
    if isinstance(o, list):
        return [scrub_m3(v) for v in o[:MAX_LIST]]
    return o


def make_m3_fixtures() -> None:
    M3_OUT.mkdir(parents=True, exist_ok=True)
    for name, src in M3_FILES.items():
        f = M3_SRC / f"{src}.json"
        if f.exists():
            payload = scrub_m3(json.loads(f.read_text(encoding="utf-8")))
            (M3_OUT / f"{name}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    for name, method in (("body_battery_events", "get_body_battery_events"), ("fitness_age", "get_fitnessage_data")):
        payload = scrub(load(method, "2026-10-15"))
        (OUT / f"{name}.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print("wrote m3 fixtures")


def load(method: str, date: str):
    return json.loads((SRC / f"{method}__{date}.json").read_text(encoding="utf-8"))


def main() -> None:
    date = sys.argv[1]
    OUT.mkdir(parents=True, exist_ok=True)
    for domain, spec in DOMAINS.items():
        payload = {k: load(m, date) for k, m in spec.items()} if isinstance(spec, dict) else load(spec, date)
        (OUT / f"{domain}.json").write_text(json.dumps(scrub(payload), indent=1), encoding="utf-8")
        print("wrote", domain)
    if ACT_SRC.exists():
        make_activity_fixtures()
    if M3_SRC.exists():
        make_m3_fixtures()


if __name__ == "__main__":
    main()

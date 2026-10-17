"""Activity domains (design §5): the activity index plus per-activity sub-resources and FIT files.

`activity_index` lists activities, upserts their summaries (the list item is already rich),
and creates one work unit per activity × sub-resource. Units are keyed by activity id.
"""

import datetime as dt
import hashlib
from typing import Any

from gds.domains import Domain, Rows, activity_domains, register
from gds.domains._util import get, num, offset_min, parse_ts, to_int
from gds.source import Source

PAGE = 100
RECENT_PAGE = 30
REQUEUE_IF_STARTED_WITHIN = dt.timedelta(days=2)  # names/notes/RPE get edited after the fact
INDOOR_HINTS = ("indoor", "treadmill", "strength", "yoga", "pilates", "cardio", "breathwork")


# ---------------------------------------------------------------- index


def _fetch_index(src: Source, key: str) -> Any:
    if key == "recent":
        return src.call("get_activities", 0, RECENT_PAGE) or []
    items: list[Any] = []
    start = 0
    while True:
        page = src.call("get_activities", start, PAGE) or []
        items.extend(page)
        if len(page) < PAGE:
            return items
        start += PAGE


def _cadence(a: dict[str, Any], kind: str) -> float | None:
    for key in (f"{kind}RunningCadenceInStepsPerMinute", f"{kind}BikingCadenceInRevPerMinute",
                f"{kind}SwimCadenceInStrokesPerMinute"):
        if num(a.get(key)) is not None:
            return num(a.get(key))
    return None


def _zones(a: dict[str, Any], prefix: str) -> list[float] | None:
    vals = [num(a.get(f"{prefix}{i}")) for i in range(1, 6)]
    return [v if v is not None else 0.0 for v in vals] if any(v is not None for v in vals) else None


def activity_row(a: dict[str, Any]) -> dict[str, Any] | None:
    aid = to_int(a.get("activityId"))
    if aid is None:
        return None
    start = parse_ts(a.get("startTimeGMT"))
    local = parse_ts(a.get("startTimeLocal"))
    duration = num(a.get("duration"))
    end = parse_ts(a.get("endTimeGMT")) or (start + dt.timedelta(seconds=duration) if start and duration else None)
    return {
        "activity_id": aid,
        "activity_uuid": a.get("activityUUID"),
        "name": a.get("activityName"),
        "type_key": get(a, "activityType", "typeKey"),
        "parent_type_id": to_int(get(a, "activityType", "parentTypeId")),
        "event_type": get(a, "eventType", "typeKey"),
        "sport_type_id": to_int(a.get("sportTypeId")),
        "start_utc": start,
        "start_local": local.replace(tzinfo=None) if local else None,
        "utc_offset_min": offset_min(a.get("startTimeGMT"), a.get("startTimeLocal")),
        "end_utc": end,
        "duration_s": duration,
        "elapsed_s": num(a.get("elapsedDuration")),
        "moving_s": num(a.get("movingDuration")),
        "distance_m": num(a.get("distance")),
        "elevation_gain_m": num(a.get("elevationGain")),
        "elevation_loss_m": num(a.get("elevationLoss")),
        "min_elevation_m": num(a.get("minElevation")),
        "max_elevation_m": num(a.get("maxElevation")),
        "avg_speed_mps": num(a.get("averageSpeed")),
        "max_speed_mps": num(a.get("maxSpeed")),
        "avg_gap_speed_mps": num(a.get("avgGradeAdjustedSpeed")),
        "avg_hr": num(a.get("averageHR")),
        "max_hr": num(a.get("maxHR")),
        "calories": num(a.get("calories")),
        "bmr_calories": num(a.get("bmrCalories")),
        "steps": to_int(a.get("steps")),
        "avg_cadence": _cadence(a, "average"),
        "max_cadence": _cadence(a, "max"),
        "avg_power": num(a.get("avgPower")),
        "max_power": num(a.get("maxPower")),
        "norm_power": num(a.get("normPower")),
        "aerobic_te": num(a.get("aerobicTrainingEffect")),
        "anaerobic_te": num(a.get("anaerobicTrainingEffect")),
        "te_label": a.get("trainingEffectLabel"),
        "training_load": num(a.get("activityTrainingLoad")),
        "vo2max": num(a.get("vO2MaxValue")),
        "moderate_im": to_int(a.get("moderateIntensityMinutes")),
        "vigorous_im": to_int(a.get("vigorousIntensityMinutes")),
        "body_battery_diff": to_int(a.get("differenceBodyBattery")),
        "water_estimated_ml": num(a.get("waterEstimated")),
        "lap_count": to_int(a.get("lapCount")),
        "location_name": a.get("locationName"),
        "start_lat": num(a.get("startLatitude")),
        "start_lon": num(a.get("startLongitude")),
        "end_lat": num(a.get("endLatitude")),
        "end_lon": num(a.get("endLongitude")),
        "device_id": to_int(a.get("deviceId")),
        "manufacturer": a.get("manufacturer"),
        "is_manual": bool(a.get("manualActivity") or a.get("isManualActivity")),
        "is_favorite": bool(a.get("favorite") or a.get("isFavorite")),
        "is_pr": bool(a.get("pr") or a.get("isPR")),
        "fastest_1k_s": num(a.get("fastestSplit_1000")),
        "fastest_mile_s": num(a.get("fastestSplit_1609")),
        "hr_zone_s": _zones(a, "hrTimeInZone_"),
        "power_zone_s": _zones(a, "powerTimeInZone_"),
    }


def units_for(a: dict[str, Any], row: dict[str, Any], now: dt.datetime) -> list[dict[str, Any]]:
    """Which sub-resources to fetch for this activity (skip ones that can't exist)."""
    type_key = (row.get("type_key") or "").lower()
    indoor = row.get("start_lat") is None or any(h in type_key for h in INDOOR_HINTS)
    recent = row["start_utc"] is not None and now - row["start_utc"] < REQUEUE_IF_STARTED_WITHIN
    units = []
    for d in activity_domains():
        if d.name == "activity_power_zones" and row.get("avg_power") is None:
            continue
        if d.name == "activity_weather" and indoor:
            continue
        if d.name == "activity_exercise_sets" and "strength" not in type_key and "cardio" not in type_key:
            continue
        if d.name == "activity_fit" and row.get("is_manual"):
            continue  # manual activities have no device file
        units.append({"domain": d.name, "scope_key": str(row["activity_id"]), "priority": d.priority,
                      "requeue": recent})
    return units


def _index(raw: Any, key: str) -> Rows:
    if not isinstance(raw, list) or not raw:
        return {}
    now = dt.datetime.now(dt.UTC)
    rows, units = [], []
    for a in raw:
        if isinstance(a, dict) and (row := activity_row(a)):
            rows.append(row)
            units.extend(units_for(a, row, now))
    return {"activities": rows, "work_units": units} if rows else {}


# ---------------------------------------------------------------- per-activity


def _aid(key: str) -> int:
    return int(key)


def _detail(raw: Any, key: str) -> Rows:
    """get_activity: fields the list doesn't carry (description, RPE, feel, min HR)."""
    if not isinstance(raw, dict) or not raw.get("activityId"):
        return {}
    s = raw.get("summaryDTO") or {}
    return {
        "activities": [
            {
                "activity_id": _aid(key),
                "description": raw.get("description") or s.get("description"),
                "rpe": to_int(s.get("directWorkoutRpe")),
                "feel": to_int(s.get("directWorkoutFeel")),
                "min_hr": num(s.get("minHR")),
            }
        ]
    }


def _laps(raw: Any, key: str) -> Rows:
    laps = raw.get("lapDTOs") if isinstance(raw, dict) else None
    if not isinstance(laps, list) or not laps:
        return {}
    rows = []
    for i, lap in enumerate(laps):
        if not isinstance(lap, dict):
            continue
        rows.append(
            {
                "activity_id": _aid(key),
                "lap_index": to_int(lap.get("lapIndex")) or i + 1,
                "start_utc": parse_ts(lap.get("startTimeGMT")),
                "duration_s": num(lap.get("duration")),
                "moving_s": num(lap.get("movingDuration")),
                "distance_m": num(lap.get("distance")),
                "avg_speed_mps": num(lap.get("averageSpeed")),
                "max_speed_mps": num(lap.get("maxSpeed")),
                "avg_gap_speed_mps": num(lap.get("avgGradeAdjustedSpeed")),
                "avg_hr": num(lap.get("averageHR")),
                "max_hr": num(lap.get("maxHR")),
                "avg_cadence": num(lap.get("averageRunCadence")) or num(lap.get("averageBikeCadence")),
                "max_cadence": num(lap.get("maxRunCadence")) or num(lap.get("maxBikeCadence")),
                "avg_power": num(lap.get("averagePower")),
                "max_power": num(lap.get("maxPower")),
                "norm_power": num(lap.get("normalizedPower")),
                "elevation_gain_m": num(lap.get("elevationGain")),
                "elevation_loss_m": num(lap.get("elevationLoss")),
                "calories": num(lap.get("calories")),
                "intensity_type": lap.get("intensityType"),
                "stride_length_cm": num(lap.get("strideLength")),
                "ground_contact_ms": num(lap.get("groundContactTime")),
                "vert_oscillation_cm": num(lap.get("verticalOscillation")),
                "vert_ratio_pct": num(lap.get("verticalRatio")),
            }
        )
    return {"activity_laps": rows} if rows else {}


def _zone_rows(raw: Any, key: str, zone_type: str) -> Rows:
    if not isinstance(raw, list):
        return {}
    rows = [
        {
            "activity_id": _aid(key),
            "zone_type": zone_type,
            "zone": to_int(z.get("zoneNumber")),
            "low_boundary": num(z.get("zoneLowBoundary")),
            "seconds": num(z.get("secsInZone")),
        }
        for z in raw
        if isinstance(z, dict) and to_int(z.get("zoneNumber")) is not None
    ]
    return {"activity_zones": rows} if rows else {}


def _sets(raw: Any, key: str) -> Rows:
    sets = raw.get("exerciseSets") if isinstance(raw, dict) else None
    if not isinstance(sets, list) or not sets:
        return {}
    rows = []
    for i, s in enumerate(sets):
        if not isinstance(s, dict):
            continue
        candidates = [e for e in s.get("exercises") or [] if isinstance(e, dict)]
        best = max(candidates, key=lambda e: num(e.get("probability")) or 0, default={})
        rows.append(
            {
                "activity_id": _aid(key),
                "set_index": i,
                "set_type": s.get("setType"),
                "start_utc": parse_ts(s.get("startTime")),
                "duration_s": num(s.get("duration")),
                "reps": to_int(s.get("repetitionCount")),
                "weight_raw": num(s.get("weight")),
                "category": best.get("category"),
                "exercise_name": best.get("name"),
                "probability": num(best.get("probability")),
                "candidates": candidates or None,
            }
        )
    return {"activity_exercise_sets": rows} if rows else {}


def _weather(raw: Any, key: str) -> Rows:
    if not isinstance(raw, dict) or num(raw.get("temp")) is None:
        return {}
    return {
        "activity_weather": [
            {
                "activity_id": _aid(key),
                "observed_utc": parse_ts((raw.get("issueDate") or "").replace("+00:00", "")),
                "temp_f": num(raw.get("temp")),
                "apparent_temp_f": num(raw.get("apparentTemp")),
                "dew_point_f": num(raw.get("dewPoint")),
                "humidity_pct": num(raw.get("relativeHumidity")),
                "wind_dir_deg": num(raw.get("windDirection")),
                "wind_dir_compass": raw.get("windDirectionCompassPoint"),
                "wind_speed_mph": num(raw.get("windSpeed")),
                "wind_gust_mph": num(raw.get("windGust")),
                "conditions": get(raw, "weatherTypeDTO", "desc"),
                "station_id": get(raw, "weatherStationDTO", "id"),
            }
        ]
    }


def _gear(raw: Any, key: str) -> Rows:
    if not isinstance(raw, list):
        return {}
    gear = [
        {
            "gear_pk": to_int(g.get("gearPk")),
            "name": g.get("displayName") or g.get("customMakeModel") or g.get("gearModelName"),
            "type": g.get("gearTypeName"),
        }
        for g in raw
        if isinstance(g, dict)
    ]
    return {"activities": [{"activity_id": _aid(key), "gear": gear}]} if gear else {}


def _fetch_fit(src: Source, key: str) -> Any:
    from garminconnect import Garmin

    return src.call("download_activity", key, Garmin.ActivityDownloadFormat.ORIGINAL)


def _fit(raw: Any, key: str) -> Rows:
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return {}
    data = bytes(raw)
    return {
        "activity_files": [
            {
                "activity_id": _aid(key),
                "format": "original_zip",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content": data,
            }
        ]
    }


def _simple(method: str):
    return lambda src, key: src.call(method, key)


# ---------------------------------------------------------------- registry

register(Domain("activity_index", "index", _fetch_index, _index, priority=50))
register(Domain("activity_summary", "activity", _simple("get_activity"), _detail, priority=9))
register(Domain("activity_laps", "activity", _simple("get_activity_splits"), _laps, priority=9))
register(Domain("activity_hr_zones", "activity", _simple("get_activity_hr_in_timezones"),
                lambda raw, key: _zone_rows(raw, key, "hr"), priority=9))
register(Domain("activity_power_zones", "activity", _simple("get_activity_power_in_timezones"),
                lambda raw, key: _zone_rows(raw, key, "power"), priority=8))
register(Domain("activity_exercise_sets", "activity", _simple("get_activity_exercise_sets"), _sets, priority=9))
register(Domain("activity_weather", "activity", _simple("get_activity_weather"), _weather, priority=7))
register(Domain("activity_gear", "activity", _simple("get_activity_gear"), _gear, priority=7))
register(Domain("activity_fit", "activity", _fetch_fit, _fit, priority=8, binary=True))
# Raw-only (full payloads kept; per-sample charts are better read from the FIT file).
register(Domain("activity_details", "activity", _simple("get_activity_details"), priority=6))
register(Domain("activity_typed_splits", "activity", _simple("get_activity_typed_splits"), priority=6))
register(Domain("activity_split_summaries", "activity", _simple("get_activity_split_summaries"), priority=6))

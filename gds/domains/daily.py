"""Daily (per calendar date) domains: summary, sleep, HRV, readiness, training status, intraday."""

import datetime as dt
from typing import Any

from gds.domains import Domain, Rows, register
from gds.domains._util import (
    array_points,
    build_series,
    descriptor_index,
    get,
    num,
    offset_min,
    parse_date,
    parse_ts,
    to_int,
)
from gds.source import Source

# ---------------------------------------------------------------- summary


def _summary(raw: Any, day: str) -> Rows:
    if not isinstance(raw, dict) or (raw.get("totalSteps") is None and raw.get("restingHeartRate") is None):
        return {}
    r = raw
    row = {
        "calendar_date": parse_date(day),
        "utc_offset_min": offset_min(r.get("wellnessStartTimeGmt"), r.get("wellnessStartTimeLocal")),
        "total_steps": to_int(r.get("totalSteps")),
        "step_goal": to_int(r.get("dailyStepGoal")),
        "total_distance_m": to_int(r.get("totalDistanceMeters")),
        "floors_ascended": num(r.get("floorsAscended")),
        "floors_descended": num(r.get("floorsDescended")),
        "total_kcal": num(r.get("totalKilocalories")),
        "active_kcal": num(r.get("activeKilocalories")),
        "bmr_kcal": num(r.get("bmrKilocalories")),
        "moderate_intensity_min": to_int(r.get("moderateIntensityMinutes")),
        "vigorous_intensity_min": to_int(r.get("vigorousIntensityMinutes")),
        "intensity_goal_min": to_int(r.get("intensityMinutesGoal")),
        "resting_hr": to_int(r.get("restingHeartRate")),
        "rhr_7d_avg": to_int(r.get("lastSevenDaysAvgRestingHeartRate")),
        "min_hr": to_int(r.get("minHeartRate")),
        "max_hr": to_int(r.get("maxHeartRate")),
        "avg_stress": to_int(r.get("averageStressLevel")),
        "max_stress": to_int(r.get("maxStressLevel")),
        "stress_qualifier": r.get("stressQualifier"),
        "rest_stress_s": to_int(r.get("restStressDuration")),
        "low_stress_s": to_int(r.get("lowStressDuration")),
        "medium_stress_s": to_int(r.get("mediumStressDuration")),
        "high_stress_s": to_int(r.get("highStressDuration")),
        "activity_stress_s": to_int(r.get("activityStressDuration")),
        "uncategorized_stress_s": to_int(r.get("uncategorizedStressDuration")),
        "bb_charged": to_int(r.get("bodyBatteryChargedValue")),
        "bb_drained": to_int(r.get("bodyBatteryDrainedValue")),
        "bb_high": to_int(r.get("bodyBatteryHighestValue")),
        "bb_low": to_int(r.get("bodyBatteryLowestValue")),
        "bb_most_recent": to_int(r.get("bodyBatteryMostRecentValue")),
        "bb_during_sleep": to_int(r.get("bodyBatteryDuringSleep")),
        "bb_at_wake": to_int(r.get("bodyBatteryAtWakeTime")),
        "avg_spo2": num(r.get("averageSpo2")),
        "lowest_spo2": num(r.get("lowestSpo2")),
        "avg_waking_respiration": num(r.get("avgWakingRespirationValue")),
        "highest_respiration": num(r.get("highestRespirationValue")),
        "lowest_respiration": num(r.get("lowestRespirationValue")),
        "highly_active_s": to_int(r.get("highlyActiveSeconds")),
        "active_s": to_int(r.get("activeSeconds")),
        "sedentary_s": to_int(r.get("sedentarySeconds")),
        "sleeping_s": to_int(r.get("sleepingSeconds")),
    }
    return {"daily_summary": [row]}


# ---------------------------------------------------------------- sleep


def _sleep(raw: Any, day: str) -> Rows:
    dto = get(raw, "dailySleepDTO", default={})
    if not dto or dto.get("sleepTimeSeconds") is None:
        return {}
    date = parse_date(day)
    start, end = parse_ts(dto.get("sleepStartTimestampGMT")), parse_ts(dto.get("sleepEndTimestampGMT"))
    offset = offset_min(dto.get("sleepStartTimestampGMT"), dto.get("sleepStartTimestampLocal"))
    scores = dto.get("sleepScores") or {}
    row = {
        "calendar_date": date,
        "sleep_start_utc": start,
        "sleep_end_utc": end,
        "utc_offset_min": offset,
        "sleep_s": to_int(dto.get("sleepTimeSeconds")),
        "nap_s": to_int(dto.get("napTimeSeconds")),
        "deep_s": to_int(dto.get("deepSleepSeconds")),
        "light_s": to_int(dto.get("lightSleepSeconds")),
        "rem_s": to_int(dto.get("remSleepSeconds")),
        "awake_s": to_int(dto.get("awakeSleepSeconds")),
        "unmeasurable_s": to_int(dto.get("unmeasurableSleepSeconds")),
        "score": to_int(get(scores, "overall", "value")),
        "score_qualifier": get(scores, "overall", "qualifierKey"),
        "deep_pct": to_int(get(scores, "deepPercentage", "value")),
        "light_pct": to_int(get(scores, "lightPercentage", "value")),
        "rem_pct": to_int(get(scores, "remPercentage", "value")),
        "sub_scores": {k: v.get("qualifierKey") for k, v in scores.items() if isinstance(v, dict)} or None,
        "score_feedback": dto.get("sleepScoreFeedback"),
        "score_insight": dto.get("sleepScoreInsight"),
        "avg_respiration": num(dto.get("averageRespirationValue")),
        "lowest_respiration": num(dto.get("lowestRespirationValue")),
        "highest_respiration": num(dto.get("highestRespirationValue")),
        "avg_sleep_stress": num(dto.get("avgSleepStress")),
        "avg_hr": num(dto.get("avgHeartRate")),
        "awake_count": to_int(dto.get("awakeCount")),
        "restless_moments": to_int(raw.get("restlessMomentsCount")),
        "avg_overnight_hrv": num(raw.get("avgOvernightHrv")),
        "hrv_status": raw.get("hrvStatus"),
        "body_battery_change": to_int(raw.get("bodyBatteryChange")),
        "resting_hr": to_int(raw.get("restingHeartRate")),
        "sleep_need_min": to_int(get(dto, "sleepNeed", "actual")),
        "sleep_need_baseline_min": to_int(get(dto, "sleepNeed", "baseline")),
        "sleep_need_feedback": get(dto, "sleepNeed", "feedback"),
        "window_confirmation": dto.get("sleepWindowConfirmationType"),
    }
    rows: Rows = {"sleep": [row]}
    stages = [(parse_ts(seg.get("startGMT")), seg.get("activityLevel")) for seg in raw.get("sleepLevels") or []]
    series = build_series(date, "sleep_stage", stages, offset)
    if series:
        rows["intraday_series"] = [series]
    return rows


# ---------------------------------------------------------------- HRV


def _hrv(raw: Any, day: str) -> Rows:
    s = get(raw, "hrvSummary", default={})
    if not s:
        return {}
    date = parse_date(day)
    rows: Rows = {
        "hrv": [
            {
                "calendar_date": date,
                "last_night_avg": to_int(s.get("lastNightAvg")),
                "last_night_5min_high": to_int(s.get("lastNight5MinHigh")),
                "weekly_avg": to_int(s.get("weeklyAvg")),
                "baseline_low_upper": to_int(get(s, "baseline", "lowUpper")),
                "baseline_balanced_low": to_int(get(s, "baseline", "balancedLow")),
                "baseline_balanced_upper": to_int(get(s, "baseline", "balancedUpper")),
                "status": s.get("status"),
                "feedback_phrase": s.get("feedbackPhrase"),
            }
        ]
    }
    offset = offset_min(raw.get("startTimestampGMT"), raw.get("startTimestampLocal"))
    points = [(parse_ts(r.get("readingTimeGMT")), r.get("hrvValue")) for r in raw.get("hrvReadings") or []]
    series = build_series(date, "hrv", points, offset)
    if series:
        rows["intraday_series"] = [series]
    return rows


# ---------------------------------------------------------------- training readiness


def _fetch_readiness(src: Source, day: str) -> Any:
    return {
        "readings": src.call("get_training_readiness", day),
        "morning": src.call("get_morning_training_readiness", day),
    }


def _readiness_row(r: dict[str, Any], day: str, is_morning: bool) -> dict[str, Any] | None:
    ts = parse_ts(r.get("timestamp"))
    if ts is None:
        return None
    return {
        "reading_utc": ts,
        "calendar_date": parse_date(r.get("calendarDate") or day),
        "utc_offset_min": offset_min(r.get("timestamp"), r.get("timestampLocal")),
        "is_morning": is_morning,
        "score": to_int(r.get("score")),
        "level": r.get("level"),
        "feedback_short": r.get("feedbackShort"),
        "feedback_long": r.get("feedbackLong"),
        "input_context": r.get("inputContext"),
        "sleep_score": to_int(r.get("sleepScore")),
        "sleep_factor_pct": to_int(r.get("sleepScoreFactorPercent")),
        "sleep_factor_feedback": r.get("sleepScoreFactorFeedback"),
        "recovery_time_min": to_int(r.get("recoveryTime")),
        "recovery_factor_pct": to_int(r.get("recoveryTimeFactorPercent")),
        "recovery_factor_feedback": r.get("recoveryTimeFactorFeedback"),
        "acwr_factor_pct": to_int(r.get("acwrFactorPercent")),
        "acwr_factor_feedback": r.get("acwrFactorFeedback"),
        "acute_load": to_int(r.get("acuteLoad")),
        "stress_history_factor_pct": to_int(r.get("stressHistoryFactorPercent")),
        "stress_history_factor_feedback": r.get("stressHistoryFactorFeedback"),
        "hrv_factor_pct": to_int(r.get("hrvFactorPercent")),
        "hrv_factor_feedback": r.get("hrvFactorFeedback"),
        "hrv_weekly_avg": to_int(r.get("hrvWeeklyAverage")),
        "sleep_history_factor_pct": to_int(r.get("sleepHistoryFactorPercent")),
        "sleep_history_factor_feedback": r.get("sleepHistoryFactorFeedback"),
        "valid_sleep": r.get("validSleep") if isinstance(r.get("validSleep"), bool) else None,
    }


def _readiness(raw: Any, day: str) -> Rows:
    readings = raw.get("readings") if isinstance(raw, dict) else None
    morning = raw.get("morning") if isinstance(raw, dict) else None
    morning_ts = parse_ts(morning.get("timestamp")) if isinstance(morning, dict) else None
    by_ts: dict[Any, dict[str, Any]] = {}
    for r in readings if isinstance(readings, list) else []:
        if isinstance(r, dict) and (row := _readiness_row(r, day, False)):
            by_ts[row["reading_utc"]] = row
    if isinstance(morning, dict) and (row := _readiness_row(morning, day, True)):
        by_ts[row["reading_utc"]] = row
    if morning_ts in by_ts:
        by_ts[morning_ts]["is_morning"] = True
    return {"training_readiness": list(by_ts.values())} if by_ts else {}


# ---------------------------------------------------------------- training status (+ VO2max)


def _primary(entries: Any) -> dict[str, Any]:
    """Pick the primary-device entry from Garmin's {deviceId: {...}} maps."""
    if not isinstance(entries, dict) or not entries:
        return {}
    values = [v for v in entries.values() if isinstance(v, dict)]
    return next((v for v in values if v.get("primaryTrainingDevice")), values[0] if values else {})


def _fetch_status(src: Source, day: str) -> Any:
    return {"status": src.call("get_training_status", day), "max_metrics": src.call("get_max_metrics", day)}


def _training_status(raw: Any, day: str) -> Rows:
    status = raw.get("status") if isinstance(raw, dict) else None
    mm = raw.get("max_metrics") if isinstance(raw, dict) else None
    ts = _primary(get(status, "mostRecentTrainingStatus", "latestTrainingStatusData"))
    lb = _primary(get(status, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap"))
    vo2 = get(status, "mostRecentVO2Max", "generic", default={})
    vo2_today = get(mm, 0, "generic", "vo2MaxValue")
    if not ts and not lb and not vo2 and vo2_today is None:
        return {}
    atl = ts.get("acuteTrainingLoadDTO") or {}
    row = {
        "calendar_date": parse_date(day),
        "status_code": to_int(ts.get("trainingStatus")),
        "status_phrase": ts.get("trainingStatusFeedbackPhrase"),
        "sport": ts.get("sport"),
        "fitness_trend": to_int(ts.get("fitnessTrend")),
        "training_paused": ts.get("trainingPaused") if isinstance(ts.get("trainingPaused"), bool) else None,
        "weekly_training_load": num(ts.get("weeklyTrainingLoad")),
        "acute_load": num(atl.get("dailyTrainingLoadAcute")),
        "chronic_load": num(atl.get("dailyTrainingLoadChronic")),
        "chronic_load_min": num(atl.get("minTrainingLoadChronic")),
        "chronic_load_max": num(atl.get("maxTrainingLoadChronic")),
        "acwr": num(atl.get("dailyAcuteChronicWorkloadRatio")),
        "acwr_pct": to_int(atl.get("acwrPercent")),
        "acwr_status": atl.get("acwrStatus"),
        "load_balance_phrase": lb.get("trainingBalanceFeedbackPhrase"),
        "monthly_load_aerobic_low": num(lb.get("monthlyLoadAerobicLow")),
        "monthly_load_aerobic_high": num(lb.get("monthlyLoadAerobicHigh")),
        "monthly_load_anaerobic": num(lb.get("monthlyLoadAnaerobic")),
        "aerobic_low_target_min": to_int(lb.get("monthlyLoadAerobicLowTargetMin")),
        "aerobic_low_target_max": to_int(lb.get("monthlyLoadAerobicLowTargetMax")),
        "aerobic_high_target_min": to_int(lb.get("monthlyLoadAerobicHighTargetMin")),
        "aerobic_high_target_max": to_int(lb.get("monthlyLoadAerobicHighTargetMax")),
        "anaerobic_target_min": to_int(lb.get("monthlyLoadAnaerobicTargetMin")),
        "anaerobic_target_max": to_int(lb.get("monthlyLoadAnaerobicTargetMax")),
        "vo2max": num(vo2_today),
        "vo2max_latest": num(vo2.get("vo2MaxValue")),
        "vo2max_latest_precise": num(vo2.get("vo2MaxPreciseValue")),
        "vo2max_latest_date": parse_date(vo2["calendarDate"]) if vo2.get("calendarDate") else None,
    }
    return {"training_status": [row]}


# ---------------------------------------------------------------- intraday series


def _series_rows(day: str, metric: str, points: list, offset: int | None) -> Rows:
    series = build_series(parse_date(day), metric, points, offset)
    return {"intraday_series": [series]} if series else {}


def _payload_offset(raw: dict[str, Any]) -> int | None:
    return offset_min(raw.get("startTimestampGMT"), raw.get("startTimestampLocal"))


def _descriptor_points(raw: dict[str, Any], values_key: str, descriptors_key: str, ts_key: str, value_key: str):
    """Points from a Garmin values array whose columns are named by a descriptor list."""
    d = raw.get(descriptors_key)
    return array_points(raw.get(values_key), descriptor_index(d, ts_key), descriptor_index(d, value_key))


def _stress(raw: Any, day: str) -> Rows:
    if not isinstance(raw, dict):
        return {}
    offset, date = _payload_offset(raw), parse_date(day)
    stress = _descriptor_points(raw, "stressValuesArray", "stressValueDescriptorsDTOList", "timestamp", "stressLevel")
    bb = _descriptor_points(
        raw, "bodyBatteryValuesArray", "bodyBatteryValueDescriptorsDTOList", "timestamp", "bodyBatteryLevel"
    )
    candidates = (build_series(date, "stress", stress, offset), build_series(date, "body_battery", bb, offset))
    rows = [s for s in candidates if s]
    return {"intraday_series": rows} if rows else {}


def _heart_rate(raw: Any, day: str) -> Rows:
    if not isinstance(raw, dict):
        return {}
    pts = _descriptor_points(raw, "heartRateValues", "heartRateValueDescriptors", "timestamp", "heartrate")
    return _series_rows(day, "hr", pts, _payload_offset(raw))


def _respiration(raw: Any, day: str) -> Rows:
    if not isinstance(raw, dict):
        return {}
    pts = _descriptor_points(
        raw, "respirationValuesArray", "respirationValueDescriptorsDTOList", "timestamp", "respiration"
    )
    return _series_rows(day, "respiration", pts, _payload_offset(raw))


def _steps(raw: Any, day: str) -> Rows:
    if not isinstance(raw, list) or not raw:
        return {}
    pts = [(parse_ts(b.get("startGMT")), b.get("steps")) for b in raw if isinstance(b, dict)]
    first = parse_ts(raw[0].get("startGMT")) if isinstance(raw[0], dict) else None
    # Buckets start at local midnight, so offset = local midnight - first bucket (UTC).
    offset = None
    if first is not None:
        local_midnight = dt.datetime.combine(parse_date(day), dt.time())
        offset = int(round((local_midnight - first.replace(tzinfo=None)).total_seconds() / 60))
    return _series_rows(day, "steps", pts, offset)


def _floors(raw: Any, day: str) -> Rows:
    if not isinstance(raw, dict):
        return {}
    pts = _descriptor_points(raw, "floorValuesArray", "floorsValueDescriptorDTOList", "startTimeGMT", "floorsAscended")
    return _series_rows(day, "floors", pts, _payload_offset(raw))


def _spo2(raw: Any, day: str) -> Rows:
    if not isinstance(raw, dict):
        return {}
    pairs = raw.get("spO2HourlyAverages")
    pts = array_points(pairs, 0, 1) if isinstance(pairs, list) else []
    return _series_rows(day, "spo2", pts, _payload_offset(raw))


def _hydration(raw: Any, day: str) -> Rows:
    if not isinstance(raw, dict) or raw.get("calendarDate") is None:
        return {}
    return {
        "daily_summary": [
            {
                "calendar_date": parse_date(day),
                "hydration_ml": num(raw.get("valueInML")),
                "hydration_goal_ml": num(raw.get("goalInML")),
                "sweat_loss_ml": num(raw.get("sweatLossInML")),
            }
        ]
    }


# ---------------------------------------------------------------- registry


def _simple(method: str):
    return lambda src, day: src.call(method, day)


register(Domain("summary", "daily", _simple("get_user_summary"), _summary, priority=10))
register(Domain("sleep", "daily", _simple("get_sleep_data"), _sleep, priority=10))
register(Domain("hrv", "daily", _simple("get_hrv_data"), _hrv, priority=10))
register(Domain("readiness", "daily", _fetch_readiness, _readiness, priority=10))
register(Domain("training_status", "daily", _fetch_status, _training_status, priority=10))
register(Domain("stress", "daily", _simple("get_stress_data"), _stress, priority=9))
register(Domain("heart_rate", "daily", _simple("get_heart_rates"), _heart_rate, priority=9))
register(Domain("respiration", "daily", _simple("get_respiration_data"), _respiration, priority=8))
register(Domain("steps", "daily", _simple("get_steps_data"), _steps, priority=8))
register(Domain("floors", "daily", _simple("get_floors"), _floors, priority=7))
register(Domain("spo2", "daily", _simple("get_spo2_data"), _spo2, priority=7))
register(Domain("hydration", "daily", _simple("get_hydration_data"), _hydration, priority=7))
# Raw-only for now: stored in full, mapped to tables in M3.
register(Domain("intensity_minutes", "daily", _simple("get_intensity_minutes_data"), priority=5))
register(Domain("rhr", "daily", _simple("get_rhr_day"), priority=5))
register(Domain("body_battery_events", "daily", _simple("get_body_battery_events"), priority=5))
register(Domain("fitness_age", "daily", _simple("get_fitnessage_data"), priority=5))

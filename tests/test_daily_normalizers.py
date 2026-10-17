"""Normalisers against scrubbed real payloads (tests/fixtures/daily, captured 2026-10-15, CEST)."""

import datetime as dt
import json
from pathlib import Path

import pytest

from gds.domains import REGISTRY, daily_domains
from gds.domains._util import offset_min, parse_ts

FIXTURES = Path(__file__).parent / "fixtures" / "daily"
DAY = "2026-10-15"
DATE = dt.date(2026, 10, 15)


def rows(domain: str) -> dict:
    raw = json.loads((FIXTURES / f"{domain}.json").read_text(encoding="utf-8"))
    return REGISTRY[domain].normalize(raw, DAY)


def series(result: dict, metric: str) -> dict:
    return next(s for s in result["intraday_series"] if s["metric"] == metric)


def test_registry_has_all_daily_domains():
    names = {d.name for d in daily_domains()}
    assert {"summary", "sleep", "hrv", "readiness", "training_status", "stress", "heart_rate"} <= names


def test_offset_and_timestamps():
    assert offset_min("2026-10-14T22:00:00.0", "2026-10-15T00:00:00.0") == 120
    assert offset_min(1792013047000, 1792020247000) == 120
    assert parse_ts(1792015200000) == dt.datetime(2026, 10, 14, 22, 0, tzinfo=dt.UTC)
    assert parse_ts("garbage") is None


def test_summary():
    (row,) = rows("summary")["daily_summary"]
    assert row["calendar_date"] == DATE
    assert row["utc_offset_min"] == 120
    assert row["total_steps"] == 2292
    assert row["resting_hr"] == 56
    assert row["avg_stress"] == 24
    assert row["high_stress_s"] == 480
    assert (row["bb_high"], row["bb_low"]) == (67, 25)
    assert row["active_kcal"] == 35.0


def test_sleep():
    result = rows("sleep")
    (row,) = result["sleep"]
    assert row["sleep_s"] == 21900
    assert (row["deep_s"], row["light_s"], row["rem_s"], row["awake_s"]) == (5760, 14820, 1320, 8160)
    assert row["score"] == 53 and row["score_qualifier"] == "POOR"
    assert (row["deep_pct"], row["light_pct"], row["rem_pct"]) == (26, 68, 6)
    assert row["utc_offset_min"] == 120
    assert row["sleep_start_utc"] == dt.datetime(2026, 10, 14, 21, 24, 7, tzinfo=dt.UTC)
    assert row["avg_overnight_hrv"] == 44.0 and row["hrv_status"] == "BALANCED"
    assert row["body_battery_change"] == 43
    assert row["sub_scores"]["remPercentage"] == "POOR"
    stages = series(result, "sleep_stage")
    assert stages["vals"][:2] == [1.0, 0.0]
    assert stages["ts_offsets_s"][0] == 0


def test_hrv():
    result = rows("hrv")
    (row,) = result["hrv"]
    assert (row["last_night_avg"], row["weekly_avg"], row["status"]) == (44, 41, "BALANCED")
    assert row["baseline_balanced_low"] == 41
    hrv = series(result, "hrv")
    assert hrv["vals"][0] == 34.0 and hrv["utc_offset_min"] == 120


def test_readiness_marks_morning_reading():
    readings = rows("readiness")["training_readiness"]
    morning = [r for r in readings if r["is_morning"]]
    assert len(morning) == 1
    assert morning[0]["score"] == 21 and morning[0]["level"] == "POOR"
    latest = max(readings, key=lambda r: r["reading_utc"])
    assert latest["score"] == 25 and latest["level"] == "LOW"
    assert latest["sleep_factor_pct"] == 36 and latest["hrv_factor_pct"] == 71
    assert all(r["utc_offset_min"] == 120 for r in readings)


def test_training_status():
    (row,) = rows("training_status")["training_status"]
    assert row["status_phrase"] == "PRODUCTIVE_2"
    assert (row["acute_load"], row["chronic_load"], row["acwr"]) == (346, 281, 1.2)
    assert (row["acwr_pct"], row["acwr_status"]) == (52, "OPTIMAL")
    assert row["load_balance_phrase"] == "BALANCED"
    assert row["vo2max"] is None  # no max-metrics reading that day
    assert row["vo2max_latest"] == 45.0 and row["vo2max_latest_date"] == dt.date(2026, 10, 14)


@pytest.mark.parametrize(
    ("domain", "metric", "first_value"),
    [("stress", "stress", 26.0), ("stress", "body_battery", 25.0), ("heart_rate", "hr", 60.0),
     ("respiration", "respiration", 16.0), ("steps", "steps", 0.0), ("floors", "floors", 0.0)],
)
def test_intraday_series(domain, metric, first_value):
    s = series(rows(domain), metric)
    assert s["calendar_date"] == DATE
    assert s["utc_offset_min"] == 120
    assert s["vals"][0] == first_value
    assert len(s["vals"]) == len(s["ts_offsets_s"])
    assert s["ts_offsets_s"] == sorted(s["ts_offsets_s"])


def test_spo2_without_readings_is_empty():
    assert rows("spo2") == {}


def test_hydration_partial_summary_row():
    (row,) = rows("hydration")["daily_summary"]
    assert set(row) == {"calendar_date", "hydration_ml", "hydration_goal_ml", "sweat_loss_ml"}
    assert row["hydration_goal_ml"] == 2000.0


@pytest.mark.parametrize("domain", [d.name for d in daily_domains() if d.normalize])
def test_normalizers_tolerate_empty_payloads(domain):
    for raw in (None, {}, [], {"unexpected": True}):
        assert isinstance(REGISTRY[domain].normalize(raw, DAY), dict)

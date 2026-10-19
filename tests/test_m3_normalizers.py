"""M3 normalisers (range, snapshot, entity) against scrubbed real payloads."""

import datetime as dt
import json
from pathlib import Path

import pytest

from gds.domains import REGISTRY, domains_in
from gds.domains.ranges import month_bounds
from gds.engine import month_key, months_between

M3 = Path(__file__).parent / "fixtures" / "m3"
DAILY = Path(__file__).parent / "fixtures" / "daily"


def norm(domain: str, key: str, fixture: str | None = None, folder: Path = M3) -> dict:
    raw = json.loads((folder / f"{fixture or domain}.json").read_text(encoding="utf-8"))
    return REGISTRY[domain].normalize(raw, key)


def test_month_helpers():
    assert month_key(dt.date(2026, 1, 15), -1) == "2025-12"
    assert month_key(dt.date(2026, 12, 1), 2) == "2027-02"
    assert months_between(dt.date(2025, 11, 20), "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert month_bounds("2024-02", cap_today=False) == (dt.date(2024, 2, 1), dt.date(2024, 2, 29))


def test_registry_scopes_and_refresh():
    assert len(domains_in("range")) >= 50 and len(domains_in("snapshot")) >= 90
    for d in domains_in("range") + domains_in("snapshot"):
        assert d.refresh_hours, d.name
    assert REGISTRY["calendar_month"].months_ahead == 2


def test_weigh_ins_convert_grams():
    rows = norm("body_composition", "2025-01")["weigh_ins"]
    assert len(rows) == 3
    latest = max(rows, key=lambda r: r["calendar_date"])
    assert latest["weight_kg"] == 107.0 and latest["source_type"] == "MANUAL"
    assert latest["calendar_date"] == dt.date(2026, 8, 29)


def test_blood_pressure_empty_month():
    # No readings, but the month is a full listing: anything stored for it was deleted in Garmin.
    assert norm("blood_pressure", "2026-09") == {
        "blood_pressure": [],
        "_replace": [{"table": "blood_pressure",
                      "between": ("calendar_date", dt.date(2026, 9, 1), dt.date(2026, 9, 30))}],
    }


def test_weigh_ins_replace_their_month():
    result = norm("body_composition", "2025-01")
    assert result["_replace"] == [{"table": "weigh_ins",
                                   "between": ("calendar_date", dt.date(2025, 1, 1), dt.date(2025, 1, 31))}]
    assert REGISTRY["body_composition"].normalize({"unexpected": 1}, "2025-01") == {}  # not a listing: keep rows


def test_vo2max_history():
    rows = norm("vo2max_history", "2026-09")["performance_metrics"]
    first = min(rows, key=lambda r: r["calendar_date"])
    assert first["metric"] == "vo2max_running" and first["value"] == pytest.approx(44.1)


def test_calendar_keeps_own_month_and_converts_units():
    result = norm("calendar_month", "2026-10")
    rows = result["calendar_items"]
    assert rows and all(r["month"] == "2026-10" and r["item_date"].month == 10 for r in rows)
    naps = [r for r in rows if r["item_type"] == "nap"]
    assert naps and naps[0]["duration_s"] < 4 * 3600  # seconds, not ms
    assert result["_replace"] == [{"table": "calendar_items", "scope": {"month": "2026-10"}}]


def test_calendar_activity_units():
    from gds.domains.ranges import _calendar

    raw = {"calendarItems": [{"id": 1, "itemType": "activity", "date": "2026-10-01",
                              "duration": 1777996, "distance": 401519}]}
    (row,) = _calendar(raw, "2026-10")["calendar_items"]
    assert row["duration_s"] == pytest.approx(1777.996) and row["distance_m"] == pytest.approx(4015.19)


@pytest.mark.parametrize(
    ("domain", "kind", "id_field"),
    [("devices", "device", "deviceId"), ("gear", "gear", "uuid"), ("personal_records", "personal_record", "typeId"),
     ("goals", "goal", "userGoalPk"), ("badges", "badge", "badgeId"), ("workouts", "workout", "workoutId"),
     ("training_plans", "training_plan", "trainingPlanId")],
)
def test_entity_lists_replace_by_kind(domain, kind, id_field):
    result = norm(domain, "latest")
    assert result["entities"] and all(e["kind"] == kind and e["entity_id"] for e in result["entities"])
    assert result["_replace"] == [{"table": "entities", "scope": {"kind": kind}}]


def test_children_units_for_gear_and_workouts():
    gear = norm("gear", "latest")["work_units"]
    assert gear[0]["domain"] == "gear_stats" and gear[0]["requeue"]
    workouts = norm("workouts", "latest")["work_units"]
    assert {u["domain"] for u in workouts} == {"workout_detail"}


def test_race_predictions_seconds():
    rows = {r["metric"]: r["value"] for r in norm("race_predictions", "latest")["performance_metrics"]}
    assert rows == {"race_5k_s": 1668, "race_10k_s": 3548, "race_half_marathon_s": 8061, "race_marathon_s": 17972}


def test_lactate_threshold_and_ftp():
    rows = {r["metric"]: r for r in norm("lactate_threshold", "latest")["performance_metrics"]}
    assert rows["lactate_threshold_hr_bpm"]["value"] == 176
    assert rows["lactate_threshold_hr_bpm"]["calendar_date"] == dt.date(2026, 9, 17)
    assert "lactate_threshold_speed_raw" in rows
    assert rows["ftp_running_w"]["value"] == 461


def test_entity_details():
    (w,) = norm("workout_detail", "1703113674")["entities"]
    assert w["kind"] == "workout_detail" and w["name"] == "Long run" and w["data"]["workoutSegments"]
    (g,) = norm("gear_stats", "75e7fa0a")["entities"]
    assert g["kind"] == "gear_stats" and g["data"]["totalActivities"] == 21


def test_body_battery_events_and_fitness_age():
    events = norm("body_battery_events", "2026-10-15", folder=DAILY)["body_battery_events"]
    sleep = next(e for e in events if e["event_type"] == "SLEEP")
    assert sleep["impact"] == 43 and sleep["utc_offset_min"] == 120 and sleep["duration_s"] == 30060
    fa = {r["metric"]: r["value"] for r in norm("fitness_age", "2026-10-15", folder=DAILY)["performance_metrics"]}
    assert fa["fitness_age"] == pytest.approx(34.33, abs=0.01)


@pytest.mark.parametrize("domain", [d.name for d in domains_in("range") + domains_in("snapshot")
                                    + domains_in("entity") if d.normalize])
def test_m3_normalizers_tolerate_empty_payloads(domain):
    for raw in (None, {}, [], {"unexpected": True}):
        assert isinstance(REGISTRY[domain].normalize(raw, "2026-10"), dict)

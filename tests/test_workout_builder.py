"""Workout spec → Garmin JSON (W-2). Expected shapes come from workouts created in Garmin
Connect itself (`workout_detail` entities in the real account)."""

import pytest

from gds import workout_builder as wb
from gds.tools import ToolError

# Steps of a real "Hard / Intervals 4x4" workout (fields the builder controls).
REAL_4X4 = [
    {"type": "ExecutableStepDTO", "stepOrder": 1, "stepType": {"stepTypeKey": "warmup"},
     "endCondition": {"conditionTypeKey": "time"}, "endConditionValue": 180.0,
     "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}},
    {"type": "RepeatGroupDTO", "stepOrder": 2, "stepType": {"stepTypeKey": "repeat"}, "numberOfIterations": 4,
     "endCondition": {"conditionTypeKey": "iterations"}, "endConditionValue": 4.0, "skipLastRestStep": True,
     "smartRepeat": False, "workoutSteps": [
         {"type": "ExecutableStepDTO", "stepOrder": 3, "stepType": {"stepTypeKey": "interval"},
          "endCondition": {"conditionTypeKey": "time"}, "endConditionValue": 240.0,
          "targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"},
          "targetValueOne": 3.2258065, "targetValueTwo": 3.030303},
         {"type": "ExecutableStepDTO", "stepOrder": 4, "stepType": {"stepTypeKey": "recovery"},
          "endCondition": {"conditionTypeKey": "time"}, "endConditionValue": 180.0,
          "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}},
     ]},
    {"type": "ExecutableStepDTO", "stepOrder": 5, "stepType": {"stepTypeKey": "cooldown"},
     "endCondition": {"conditionTypeKey": "time"}, "endConditionValue": 180.0,
     "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}},
]


def _subset(expected, actual, path="$"):
    """Every key in `expected` is in `actual` with the same value (floats to 1e-6)."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        for k, v in expected.items():
            assert k in actual, f"{path}.{k} missing"
            _subset(v, actual[k], f"{path}.{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), path
        for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
            _subset(e, a, f"{path}[{i}]")
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, abs=1e-6), path
    else:
        assert actual == expected, path


def test_matches_a_workout_made_in_garmin_connect():
    workout, summary = wb.build({"name": "Hard / Intervals 4x4", "steps": [
        {"type": "warmup", "time": "3:00"},
        {"repeat": 4, "skip_last_rest": True, "steps": [
            {"type": "run", "time": "4:00", "target": {"pace": "5:10-5:30"}},
            {"type": "recover", "time": 180}]},
        {"type": "cooldown", "time": "3:00"},
    ]})
    assert workout["sportType"]["sportTypeKey"] == "running"
    _subset(REAL_4X4, workout["workoutSegments"][0]["workoutSteps"])
    assert workout["estimatedDurationInSecs"] == 180 + 4 * (240 + 180) + 180
    assert "Repeat 4× (skip last rest)" in summary and "@ 5:10–5:30 /km" in summary


def test_distance_units_targets_and_estimates():
    workout, summary = wb.build({"name": "6x800", "steps": [
        {"type": "warmup", "distance_km": 2, "target": {"hr_zone": 2}},
        {"repeat": 6, "steps": [{"type": "run", "distance_m": 800, "target": {"pace": "4:30"}},
                                {"type": "recover", "time": "2:00", "note": "jog"}]},
        {"type": "cooldown"},
    ]})
    warm, rep, cool = workout["workoutSegments"][0]["workoutSteps"]
    assert warm["endConditionValue"] == 2000.0 and warm["preferredEndConditionUnit"]["unitKey"] == "kilometer"
    assert warm["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone" and warm["zoneNumber"] == 2
    run = rep["workoutSteps"][0]
    assert run["preferredEndConditionUnit"]["unitKey"] == "meter"
    assert run["targetValueOne"] == pytest.approx(1000 / 265, abs=1e-6)  # faster limit first, ±5 s
    assert run["targetValueTwo"] == pytest.approx(1000 / 275, abs=1e-6)
    assert rep["workoutSteps"][1]["description"] == "jog"
    assert cool["endCondition"]["conditionTypeKey"] == "lap.button"
    assert workout["estimatedDistanceInMeters"] == 2000 + 6 * 800
    assert workout["estimatedDurationInSecs"] == round(6 * 120 + 6 * 800 / ((1000 / 265 + 1000 / 275) / 2))
    assert "Cool-down until lap button" in summary


def test_step_orders_are_unique_and_in_traversal_order():
    workout, _ = wb.build({"name": "x", "sport": "strength", "steps": [
        {"type": "warmup", "time": 300},
        {"exercise": "Barbell Bench Press", "sets": 4, "reps": 8, "weight_kg": 60, "rest_s": 90},
        {"exercise": "Plank", "time": "0:45", "rest_s": 30},
        {"exercise": "Push Up", "reps": 15},
    ]})
    orders = []

    def walk(steps):
        for s in steps:
            orders.append(s["stepOrder"])
            walk(s.get("workoutSteps", []))
    walk(workout["workoutSegments"][0]["workoutSteps"])
    assert orders == list(range(1, len(orders) + 1))


def test_strength_sets():
    workout, summary = wb.build({"name": "Upper", "sport": "strength", "steps": [
        {"exercise": "barbell bench press", "sets": 4, "reps": 8, "weight_kg": 60, "rest_s": "1:30"},
        {"exercise": "Plank", "sets": 1, "time": 45},
    ]})
    assert workout["sportType"] == {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5}
    bench, plank = workout["workoutSegments"][0]["workoutSteps"]
    assert bench["type"] == "RepeatGroupDTO" and bench["numberOfIterations"] == 4
    ex, rest = bench["workoutSteps"]
    assert (ex["category"], ex["exerciseName"]) == ("BENCH_PRESS", "BARBELL_BENCH_PRESS")
    assert ex["endCondition"]["conditionTypeKey"] == "reps" and ex["endConditionValue"] == 8.0
    assert ex["weightValue"] == 60000.0 and ex["weightUnit"]["unitKey"] == "kilogram"  # grams
    assert rest["stepType"]["stepTypeKey"] == "rest" and rest["endConditionValue"] == 90.0
    assert plank["type"] == "ExecutableStepDTO" and plank["category"] == "PLANK"
    assert "Barbell Bench Press: 4 × 8 reps @ 60 kg, rest 1:30" in summary


def test_unknown_exercise_suggests_names():
    with pytest.raises(ToolError, match="Did you mean:.*Bench Press"):
        wb.build({"name": "x", "sport": "strength", "steps": [{"exercise": "Bench Pres", "reps": 5}]})


@pytest.mark.parametrize("spec, message", [
    ({"name": "x", "steps": [{"type": "sprint", "time": 30}]}, "Step type"),
    ({"name": "x", "steps": [{"type": "run", "time": 30, "target": {"pace": "fast"}}]}, "pace"),
    ({"name": "x", "steps": [{"type": "run", "time": 30, "distance_m": 400}]}, "one condition"),
    ({"name": "x", "steps": [{"type": "run", "target": {"hr_zone": 7}}]}, "zone number"),
    ({"name": "x", "steps": [{"exercise": "Plank", "time": 30}]}, "strength"),
    ({"name": "x", "steps": []}, "non-empty"),
    ({"name": "", "steps": [{"type": "run"}]}, "name"),
    ({"name": "x", "laps": 3, "steps": [{"type": "run"}]}, "Unknown spec fields"),
    ({"name": "x", "steps": [{"repeat": 0, "steps": [{"type": "run"}]}]}, "repeat"),
])
def test_rejects_bad_specs(spec, message):
    with pytest.raises(ToolError, match=message):
        wb.build(spec)


@pytest.mark.parametrize("text, seconds", [("10:00", 600), ("1:05:00", 3900), (90, 90), ("45s", 45)])
def test_parse_seconds(text, seconds):
    assert wb.parse_seconds(text) == seconds


def test_pace_in_miles():
    fast, slow, label = wb.parse_pace("7:00-7:30/mi")
    assert fast == pytest.approx(1609.344 / 420) and slow == pytest.approx(1609.344 / 450)
    assert label == "7:00–7:30 /mi"

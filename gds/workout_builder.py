"""Workout spec → Garmin workout JSON (design §8.3, W-2).

Claude writes a small, documented spec (SPEC_DOC); `build` validates it and returns the
JSON Garmin's workout service accepts plus a human-readable summary for the confirmation
step. The output mirrors workouts created in Garmin Connect itself (the `workout_detail`
entities): pace targets are m/s with the faster limit first, HR ranges in bpm, zones by
number, distances in metres with the display unit Garmin would pick.
"""

import difflib
import re
from typing import Any

from garminconnect import exercises

from gds.tools import ToolError

SPEC_DOC = """Workout spec (JSON object):
  name         required, shown on the watch (max 80 chars)
  sport        running (default) | cycling | strength | cardio | hiit | yoga | pilates | mobility | other
  description  optional note
  steps        list of steps, in order

Endurance step: {"type": warmup|run|recover|rest|cooldown|other, <end>, "target": {...}?, "note": str?}
  end: "time": "10:00" / "1:05:00" / 600 (seconds)  |  "distance_m": 800  |  "distance_km": 5
       (no time or distance = until the lap button is pressed)
  target (one of): {"pace": "4:20-4:40"} or {"pace": "4:30"} (±5 s; per km, add "/mi" for miles)
                   {"hr_zone": 2}  {"hr": "140-155"}  {"power_zone": 3}  {"power": "200-250"}
                   {"cadence": "170-180"}  {"speed_kph": "28-32"}
Repeat: {"repeat": 6, "steps": [...], "skip_last_rest": true?}
Strength step (any sport except running/cycling):
  {"exercise": "Barbell Bench Press", "sets": 4, "reps": 8, "weight_kg": 60?, "rest_s": 90?, "note": str?}
  {"exercise": "Plank", "sets": 3, "time": "0:45", "rest_s": 60}
  Exercise names come from Garmin's catalog; unknown names are rejected with suggestions.

Example: {"name": "6x800 @ 4:30", "steps": [
  {"type": "warmup", "time": "15:00", "target": {"hr_zone": 2}},
  {"repeat": 6, "skip_last_rest": true, "steps": [
     {"type": "run", "distance_m": 800, "target": {"pace": "4:25-4:35"}},
     {"type": "recover", "time": "2:00"}]},
  {"type": "cooldown", "time": "10:00"}]}"""

SPORTS = {
    "running": (1, "running"), "cycling": (2, "cycling"), "other": (3, "other"),
    "strength": (5, "strength_training"), "strength_training": (5, "strength_training"),
    "cardio": (6, "cardio_training"), "cardio_training": (6, "cardio_training"), "yoga": (7, "yoga"),
    "pilates": (8, "pilates"), "hiit": (9, "hiit"), "mobility": (11, "mobility"),
}
ENDURANCE_SPORTS = {"running", "cycling"}

STEP_TYPES = {
    "warmup": (1, "warmup"), "warm_up": (1, "warmup"), "cooldown": (2, "cooldown"), "cool_down": (2, "cooldown"),
    "run": (3, "interval"), "interval": (3, "interval"), "work": (3, "interval"), "ride": (3, "interval"),
    "recover": (4, "recovery"), "recovery": (4, "recovery"), "rest": (5, "rest"), "other": (7, "other"),
}
STEP_LABELS = {"warmup": "Warm-up", "cooldown": "Cool-down", "interval": "Run", "recovery": "Recover",
               "rest": "Rest", "other": "Other"}

# (id, key) as in Garmin's workout dictionary; displayOrder equals the id in real payloads.
COND_LAP, COND_TIME, COND_DISTANCE, COND_ITERATIONS, COND_REPS = (
    (1, "lap.button"), (2, "time"), (3, "distance"), (7, "iterations"), (10, "reps"))
TARGETS = {
    "none": (1, "no.target"), "power": (2, "power.zone"), "cadence": (3, "cadence"),
    "hr": (4, "heart.rate.zone"), "speed": (5, "speed.zone"), "pace": (6, "pace.zone"),
}
UNIT_KM = {"unitId": 2, "unitKey": "kilometer", "factor": 100000.0}
UNIT_M = {"unitId": 1, "unitKey": "meter", "factor": 100.0}
WEIGHT_KG = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}

MAX_STEPS = 100


def _cond(c: tuple[int, str], displayable: bool = True) -> dict[str, Any]:
    return {"conditionTypeId": c[0], "conditionTypeKey": c[1], "displayOrder": c[0], "displayable": displayable}


def _target_type(key: str) -> dict[str, Any]:
    tid, tkey = TARGETS[key]
    return {"workoutTargetTypeId": tid, "workoutTargetTypeKey": tkey, "displayOrder": tid}


# ---------------------------------------------------------------- parsing helpers


def parse_seconds(value: Any, field: str = "time") -> float:
    """600 / 600.0 / "600" / "90s" / "10:00" / "1:05:00" → seconds."""
    if isinstance(value, bool):
        raise ToolError(f"{field}: expected a duration, got {value!r}")
    if isinstance(value, int | float):
        secs = float(value)
    else:
        text = str(value).strip().lower().removesuffix("s").strip()
        parts = text.split(":")
        try:
            if len(parts) == 1:
                secs = float(parts[0])
            elif len(parts) in (2, 3):
                nums = [float(p) for p in parts]
                secs = sum(n * 60 ** i for i, n in enumerate(reversed(nums)))
            else:
                raise ValueError
        except ValueError as e:
            raise ToolError(f"{field}: use seconds or mm:ss / h:mm:ss, got {value!r}") from e
    if not 0 < secs <= 24 * 3600:
        raise ToolError(f"{field}: must be between 1 s and 24 h, got {value!r}")
    return secs


def _fmt_secs(secs: float) -> str:
    s = int(round(secs))
    h, rem = divmod(s, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def _range(value: Any, field: str, lo: float, hi: float) -> tuple[float, float]:
    """"140-155" / [140, 155] / 150 → (low, high) within [lo, hi]."""
    if isinstance(value, list | tuple) and len(value) == 2:
        a, b = value
    elif isinstance(value, int | float) and not isinstance(value, bool):
        a = b = value
    else:
        m = re.fullmatch(r"\s*([\d.]+)\s*(?:-|–|to)\s*([\d.]+)\s*", str(value))
        if not m:
            raise ToolError(f"target {field}: use 'low-high', got {value!r}")
        a, b = m.groups()
    try:
        a, b = sorted((float(a), float(b)))
    except (TypeError, ValueError) as e:
        raise ToolError(f"target {field}: numbers expected, got {value!r}") from e
    if a < lo or b > hi:
        raise ToolError(f"target {field}: {a:g}-{b:g} is outside {lo:g}-{hi:g}")
    return a, b


def _pace_secs(text: str) -> float:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text)
    if not m:
        raise ToolError(f"pace: use m:ss per km (e.g. '4:30' or '4:20-4:40'), got {text!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def parse_pace(value: Any) -> tuple[float, float, str]:
    """"4:20-4:40" / "4:30" (±5 s) / "7:30/mi" → (fast m/s, slow m/s, label)."""
    text = str(value).strip().lower()
    per_m = 1000.0
    for suffix, metres in (("/mi", 1609.344), ("/km", 1000.0)):
        if text.endswith(suffix):
            text, per_m = text.removesuffix(suffix), metres
    parts = re.split(r"\s*(?:-|–|to)\s*", text)
    if len(parts) == 1:
        mid = _pace_secs(parts[0])
        fast, slow = mid - 5, mid + 5
    elif len(parts) == 2:
        fast, slow = sorted(_pace_secs(p) for p in parts)
    else:
        raise ToolError(f"pace: use '4:30' or '4:20-4:40', got {value!r}")
    if not 120 <= fast <= slow <= 1200 * (per_m / 1000):
        raise ToolError(f"pace {value!r} is out of range")
    unit = "/mi" if per_m != 1000.0 else "/km"
    label = f"{_fmt_secs(fast)}–{_fmt_secs(slow)} {unit}"
    return per_m / fast, per_m / slow, label


def _target(spec: Any, sport: str) -> tuple[dict[str, Any], str]:
    """Target spec → step fields + a label."""
    none = {"targetType": _target_type("none")}
    if spec in (None, {}, "none"):
        return none, ""
    if not isinstance(spec, dict) or len(spec) != 1:
        raise ToolError(f"target: one of pace / hr_zone / hr / power_zone / power / cadence / speed_kph, got {spec!r}")
    (kind, value), = spec.items()
    if kind == "pace":
        fast, slow, label = parse_pace(value)
        return {"targetType": _target_type("pace"), "targetValueOne": round(fast, 7),
                "targetValueTwo": round(slow, 7)}, f"@ {label}"
    if kind in ("hr_zone", "power_zone"):
        top = 5 if kind == "hr_zone" else 7
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
            raise ToolError(f"target {kind}: a zone number 1-{top}, got {value!r}")
        key, label = ("hr", "HR") if kind == "hr_zone" else ("power", "power")
        return {"targetType": _target_type(key), "zoneNumber": value}, f"in {label} zone {value}"
    if kind == "hr":
        a, b = _range(value, "hr", 40, 230)
        return {"targetType": _target_type("hr"), "targetValueOne": a, "targetValueTwo": b}, f"@ {a:g}–{b:g} bpm"
    if kind == "power":
        a, b = _range(value, "power", 1, 2500)
        return {"targetType": _target_type("power"), "targetValueOne": a, "targetValueTwo": b}, f"@ {a:g}–{b:g} W"
    if kind == "cadence":
        a, b = _range(value, "cadence", 10, 250)
        unit = "rpm" if sport == "cycling" else "spm"
        fields = {"targetType": _target_type("cadence"), "targetValueOne": a, "targetValueTwo": b}
        return fields, f"@ {a:g}–{b:g} {unit}"
    if kind == "speed_kph":
        a, b = _range(value, "speed_kph", 1, 80)
        return {"targetType": _target_type("speed"), "targetValueOne": round(a / 3.6, 7),
                "targetValueTwo": round(b / 3.6, 7)}, f"@ {a:g}–{b:g} km/h"
    raise ToolError(f"target: unknown kind {kind!r} (pace, hr_zone, hr, power_zone, power, cadence, speed_kph)")


def resolve_exercise(name: Any) -> dict[str, str]:
    """Catalog lookup: exact display name, case-insensitive name, or CATEGORY/EXERCISE."""
    if not isinstance(name, str) or not name.strip():
        raise ToolError("exercise: a name from Garmin's exercise catalog is required")
    text = name.strip()
    squash = re.sub(r"[^a-z0-9]", "", text.lower())  # "Push Up" == "Push-up"
    hit = exercises.resolve(text) or next(
        (e for e in exercises.EXERCISES if re.sub(r"[^a-z0-9]", "", e["name"].lower()) == squash), None)
    if hit is None and "/" in text:
        cat, _, ex = text.upper().partition("/")
        hit = next((e for e in exercises.EXERCISES if e["category"] == cat and e["exercise"] == ex), None)
    if hit:
        return hit
    names = [e["name"] for e in exercises.EXERCISES]
    lowered = {n.lower(): n for n in names}
    close = [lowered[n] for n in difflib.get_close_matches(text.lower(), lowered, n=5, cutoff=0.5)]
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2]
    contains = [n for n in names if words and all(w in n.lower() for w in words)]
    suggestions = list(dict.fromkeys(close + contains))[:8]
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ToolError(f"Unknown exercise {name!r}.{hint}")


# ---------------------------------------------------------------- building


class _Builder:
    def __init__(self, sport: str):
        self.sport = sport
        self.order = 0
        self.lines: list[str] = []
        self.est_s = 0.0
        self.distance_m = 0.0

    def next_order(self) -> int:
        self.order += 1
        if self.order > MAX_STEPS:
            raise ToolError(f"Too many steps (max {MAX_STEPS}).")
        return self.order

    def steps(self, specs: Any, depth: int, mult: int) -> list[dict[str, Any]]:
        if not isinstance(specs, list) or not specs:
            raise ToolError("steps: a non-empty list is required")
        out: list[dict[str, Any]] = []
        for spec in specs:
            if not isinstance(spec, dict):
                raise ToolError(f"Each step must be an object, got {spec!r}")
            if "repeat" in spec:
                out.append(self.repeat(spec, depth, mult))
            elif "exercise" in spec:
                out.append(self.exercise(spec, depth, mult))
            else:
                out.append(self.endurance(spec, depth, mult))
        return out

    def repeat(self, spec: dict[str, Any], depth: int, mult: int) -> dict[str, Any]:
        n = spec["repeat"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 99:
            raise ToolError(f"repeat: a count 1-99 is required, got {n!r}")
        if depth >= 2:
            raise ToolError("Repeats can be nested at most two deep.")
        order = self.next_order()
        skip = bool(spec.get("skip_last_rest", False))
        self.lines.append(f"{'  ' * depth}Repeat {n}×{' (skip last rest)' if skip else ''}:")
        inner = self.steps(spec.get("steps"), depth + 1, mult * n)
        return {
            "type": "RepeatGroupDTO", "stepOrder": order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "numberOfIterations": n, "endCondition": _cond(COND_ITERATIONS, False), "endConditionValue": float(n),
            "smartRepeat": False, "skipLastRestStep": skip, "workoutSteps": inner,
        }

    def _end(self, spec: dict[str, Any], allow_reps: bool = False) -> tuple[dict[str, Any], str, float | None]:
        given = [k for k in ("time", "distance_m", "distance_km", "reps") if spec.get(k) is not None]
        if len(given) > 1:
            raise ToolError(f"A step ends on one condition only; got {', '.join(given)}")
        if spec.get("time") is not None:
            secs = parse_seconds(spec["time"])
            return {"endCondition": _cond(COND_TIME), "endConditionValue": secs}, _fmt_secs(secs), secs
        if spec.get("distance_m") is not None or spec.get("distance_km") is not None:
            raw = spec.get("distance_m")
            if raw is None:
                km = spec["distance_km"]
                raw = km * 1000 if isinstance(km, int | float) and not isinstance(km, bool) else km
            if isinstance(raw, bool) or not isinstance(raw, int | float) or not 1 <= raw <= 300_000:
                raise ToolError(f"distance: metres between 1 and 300000, got {raw!r}")
            metres = float(raw)
            unit = UNIT_KM if metres >= 1000 and metres % 100 == 0 else UNIT_M
            label = f"{metres / 1000:g} km" if unit is UNIT_KM else f"{metres:g} m"
            return ({"endCondition": _cond(COND_DISTANCE), "endConditionValue": metres,
                     "preferredEndConditionUnit": dict(unit)}, label, None)
        if allow_reps and spec.get("reps") is not None:
            reps = spec["reps"]
            if isinstance(reps, bool) or not isinstance(reps, int) or not 1 <= reps <= 500:
                raise ToolError(f"reps: 1-500, got {reps!r}")
            return {"endCondition": _cond(COND_REPS), "endConditionValue": float(reps)}, f"{reps} reps", None
        return {"endCondition": _cond(COND_LAP), "endConditionValue": 0.0}, "until lap button", None

    def endurance(self, spec: dict[str, Any], depth: int, mult: int) -> dict[str, Any]:
        kind = str(spec.get("type", "")).lower()
        if kind not in STEP_TYPES:
            raise ToolError(f"Step type {spec.get('type')!r}: use warmup, run, recover, rest, cooldown or other "
                            "(or 'repeat' / 'exercise' steps)")
        tid, tkey = STEP_TYPES[kind]
        end, end_label, secs = self._end(spec)
        target, target_label = _target(spec.get("target"), self.sport)
        step = {"type": "ExecutableStepDTO", "stepOrder": self.next_order(),
                "stepType": {"stepTypeId": tid, "stepTypeKey": tkey, "displayOrder": tid}, **end, **target}
        if spec.get("note"):
            step["description"] = str(spec["note"])[:512]
        label = STEP_LABELS[tkey] if not (tkey == "interval" and self.sport == "cycling") else "Ride"
        self.lines.append(f"{'  ' * depth}{label} {end_label}{' ' + target_label if target_label else ''}"
                          f"{' — ' + step['description'] if 'description' in step else ''}")
        self._estimate(end, secs, target, mult)
        return step

    def _estimate(self, end: dict[str, Any], secs: float | None, target: dict[str, Any], mult: int) -> None:
        if secs:
            self.est_s += secs * mult
        elif end["endCondition"]["conditionTypeKey"] == "distance":
            self.distance_m += end["endConditionValue"] * mult
            if target.get("targetType", {}).get("workoutTargetTypeKey") in ("pace.zone", "speed.zone"):
                mps = (target["targetValueOne"] + target["targetValueTwo"]) / 2
                self.est_s += end["endConditionValue"] / mps * mult

    def exercise(self, spec: dict[str, Any], depth: int, mult: int) -> dict[str, Any]:
        if self.sport in ENDURANCE_SPORTS:
            raise ToolError("Exercise steps need a strength/cardio/hiit/yoga/pilates/mobility/other workout.")
        ex = resolve_exercise(spec.get("exercise"))
        sets = spec.get("sets", 1)
        if isinstance(sets, bool) or not isinstance(sets, int) or not 1 <= sets <= 50:
            raise ToolError(f"sets: 1-50, got {sets!r}")
        if spec.get("time") is None and spec.get("reps") is None:
            raise ToolError(f"{ex['name']}: give reps or time per set")
        rest = parse_seconds(spec["rest_s"], "rest_s") if spec.get("rest_s") not in (None, 0) else None
        weight = spec.get("weight_kg")
        if weight is not None and (isinstance(weight, bool) or not isinstance(weight, int | float)
                                   or not 0 < weight <= 500):
            raise ToolError(f"weight_kg: 0-500, got {weight!r}")

        # A rest belongs to its exercise, so even one set + rest becomes a (1×) repeat group.
        group_order = self.next_order() if sets > 1 or rest else None
        end, end_label, secs = self._end(spec, allow_reps=True)
        step: dict[str, Any] = {
            "type": "ExecutableStepDTO", "stepOrder": self.next_order(),
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3}, **end,
            "targetType": _target_type("none"), "category": ex["category"], "exerciseName": ex["exercise"] or None,
        }
        if weight is not None:
            step["weightValue"] = float(weight) * 1000.0  # Garmin stores grams, tagged kilogram
            step["weightUnit"] = dict(WEIGHT_KG)
        if spec.get("note"):
            step["description"] = str(spec["note"])[:512]
        inner = [step]
        if rest:
            inner.append({"type": "ExecutableStepDTO", "stepOrder": self.next_order(),
                          "stepType": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
                          "endCondition": _cond(COND_TIME), "endConditionValue": rest,
                          "targetType": _target_type("none")})
        n = sets * mult
        self.est_s += ((secs or 0) + (rest or 0)) * n
        if secs is None:
            self.est_s += 3 * float(spec.get("reps", 0)) * n  # ~3 s per rep
        self.lines.append(f"{'  ' * depth}{ex['name']}: {sets} × {end_label}"
                          f"{f' @ {weight:g} kg' if weight is not None else ''}"
                          f"{f', rest {_fmt_secs(rest)}' if rest else ''}"
                          f"{' — ' + step['description'] if 'description' in step else ''}")
        if group_order is None:
            return step
        return {
            "type": "RepeatGroupDTO", "stepOrder": group_order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "numberOfIterations": sets, "endCondition": _cond(COND_ITERATIONS, False),
            "endConditionValue": float(sets), "smartRepeat": False, "skipLastRestStep": False, "workoutSteps": inner,
        }


def build(spec: Any) -> tuple[dict[str, Any], str]:
    """Validate a spec and return (Garmin workout JSON, human-readable summary)."""
    if not isinstance(spec, dict):
        raise ToolError("The workout spec must be a JSON object.\n\n" + SPEC_DOC)
    unknown = set(spec) - {"name", "sport", "description", "steps"}
    if unknown:
        raise ToolError(f"Unknown spec fields: {', '.join(sorted(unknown))}.\n\n" + SPEC_DOC)
    name = str(spec.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ToolError("name: required, at most 80 characters")
    sport_in = str(spec.get("sport") or "running").lower()
    if sport_in not in SPORTS:
        raise ToolError(f"sport {sport_in!r}: use running, cycling, strength, cardio, hiit, yoga, pilates, "
                        "mobility or other")
    sport_id, sport_key = SPORTS[sport_in]
    b = _Builder(sport_key)
    steps = b.steps(spec.get("steps"), 0, 1)
    sport_type = {"sportTypeId": sport_id, "sportTypeKey": sport_key, "displayOrder": sport_id}
    workout: dict[str, Any] = {
        "workoutName": name,
        "sportType": sport_type,
        "estimatedDurationInSecs": int(round(b.est_s)),
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport_type, "workoutSteps": steps}],
    }
    if b.distance_m:
        workout["estimatedDistanceInMeters"] = b.distance_m
    if spec.get("description"):
        workout["description"] = str(spec["description"])[:1024]
    header = f"{name} ({sport_key.replace('_', ' ')}"
    header += f", ~{_fmt_secs(b.est_s)}" if b.est_s else ""
    header += f", {b.distance_m / 1000:g} km" if b.distance_m else ""
    summary = "\n".join([header + ")", *[f"  {line}" for line in b.lines]])
    return workout, summary

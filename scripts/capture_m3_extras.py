"""Capture the M3 endpoints with non-standard signatures (read-only, local token).

    uv run python scripts/capture_m3_extras.py
"""

import datetime as dt
import json
import time
from pathlib import Path

from garminconnect import Garmin

OUT = Path("scratch/payloads/m3")


def save(name: str, fn, *args) -> object:
    time.sleep(0.6)
    try:
        payload = fn(*args)
    except Exception as e:  # noqa: BLE001
        payload = {"__error__": f"{type(e).__name__}: {str(e)[:200]}"}
    (OUT / f"{name}.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    print(name, "->", "error" if isinstance(payload, dict) and "__error__" in payload else
          f"{len(json.dumps(payload, default=str))} bytes")
    return payload


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    g = Garmin()
    g.login("~/.garminconnect")
    today = dt.date.today()
    save("x_body_composition_all", g.get_body_composition, "2025-01-01", today.isoformat())
    save("x_blood_pressure_2026-09", g.get_blood_pressure, "2026-09-01", "2026-09-30")
    save("x_endurance_score_daily_2026-09", g.get_endurance_score, "2026-09-01", "2026-09-30", "daily")
    save("x_endurance_score_weekly_2026-09", g.get_endurance_score, "2026-09-01", "2026-09-30", "weekly")
    save("x_hill_score_2026-09", g.get_hill_score, "2026-09-01", "2026-09-30")
    save("x_scheduled_workouts_2026-10", g.get_scheduled_workouts, 2026, 10)
    save("x_daily_stress_stats_7d", g.get_daily_stress_stats, "2026-09-01", "2026-09-07")
    save("x_daily_stats_7d", g.get_daily_stats, "2026-09-01", "2026-09-07")
    gear = save("x_gear_list", g.get_gear_list)
    if isinstance(gear, list) and gear:
        save("x_gear_stats_first", g.get_gear_stats, gear[0].get("uuid"))
    workouts = json.loads((OUT / "get_workouts.json").read_text(encoding="utf-8"))
    if isinstance(workouts, list) and workouts:
        save("x_workout_first", g.get_workout_by_id, workouts[0].get("workoutId"))


if __name__ == "__main__":
    main()

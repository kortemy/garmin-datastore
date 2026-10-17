"""Dump raw Garmin activity payloads to scratch/payloads/activities/ (git-ignored).

Captures the activity list page plus every per-activity endpoint for the most recent
activity of each requested type. Uses a local token directory, read-only.

    uv run python scripts/capture_activity_payloads.py --types running,strength_training,soccer
"""

import argparse
import json
import time
from pathlib import Path

from garminconnect import Garmin

PER_ACTIVITY = [
    "get_activity", "get_activity_splits", "get_activity_typed_splits", "get_activity_split_summaries",
    "get_activity_hr_in_timezones", "get_activity_power_in_timezones", "get_activity_exercise_sets",
    "get_activity_weather", "get_activity_gear",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", default="running,strength_training,soccer")
    ap.add_argument("--tokens", default="~/.garminconnect")
    ap.add_argument("--out", default="scratch/payloads/activities")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    g = Garmin()
    g.login(args.tokens)

    page = g.get_activities(0, 40)
    (out / "get_activities__page0.json").write_text(json.dumps(page, indent=1, default=str), encoding="utf-8")
    print("activities in page:", len(page), "| total:", g.count_activities())

    for t in args.types.split(","):
        act = next((a for a in page if (a.get("activityType") or {}).get("typeKey") == t), None)
        if not act:
            print("no recent activity of type", t)
            continue
        aid = act["activityId"]
        for name in PER_ACTIVITY:
            time.sleep(0.5)
            try:
                payload = getattr(g, name)(aid)
            except Exception as e:  # noqa: BLE001
                payload = {"__error__": f"{type(e).__name__}: {e}"}
            (out / f"{name}__{t}.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        time.sleep(0.5)
        fit = g.download_activity(aid, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
        (out / f"original__{t}.zip").write_bytes(fit)
        print("captured", t, aid, f"fit={len(fit)}B")


if __name__ == "__main__":
    main()

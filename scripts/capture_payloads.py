"""Dump raw Garmin daily payloads to scratch/payloads/ (git-ignored) for building normalizers.

Uses a local token directory (default ~/.garminconnect), never the cloud token family.
Read-only against Garmin.

    uv run python scripts/capture_payloads.py --days 3
"""

import argparse
import datetime as dt
import json
import time
from pathlib import Path

from garminconnect import Garmin

DAILY = [
    "get_user_summary", "get_sleep_data", "get_hrv_data", "get_training_readiness",
    "get_morning_training_readiness", "get_training_status", "get_max_metrics", "get_stress_data",
    "get_body_battery_events", "get_heart_rates", "get_steps_data", "get_respiration_data",
    "get_spo2_data", "get_hydration_data", "get_intensity_minutes_data", "get_floors",
    "get_rhr_day", "get_fitnessage_data",
]
RANGE_SAME_DAY = ["get_body_battery"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--tokens", default="~/.garminconnect")
    ap.add_argument("--out", default="scratch/payloads")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    g = Garmin()
    g.login(args.tokens)
    today = dt.date.today()
    for i in range(1, args.days + 1):
        d = (today - dt.timedelta(days=i)).isoformat()
        for name in DAILY + RANGE_SAME_DAY:
            time.sleep(0.5)
            try:
                payload = getattr(g, name)(d, d) if name in RANGE_SAME_DAY else getattr(g, name)(d)
            except Exception as e:  # noqa: BLE001
                payload = {"__error__": f"{type(e).__name__}: {e}"}
            (out / f"{name}__{d}.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        print("captured", d)


if __name__ == "__main__":
    main()

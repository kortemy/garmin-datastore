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
SCRUB = {"userProfilePK", "userProfilePk", "userProfileId", "userId", "deviceId", "uuid",
         "userDailySummaryId", "id", "imageURL", "deviceName"}
MAX_LIST = 30

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
        return {("0" if k.isdigit() else k): scrub(v) for k, v in o.items() if k not in SCRUB}
    if isinstance(o, list):
        return [scrub(v) for v in o[:MAX_LIST]]
    return o


def load(method: str, date: str):
    return json.loads((SRC / f"{method}__{date}.json").read_text(encoding="utf-8"))


def main() -> None:
    date = sys.argv[1]
    OUT.mkdir(parents=True, exist_ok=True)
    for domain, spec in DOMAINS.items():
        payload = {k: load(m, date) for k, m in spec.items()} if isinstance(spec, dict) else load(spec, date)
        (OUT / f"{domain}.json").write_text(json.dumps(scrub(payload), indent=1), encoding="utf-8")
        print("wrote", domain)


if __name__ == "__main__":
    main()

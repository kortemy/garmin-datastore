"""M1 exit check: compare garmin.v_daily_wellness with the old Notion collector's reports.

For each date, uses the newest report generated *after* that date (so the day was complete
when collected) and compares every scalar field. Hourly timelines are skipped: the new
store keeps full-resolution intraday data instead.

    uv run python scripts/parity_check.py ../garmin-connect-mcp/reports [--days 30]
"""

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gds.cli import load_env_into_process  # noqa: E402

SKIP = {"date", "bb_timeline", "stress_timeline", "hr_timeline", "steps_timeline", "_raw"}
FLOAT_TOL = 0.051


def latest_reports(folder: Path) -> dict[str, dict]:
    """date → (report generated on the latest day after `date`)'s row for that date."""
    best: dict[str, tuple[str, dict]] = {}
    for f in sorted(folder.glob("wellness-*.json")):
        generated = f.stem.removeprefix("wellness-")
        for row in json.loads(f.read_text(encoding="utf-8")):
            d = row.get("date")
            if d and d < generated and (d not in best or generated > best[d][0]):
                best[d] = (generated, row)
    return {d: row for d, (_, row) in best.items()}


def same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= FLOAT_TOL
    return a == b


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", type=Path)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    load_env_into_process()
    from gds.db import connect

    reports = latest_reports(args.reports)
    cutoff = (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    dates = sorted(d for d in reports if d >= cutoff)

    with connect() as conn:
        rows = conn.execute(
            "select * from garmin.v_daily_wellness where date = any(%s::date[])", (dates,)
        ).fetchall()
    ours = {r["date"].isoformat(): r for r in rows}

    compared = mismatched = filled = 0
    mismatches: Counter = Counter()
    examples: dict[str, list] = {}
    missing_days = [d for d in dates if d not in ours]
    for d in dates:
        if d not in ours:
            continue
        for key, theirs in reports[d].items():
            if key in SKIP:
                continue
            if key not in ours[d]:
                mismatches[f"{key} (not in view)"] += 1
                continue
            mine = ours[d][key]
            if hasattr(mine, "__float__") and not isinstance(mine, (int, float, bool)):
                mine = float(mine)  # Decimal from round()
            if theirs is None:
                filled += mine is not None
                continue
            compared += 1
            if not same(mine, theirs):
                mismatched += 1
                mismatches[key] += 1
                examples.setdefault(key, []).append((d, theirs, mine))

    print(f"days in reports (last {args.days}): {len(dates)}   days in view: {len(dates) - len(missing_days)}")
    if missing_days:
        print(f"missing from view: {', '.join(missing_days)}")
    print(f"fields compared: {compared}   matched: {compared - mismatched} "
          f"({100 * (compared - mismatched) / max(compared, 1):.1f}%)   "
          f"filled where Notion had nothing: {filled}")
    for key, n in mismatches.most_common():
        sample = "; ".join(f"{d}: notion={t!r} ours={m!r}" for d, t, m in examples.get(key, [])[:3])
        print(f"  {key:28s} {n:3d}  {sample}")


if __name__ == "__main__":
    main()

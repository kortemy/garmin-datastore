"""Probe every parameterless and (start, end) read endpoint once, saving payloads.

Parameterless methods are called once; range methods with one calendar month. Results
(status, size) go to stdout as JSON; payloads to scratch/payloads/m3/ (git-ignored).
Read-only, local token directory.

    uv run python scripts/probe_endpoints.py --month 2026-09
"""

import argparse
import calendar
import inspect
import json
import time
from pathlib import Path

from garminconnect import Garmin

RANGE_START = {"startdate", "start", "start_date", "startdate_str"}
RANGE_END = {"enddate", "end", "end_date", "enddate_str"}


def required_params(fn) -> list[str]:
    try:
        return [p.name.lower() for p in inspect.signature(fn).parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)]
    except (TypeError, ValueError):
        return ["?"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2026-09")
    ap.add_argument("--tokens", default="~/.garminconnect")
    ap.add_argument("--out", default="scratch/payloads/m3")
    args = ap.parse_args()
    y, m = map(int, args.month.split("-"))
    start, end = f"{args.month}-01", f"{args.month}-{calendar.monthrange(y, m)[1]:02d}"

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    g = Garmin()
    g.login(args.tokens)

    results = {}
    for name in sorted(dir(g)):
        if not name.startswith("get_"):
            continue
        fn = getattr(g, name)
        if not callable(fn):
            continue
        params = required_params(fn)
        if params == []:
            call_args: tuple = ()
            kind = "snapshot"
        elif len(params) == 2 and params[0] in RANGE_START and params[1] in RANGE_END:
            call_args = (start, end)
            kind = "range"
        else:
            continue
        time.sleep(0.6)
        t0 = time.monotonic()
        try:
            payload = fn(*call_args)
            status = "empty" if payload in (None, [], {}) else "ok"
            size = len(json.dumps(payload, default=str))
            (out / f"{name}.json").write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            status, size = f"err:{type(e).__name__}: {str(e)[:120]}", 0
        results[name] = {"kind": kind, "status": status, "bytes": size,
                         "ms": int((time.monotonic() - t0) * 1000)}
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()

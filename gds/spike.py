"""M0 spike: can Vercel talk to Garmin, and how do tokens behave? (design §15)"""

import datetime as dt
import importlib.metadata as md
import os
import platform
import time
from typing import Any

from garminconnect import Garmin
from psycopg.types.json import Jsonb

from gds.db import connect
from gds.garmin_client import garmin_session

DAILY_CALLS = [
    "get_user_summary", "get_sleep_data", "get_hrv_data", "get_training_readiness",
    "get_training_status", "get_stress_data", "get_heart_rates", "get_steps_data",
    "get_respiration_data", "get_spo2_data", "get_max_metrics", "get_hydration_data",
    "get_intensity_minutes_data", "get_floors", "get_body_battery_events", "get_rhr_day",
    "get_daily_weigh_ins", "get_fitnessage_data", "get_training_four_week_load_balance",
]


def _environment() -> dict[str, Any]:
    env: dict[str, Any] = {
        "python": platform.python_version(),
        "garminconnect": md.version("garminconnect"),
        "vercel_region": os.environ.get("VERCEL_REGION"),
        "aws_region": os.environ.get("AWS_REGION"),
    }
    try:
        import curl_cffi  # noqa: F401

        env["curl_cffi"] = md.version("curl_cffi")
    except Exception as e:  # noqa: BLE001
        env["curl_cffi"] = f"import failed: {type(e).__name__}"
    return env


def _call(g: Garmin, name: str, *args: Any, pause: float) -> dict[str, Any]:
    time.sleep(pause)
    t0 = time.monotonic()
    try:
        result = getattr(g, name)(*args)
        status = "empty" if result in (None, [], {}) else "ok"
        err = None
    except Exception as e:  # noqa: BLE001
        status, err = type(e).__name__, str(e)[:160]
    return {"call": name, "status": status, "ms": int((time.monotonic() - t0) * 1000), "error": err}


def garmin_probe(days: int = 2, pause: float = 1.0) -> dict[str, Any]:
    started = time.monotonic()
    calls: list[dict[str, Any]] = []
    login_error = None
    try:
        with garmin_session(event="probe_refresh") as g:
            today = dt.date.today()
            for i in range(1, days + 1):
                d = (today - dt.timedelta(days=i)).isoformat()
                calls += [_call(g, name, d, pause=pause) for name in DAILY_CALLS]
            calls.append(_call(g, "get_user_profile", pause=pause))
            calls.append(_call(g, "get_devices", pause=pause))
            calls.append(_call(g, "get_personal_record", pause=pause))
            time.sleep(pause)
            activities = g.get_activities(0, 20)
            calls.append({"call": "get_activities", "status": "ok", "ms": None, "error": None})
            for a in activities[:3]:
                aid = a["activityId"]
                calls.append(_call(g, "get_activity", aid, pause=pause))
                calls.append(_call(g, "get_activity_splits", aid, pause=pause))
                calls.append(
                    _call(g, "download_activity", aid, Garmin.ActivityDownloadFormat.ORIGINAL, pause=pause)
                )
    except Exception as e:  # noqa: BLE001
        login_error = f"{type(e).__name__}: {str(e)[:200]}"

    summary: dict[str, int] = {}
    for c in calls:
        summary[c["status"]] = summary.get(c["status"], 0) + 1
    result = {
        "environment": _environment(),
        "seconds": round(time.monotonic() - started, 1),
        "requests": len(calls),
        "summary": summary,
        "login_error": login_error,
        "failures": [c for c in calls if c["status"] not in ("ok", "empty")],
        "median_ms": sorted(c["ms"] for c in calls if c["ms"] is not None)[len(calls) // 2] if calls else None,
    }
    _record("garmin_probe", result)
    return result


def forced_refresh() -> dict[str, Any]:
    """Refresh the access token once and record whether Garmin rotated the refresh token."""
    with garmin_session(event="forced_refresh") as g:
        before = g.client.di_refresh_token
        g.client._refresh_di_token()  # private API: fine for a spike, wrapped by the broker in M1
        rotated = g.client.di_refresh_token != before
        check = _call(g, "get_user_profile", pause=0)
    result = {"refresh_token_rotated": rotated, "call_after_refresh": check["status"]}
    _record("forced_refresh", result)
    return result


def record_tick(source: str, phase: str, detail: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "insert into garmin.spike_ticks (source, phase, detail) values (%s, %s, %s)",
            (source, phase, Jsonb(detail or {})),
        )


def status() -> dict[str, Any]:
    with connect() as conn:
        ticks = conn.execute(
            """select source, phase, received_at from garmin.spike_ticks
               order by received_at desc limit 6"""
        ).fetchall()
        results = conn.execute(
            """select kind, created_at, result from garmin.spike_results
               order by created_at desc limit 3"""
        ).fetchall()
        token = conn.execute(
            "select version, access_expires_at, updated_at from garmin.garmin_tokens limit 1"
        ).fetchone()
    return {"recent_ticks": ticks, "recent_results": results, "token": token}


def _record(kind: str, result: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "insert into garmin.spike_results (kind, result) values (%s, %s)", (kind, Jsonb(result))
        )

"""Defensive helpers for reading Garmin payloads (missing keys → None, never raise)."""

import datetime as dt
from typing import Any

UTC = dt.UTC


def get(d: Any, *path: Any, default: Any = None) -> Any:
    cur = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and isinstance(p, int) and -len(cur) <= p < len(cur):
            cur = cur[p]
        else:
            return default
    return default if cur is None else cur


def num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def to_int(v: Any) -> int | None:
    v = num(v)
    return int(round(v)) if v is not None else None


def parse_ts(v: Any) -> dt.datetime | None:
    """Garmin GMT timestamp: ISO string ('2026-10-14T22:00:00.0') or epoch ms → aware UTC."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / 1000, tz=UTC)
    if isinstance(v, str) and v:
        try:
            return dt.datetime.fromisoformat(v.split(".")[0].rstrip("Z")).replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def offset_min(gmt: Any, local: Any) -> int | None:
    """Local-minus-UTC offset in minutes from a pair of Garmin GMT/Local timestamps.

    Garmin "Local" values are wall-clock times encoded as if they were UTC, so the
    difference between the two is the offset in effect (handles travel per day).
    """
    g, loc = parse_ts(gmt), parse_ts(local)
    if g is None or loc is None:
        return None
    return int(round((loc - g).total_seconds() / 60))


def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def descriptor_index(descriptors: Any, key: str) -> int | None:
    """Find the column index for `key` in any of Garmin's descriptor-list shapes."""
    for d in descriptors or []:
        if not isinstance(d, dict):
            continue
        k = d.get("key") or d.get("bodyBatteryValueDescriptorKey") or d.get("respirationAveragesValueDescriptionKey")
        i = d.get("index")
        if i is None:
            i = d.get("bodyBatteryValueDescriptorIndex", d.get("respirationAveragesValueDescriptorIndex"))
        if k == key and isinstance(i, int):
            return i
    return None


def build_series(
    calendar_date: dt.date,
    metric: str,
    points: list[tuple[dt.datetime, float]],
    utc_offset: int | None,
) -> dict[str, Any] | None:
    """Pack (utc_ts, value) points into one intraday_series row (design §4.4)."""
    pts = sorted((t, float(v)) for t, v in points if t is not None and num(v) is not None)
    if not pts:
        return None
    start = pts[0][0]
    return {
        "calendar_date": calendar_date,
        "metric": metric,
        "start_utc": start,
        "utc_offset_min": utc_offset if utc_offset is not None else 0,
        "ts_offsets_s": [int((t - start).total_seconds()) for t, _ in pts],
        "vals": [v for _, v in pts],
    }


def array_points(rows: Any, ts_idx: int | None, val_idx: int | None) -> list[tuple[dt.datetime, Any]]:
    if ts_idx is None or val_idx is None:
        return []
    out = []
    for r in rows or []:
        if isinstance(r, (list, tuple)) and len(r) > max(ts_idx, val_idx):
            out.append((parse_ts(r[ts_idx]), r[val_idx]))
    return out

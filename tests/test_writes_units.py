"""Write executor (M5): idempotency, dry runs, audit rows, ownership, error mapping.
No network or database: a tiny in-memory stand-in plays `garmin.write_log` and friends."""

import datetime as dt
import uuid
from contextlib import contextmanager
from typing import Any

import pytest
from garminconnect.exceptions import GarminConnectConnectionError

from gds import writes
from gds.tools import ToolError

ACCT = uuid.uuid4()


class Result:
    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = rows or []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeDB:
    """Just enough SQL for writes.execute: accounts, write_log and a few lookups."""

    def __init__(self):
        self.log: dict[str, dict[str, Any]] = {}
        self.next_id = 1
        self.lookups: dict[str, list[dict[str, Any]]] = {}

    @contextmanager
    def transaction(self):
        yield

    def execute(self, sql: str, params: tuple = ()) -> Result:
        s = " ".join(sql.split()).lower()
        if s.startswith("select id, home_timezone, status from garmin.accounts"):
            return Result([{"id": ACCT, "home_timezone": "Europe/Paris", "status": "active"}])
        if s.startswith("select * from garmin.write_log where account_id = %s and idempotency_key"):
            row = self.log.get(params[1])
            return Result([row] if row else [])
        if s.startswith("insert into garmin.write_log"):
            self.log[params[1]] = {"id": self.next_id, "idempotency_key": params[1], "tool": params[2],
                                   "input": params[3].obj, "status": "pending", "preview": None,
                                   "created_at": dt.datetime.now(dt.UTC), "undone_by": None}
            self.next_id += 1
            return Result()
        if s.startswith("update garmin.write_log set preview"):
            self.log[params[2]]["preview"] = params[0]
            return Result()
        if s.startswith("update garmin.write_log set status = %s"):
            row = self.log[params[-1]]
            keys = ["status", "error", "garmin_response", "entity_kind", "entity_id", "entity_date",
                    "created_by_system", "undo"]
            row.update({k: getattr(v, "obj", v) for k, v in zip(keys, params, strict=False)})
            return Result([{"id": row["id"]}])
        if s.startswith("update garmin.write_log set status = 'pending'"):
            row = next(r for r in self.log.values() if r["id"] == params[0])
            row["status"] = "pending"
            return Result()
        if s.startswith("update garmin.write_log set undone_by"):
            row = next(r for r in self.log.values() if r["id"] == params[1])
            row["undone_by"] = params[0]
            return Result()
        if s.startswith("update garmin.write_log"):
            return Result()
        for marker, rows in self.lookups.items():
            if marker in s:
                return Result(rows)
        if "from garmin.write_log" in s and "created_by_system and status = 'ok'" in s:
            kind, eid = params[1], params[2]
            rows = [r for r in self.log.values() if r.get("entity_kind") == kind and r.get("entity_id") == eid
                    and r.get("created_by_system") and r["status"] == "ok"]
            return Result(rows[-1:])
        return Result()


class FakeGarmin:
    def __init__(self, **responses: Any):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.responses = responses
        self.client = self

    def dumps(self) -> str:
        return "token"

    def __getattr__(self, name: str):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            r = self.responses.get(name)
            if isinstance(r, Exception):
                raise r
            return r
        return method


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    garmin = FakeGarmin()

    @contextmanager
    def fake_connect(*_, **__):
        yield db

    monkeypatch.setattr(writes, "connect", fake_connect)
    monkeypatch.setattr(writes.store, "load_raw", lambda *a: None)
    monkeypatch.setattr(writes.broker, "open_client", lambda conn, acct: (garmin, "token"))
    monkeypatch.setattr(writes.broker, "persist_if_changed", lambda *a: None)
    monkeypatch.setattr(writes, "_resync", lambda conn, src, acct, units: [{"domain": d, "scope_key": k}
                                                                          for d, k in units])
    monkeypatch.setattr(writes.Source, "_wait", lambda self: None)
    return db, garmin


KEY = "11111111-2222-3333-4444-555555555555"
SPEC = {"name": "Test — easy 20", "steps": [{"type": "run", "time": "20:00", "target": {"hr_zone": 2}}]}


def test_dry_run_previews_without_writing(env):
    db, garmin = env
    out = writes.create_workout("", SPEC, dry_run=True)
    assert out["dry_run"] is True and "Test — easy 20" in out["preview"]
    assert out["garmin_workout"]["workoutName"] == "Test — easy 20"
    assert db.log == {} and garmin.calls == []


def test_write_is_logged_resynced_and_idempotent(env):
    db, garmin = env
    garmin.responses["upload_workout"] = {"workoutId": 987}
    out = writes.create_workout(KEY, SPEC, dry_run=False)
    assert out["status"] == "ok" and out["entity_id"] == "987" and out["workout_id"] == 987
    assert out["synced"] == [{"domain": "workouts", "scope_key": "latest"},
                             {"domain": "workout_detail", "scope_key": "987"}]
    row = db.log[KEY]
    assert row["status"] == "ok" and row["created_by_system"] is True and row["entity_kind"] == "workout"
    assert row["preview"].startswith("Create workout")

    again = writes.create_workout(KEY, SPEC, dry_run=False)
    assert again["replayed"] is True and again["entity_id"] == "987"
    assert [c[0] for c in garmin.calls] == ["upload_workout"]  # sent once


def test_key_reuse_for_a_different_write_is_refused(env):
    env[1].responses["upload_workout"] = {"workoutId": 1}
    writes.create_workout(KEY, SPEC, dry_run=False)
    with pytest.raises(ToolError, match="different write"):
        writes.create_workout(KEY, {**SPEC, "name": "Other"}, dry_run=False)


def test_missing_key_is_refused(env):
    with pytest.raises(ToolError, match="idempotency_key"):
        writes.create_workout("", SPEC, dry_run=False)


def test_garmin_error_marks_failed_and_allows_retry(env):
    db, garmin = env
    garmin.responses["upload_workout"] = GarminConnectConnectionError("API Error 500")
    with pytest.raises(ToolError, match="did not happen"):
        writes.create_workout(KEY, SPEC, dry_run=False)
    assert db.log[KEY]["status"] == "failed"
    garmin.responses["upload_workout"] = {"workoutId": 5}
    assert writes.create_workout(KEY, SPEC, dry_run=False)["entity_id"] == "5"


def test_unclear_response_keeps_the_key_reserved(env):
    db, garmin = env
    garmin.responses["upload_workout"] = {"unexpected": True}
    with pytest.raises(ToolError, match="no workout id"):
        writes.create_workout(KEY, SPEC, dry_run=False)
    assert db.log[KEY]["status"] == "pending"
    with pytest.raises(ToolError, match="in progress"):
        writes.create_workout(KEY, SPEC, dry_run=False)


def test_unschedule_refuses_entries_the_system_did_not_create(env):
    with pytest.raises(ToolError, match="not created by this system"):
        writes.unschedule_workout(KEY, 4242, dry_run=True)


def test_schedule_then_unschedule(env):
    db, garmin = env
    day = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    garmin.responses["schedule_workout"] = {"workoutScheduleId": 777, "calendarDate": day}
    out = writes.schedule_workout(KEY, 987, day, dry_run=False)
    assert out["scheduled_workout_id"] == 777
    db.log[KEY]["entity_date"] = dt.date.fromisoformat(day)
    db.log[KEY]["undo"] = {"workout_id": 987}

    garmin.responses["unschedule_workout"] = {}
    removed = writes.unschedule_workout(KEY + "-u", 777, dry_run=False)
    assert removed["status"] == "ok" and garmin.calls[-1][0] == "unschedule_workout"
    assert db.log[KEY]["undone_by"] == db.log[KEY + "-u"]["id"]
    with pytest.raises(ToolError, match="already removed"):
        writes.unschedule_workout(KEY + "-v", 777, dry_run=True)


def test_log_weight_uses_local_and_utc_timestamps(env):
    db, garmin = env
    garmin.responses["add_weigh_in_with_timestamps"] = {}
    garmin.responses["get_daily_weigh_ins"] = {"dateWeightList": []}
    out = writes.log_weight(KEY, 106.4, date="2026-10-19", time="07:30", dry_run=False)
    assert out["written"].startswith("Weigh-in 106.4 kg at Mon 2026-10-19 07:30")
    name, args, _ = garmin.calls[0]
    assert name == "add_weigh_in_with_timestamps"
    assert args[:3] == (106.4, "kg", "2026-10-19T07:30:00+02:00")
    assert args[3] == "2026-10-19T05:30:00"  # GMT
    assert out["synced"] == [{"domain": "body_composition", "scope_key": "2026-10"}]


def test_body_composition_goes_through_the_fit_upload(env):
    _, garmin = env
    garmin.responses["add_body_composition"] = {}
    garmin.responses["get_daily_weigh_ins"] = {}
    writes.log_weight(KEY, 106.4, date="2026-10-19", time="07:30", body_fat_pct=22, dry_run=False)
    name, args, kwargs = garmin.calls[0]
    assert name == "add_body_composition" and args == ("2026-10-19T07:30:00+02:00", 106.4)
    assert kwargs == {"percent_fat": 22.0}


@pytest.mark.parametrize("call, message", [
    (lambda: writes.log_weight(KEY, 5, dry_run=True), "weight_kg"),
    (lambda: writes.log_weight(KEY, 80, date="2999-01-01", dry_run=True), "future"),
    (lambda: writes.log_blood_pressure(KEY, 80, 120, dry_run=True), "systolic"),
    (lambda: writes.log_blood_pressure(KEY, 120, 125, dry_run=True), "diastolic"),
    (lambda: writes.log_hydration(KEY, 0, dry_run=True), "ml"),
    (lambda: writes.manage_gear(KEY, "delete", dry_run=True), "action"),
])
def test_validation(env, call, message):
    with pytest.raises(ToolError, match=message):
        call()


def test_first_finds_nested_ids():
    assert writes._first({"a": 1, "inner": {"workoutId": 9}}, "workoutId") == 9
    assert writes._first({"workoutScheduleId": 3, "id": 4}, "workoutScheduleId", "id") == 3
    assert writes._first(None, "x") is None

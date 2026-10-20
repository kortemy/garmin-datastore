"""Pure engine/store helpers (no database)."""

import datetime as dt

import pytest

from gds.engine import backoff
from gds.store import encode_raw, upsert_sql


def test_backoff_doubles_and_caps():
    assert backoff(1) == dt.timedelta(minutes=5)
    assert backoff(2) == dt.timedelta(minutes=10)
    assert backoff(10) == dt.timedelta(hours=6)


def test_encode_raw_is_order_independent():
    a, ha = encode_raw({"b": 1, "a": [1, 2]})
    b, hb = encode_raw({"a": [1, 2], "b": 1})
    assert a == b and ha == hb


def test_upsert_only_overwrites_given_columns():
    q = upsert_sql("daily_summary", ["account_id", "calendar_date", "hydration_ml"]).as_string(None)
    assert 'on conflict ("account_id", "calendar_date")' in q
    assert '"hydration_ml" = excluded."hydration_ml"' in q
    assert '"total_steps"' not in q


def test_empty_payload_clears_previous_data_once(monkeypatch):
    """activity_gear: all gear unlinked → the activity's gear is cleared, and only on the transition."""
    import uuid
    from contextlib import contextmanager

    from gds import engine

    applied, marked, sql = [], [], []

    class Conn:
        @contextmanager
        def transaction(self):
            yield

        def execute(self, q, params=()):
            sql.append(q)

    monkeypatch.setattr(engine, "apply_rows", lambda conn, acct, rows, boost=None: applied.append(rows))
    monkeypatch.setattr(engine, "_mark", lambda conn, acct, unit, status, **kw: marked.append(status))

    class Src:
        def call(self, *a):
            return []

    unit = {"domain": "activity_gear", "scope_key": "123", "attempts": 0, "content_hash": "abc"}
    assert engine.process_unit(Conn(), Src(), uuid.uuid4(), unit) == "empty"
    assert applied == [{"activities": [{"activity_id": 123, "gear": None}]}]
    assert any("content_hash = null" in q for q in sql)

    applied.clear()
    assert engine.process_unit(Conn(), Src(), uuid.uuid4(), {**unit, "content_hash": None}) == "empty"
    assert applied == [] and marked == ["empty", "empty"]


def test_ensure_units_boost_only_raises_new_and_pending_units():
    import uuid

    from gds import store

    calls = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def executemany(self, q, params):
            calls.append(params)

    class Conn:
        def cursor(self):
            return Cur()

    acct = uuid.uuid4()
    units = [{"domain": "activity_laps", "scope_key": "1", "priority": 9, "requeue": True}]
    store.ensure_units(Conn(), acct, units)
    store.ensure_units(Conn(), acct, units, boost=200)
    # (acct, domain, key, insert/pending priority, re-queue priority, requeue, boost?)
    assert calls[0] == [(acct, "activity_laps", "1", 9, 9, True, False)]
    assert calls[1] == [(acct, "activity_laps", "1", 200, 9, True, True)]


def test_manual_units_pass_their_priority_to_child_units(monkeypatch):
    """A manual sync of the activity index fetches the new activity's details in the same run."""
    import uuid
    from contextlib import contextmanager

    from gds import engine, store
    from gds.domains import REGISTRY, Domain

    boosts = []

    class Conn:
        @contextmanager
        def transaction(self):
            yield

    monkeypatch.setitem(REGISTRY, "t_index", Domain(
        "t_index", "index", lambda src, key: [{"id": 1}],
        lambda raw, key: {"work_units": [{"domain": "activity_laps", "scope_key": "1", "priority": 9}]}))
    monkeypatch.setattr(engine, "apply_rows", lambda conn, acct, rows, boost=None: boosts.append(boost))
    monkeypatch.setattr(engine, "_mark", lambda *a, **k: None)
    monkeypatch.setattr(store, "save_raw", lambda *a, **k: None)
    unit = {"domain": "t_index", "scope_key": "recent", "attempts": 0, "content_hash": None}
    engine.process_unit(Conn(), None, uuid.uuid4(), {**unit, "priority": engine.PRIORITY_MANUAL})
    engine.process_unit(Conn(), None, uuid.uuid4(), {**unit, "priority": 50})
    assert boosts == [engine.PRIORITY_MANUAL, None]


def _fake_connect(rows):
    """connect() stand-in whose execute() returns the next canned row(s)."""
    from contextlib import contextmanager

    queries = []

    class Res:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

        def fetchall(self):
            return self.value

    class Conn:
        def execute(self, q, params=()):
            queries.append(q)
            return Res(rows.pop(0) if rows else None)

    @contextmanager
    def connect(*a, **k):
        yield Conn()

    return connect, queries


def test_sync_now_cools_down_between_calls(monkeypatch):
    import uuid

    from gds import engine

    connect, _ = _fake_connect([{"id": uuid.uuid4()}, {"ago_s": 12}])
    monkeypatch.setattr(engine, "connect", connect)
    monkeypatch.setattr(engine, "run_tick", lambda *a, **k: pytest.fail("must not tick during the cooldown"))
    out = engine.sync_now()
    assert out["status"] == "skipped" and "12 s ago" in out["reason"]


def test_sync_now_queues_fresh_units_and_runs_a_short_tick(monkeypatch):
    import uuid

    from gds import engine

    acct = {"id": uuid.uuid4(), "home_timezone": "Europe/Belgrade"}
    latest = [{"activity_id": 1, "name": "Morning Run"}]
    connect, _ = _fake_connect([acct, None, {"n": 0}, latest])
    queued, ticks = [], []
    monkeypatch.setattr(engine, "connect", connect)
    monkeypatch.setattr(engine, "queue_fresh", lambda conn, a, days: queued.append(days) or 17)
    monkeypatch.setattr(engine, "run_tick", lambda trigger, budget: ticks.append((trigger, budget)) or {
        "status": "ok", "error": None, "done": 12, "unchanged": 4, "empty": 1, "failed": 0})
    out = engine.sync_now(days=30)
    assert queued == [7]  # clamped
    assert ticks == [(engine.SYNC_NOW_TRIGGER, engine.SYNC_NOW_BUDGET_S)]
    assert out == {"status": "ok", "queued_units": 17, "fetched_units": 17, "failed_units": 0, "still_queued": 0,
                   "latest_activities": latest}


def test_sync_now_reports_a_busy_lease(monkeypatch):
    import uuid

    from gds import engine

    connect, _ = _fake_connect([{"id": uuid.uuid4()}, None, {"n": 17}, []])
    monkeypatch.setattr(engine, "connect", connect)
    monkeypatch.setattr(engine, "queue_fresh", lambda *a: 17)
    monkeypatch.setattr(engine, "run_tick", lambda *a: {"status": "skipped", "reason": "another tick is running"})
    out = engine.sync_now()
    assert out["still_queued"] == 17 and "another tick is running" in out["note"]

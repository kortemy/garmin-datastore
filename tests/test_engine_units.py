"""Pure engine/store helpers (no database)."""

import datetime as dt

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

    monkeypatch.setattr(engine, "apply_rows", lambda conn, acct, rows: applied.append(rows))
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

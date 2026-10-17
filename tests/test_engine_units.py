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

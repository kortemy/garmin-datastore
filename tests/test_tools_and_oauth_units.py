"""Pure helpers of the read tools and the OAuth module (no database)."""

import pytest

from gds import oauth, tools


def test_password_hash_roundtrip():
    h = oauth.hash_password("correct horse", n=2**12)
    assert h.startswith("scrypt$4096$8$1$")
    assert oauth.verify_password("correct horse", h)
    assert not oauth.verify_password("wrong", h)
    assert not oauth.verify_password("correct horse", "garbage")


def test_login_page_escapes_client_name():
    page = oauth.login_page("req<1>", "<script>x</script>", "Wrong password.")
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page
    assert 'value="req&lt;1&gt;"' in page and "Wrong password." in page


@pytest.mark.parametrize("sql", [
    "delete from garmin.activities",
    "select 1; drop table garmin.activities",
    "update garmin.sleep set score = 1",
    "with x as (delete from garmin.entities returning 1) select * from x",
    "set role postgres",
    "copy garmin.activities to stdout",
])
def test_query_sql_rejects_non_select(sql):
    with pytest.raises(tools.ToolError):
        tools.query_sql(sql)


def test_query_sql_allows_keywords_inside_strings(monkeypatch):
    captured = {}
    monkeypatch.setattr(tools, "read", lambda sql, *a, **k: captured.setdefault("sql", sql) and [])
    tools.query_sql("select name from garmin.v_activities where name like '%update%'")
    assert captured["sql"].count("%%") == 2  # percent signs escaped for psycopg


def test_date_range_validation():
    with pytest.raises(tools.ToolError):
        tools.date_range("2026-10-10", "2026-10-01")
    with pytest.raises(tools.ToolError):
        tools.date_range("2020-01-01", "2026-10-01")
    with pytest.raises(tools.ToolError):
        tools.date_range("10/01/2026", None)
    s, e = tools.date_range("2026-10-01", "2026-10-07")
    assert (e - s).days == 6


def test_trend_and_intraday_whitelists():
    with pytest.raises(tools.ToolError):
        tools.trend("drop table", None, None, "week", None)
    with pytest.raises(tools.ToolError):
        tools.trend("hrv", None, None, "decade", None)
    with pytest.raises(tools.ToolError):
        tools.intraday("bogus", "2026-10-01", 15)
    with pytest.raises(tools.ToolError):
        tools.entities("secrets", None)

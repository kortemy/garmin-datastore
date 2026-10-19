"""W-9 Strava writes: matching, token refresh, error mapping, write + undo. No network or DB."""

import datetime as dt
import json
from contextlib import contextmanager

import httpx
import pytest

from gds import strava, writes
from gds.tools import ToolError
from tests.test_writes_units import ACCT, KEY, FakeDB, Result, env  # noqa: F401  (fixture)

START = dt.datetime(2026, 10, 18, 7, 0, tzinfo=dt.UTC)


def _act(id_, minutes=0.0, external_id=None):
    return {"id": id_, "start_date": (START + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "external_id": external_id}


# ---------------------------------------------------------------- matching


def test_match_by_start_time():
    assert strava.pick_match([_act(1, -30), _act(2, 1.5), _act(3, 90)], START, 999)["id"] == 2


def test_match_prefers_garmin_uploads_when_ambiguous():
    # Strava's external_id for Garmin uploads is garmin_ping_<upload id>, not the activity id (seen live).
    picked = strava.pick_match([_act(1, 0.5, "other.fit"), _act(2, -0.5, "garmin_ping_642561871983")], START, 999)
    assert picked["id"] == 2


def test_match_falls_back_to_the_exact_start():
    picked = strava.pick_match([_act(1, 0.5, "garmin_ping_1"), _act(2, 0, "garmin_ping_2")], START, 999)
    assert picked["id"] == 2


@pytest.mark.parametrize("acts, message", [
    ([_act(1, 10)], "No Strava activity"),
    ([], "No Strava activity"),
    ([_act(1, 0.5), _act(2, -0.5)], "Several Strava activities"),  # neither exact nor from Garmin
])
def test_match_refuses_when_unsure(acts, message):
    with pytest.raises(ToolError, match=message):
        strava.pick_match(acts, START, 999)


# ---------------------------------------------------------------- client errors


def _client(status: int, body: object = None) -> strava.StravaClient:
    c = strava.StravaClient("tok")
    c.http = httpx.Client(base_url=strava.API, transport=httpx.MockTransport(
        lambda req: httpx.Response(status, json=body if body is not None else {})))
    return c


@pytest.mark.parametrize("status, message", [
    (401, "rejected the token"), (403, "scope"), (404, "no such activity"), (429, "rate limit"), (500, "HTTP 500"),
])
def test_client_maps_errors(status, message):
    with pytest.raises(ToolError, match=message):
        _client(status).update(1, {"name": "x"})


def test_client_update_sends_put_with_json():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(method=req.method, path=req.url.path, body=json.loads(req.content),
                    auth=req.headers["authorization"])
        return httpx.Response(200, json={"id": 5, "name": "New", "map": {"polyline": "..."}})
    c = strava.StravaClient("tok")
    c.http = httpx.Client(base_url=strava.API, headers={"Authorization": "Bearer tok"},
                          transport=httpx.MockTransport(handler))
    assert c.update(5, {"name": "New"})["name"] == "New"
    assert seen == {"method": "PUT", "path": "/api/v3/activities/5", "body": {"name": "New"},
                    "auth": "Bearer tok"}


# ---------------------------------------------------------------- token refresh


class TokenDB:
    def __init__(self, token: dict, status: str = "active"):
        self.token, self.status, self.saved, self.sql = token, status, [], []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, sql, params=()):
        self.sql.append(" ".join(sql.split()))
        if "from garmin.strava_tokens" in sql:
            return Result([{"ciphertext": b"c", "nonce": b"n", "status": self.status}])
        return Result()


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setattr(strava, "configured", lambda: True)
    monkeypatch.setattr(strava.crypto, "decrypt", lambda *a: json.dumps(db.token))
    monkeypatch.setattr(strava, "save_token", lambda conn, acct, tok: db.saved.append(tok))
    alerts = []
    monkeypatch.setattr(strava, "raise_alert", lambda *a: alerts.append(a[2]))
    db = TokenDB({"access_token": "old", "refresh_token": "r1", "expires_at": 0})
    return db, alerts


def test_fresh_token_is_used_as_is(tokens, monkeypatch):
    db, _ = tokens
    db.token["expires_at"] = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=3)).timestamp()
    monkeypatch.setattr(strava, "_post_token", lambda data: pytest.fail("should not refresh"))
    assert strava.access_token(db, ACCT) == "old"


def test_stale_token_is_refreshed_and_saved_before_use(tokens, monkeypatch):
    db, _ = tokens
    sent = {}

    def post(data):
        sent.update(data)
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_at": 9e9})
    monkeypatch.setattr(strava, "_post_token", post)
    assert strava.access_token(db, ACCT) == "new"
    assert sent == {"grant_type": "refresh_token", "refresh_token": "r1"}
    assert db.saved[0]["refresh_token"] == "r2"  # rotated refresh token persisted
    assert any("for update" in q for q in db.sql)


def test_revoked_grant_flags_reauth_and_alerts(tokens, monkeypatch):
    db, alerts = tokens
    monkeypatch.setattr(strava, "_post_token", lambda data: httpx.Response(400, json={"message": "Bad Request"}))
    with pytest.raises(ToolError, match="strava-auth"):
        strava.access_token(db, ACCT)
    assert any("status = 'needs_reauth'" in q for q in db.sql)
    assert alerts == ["strava_needs_reauth"]


def test_needs_reauth_status_refuses_without_calling_strava(tokens, monkeypatch):
    db, _ = tokens
    db.status = "needs_reauth"
    monkeypatch.setattr(strava, "_post_token", lambda data: pytest.fail("should not call Strava"))
    with pytest.raises(ToolError, match="re-authorization"):
        strava.access_token(db, ACCT)


# ---------------------------------------------------------------- write + undo


class FakeStrava:
    def __init__(self):
        self.puts = []
        self.current = {"id": 77, "name": "Morning Run", "description": "", "sport_type": "Run"}

    def activities_between(self, after, before):
        return [{"id": 77, "start_date": "2026-10-18T07:00:20Z", "external_id": "garmin_ping_555"}]

    def activity(self, sid):
        return dict(self.current)

    def update(self, sid, fields):
        self.puts.append((sid, fields))
        self.current.update(fields)
        return {**self.current, "map": {"polyline": "secret-ish"}}

    def close(self):
        pass


@pytest.fixture
def strava_env(env, monkeypatch):  # noqa: F811
    db, garmin = env
    fake = FakeStrava()
    monkeypatch.setattr(writes, "_strava_client", lambda ctx: fake)
    db.lookups["from garmin.activities where account_id = %s and activity_id = %s"] = [
        {"activity_id": 555, "name": "Morning Run", "type_key": "running", "start_utc": START,
         "start_local": dt.datetime(2026, 10, 18, 9, 0)}]
    return db, garmin, fake


def test_strava_update_preview_write_and_undo(strava_env):
    db, garmin, fake = strava_env
    preview = writes.update_strava_activity("", 555, name="Tempo 3x10'", description="Felt strong", dry_run=True)
    assert "Strava activity 77" in preview["preview"] and "'Morning Run' → \"Tempo 3x10'\"" in preview["preview"]
    assert preview["strava_activity"]["current_name"] == "Morning Run" and fake.puts == []

    out = writes.update_strava_activity(KEY, 555, name="Tempo 3x10'", description="Felt strong")
    assert out["strava_activity_id"] == 77 and out["entity_kind"] == "strava_activity"
    assert fake.puts == [(77, {"name": "Tempo 3x10'", "description": "Felt strong"})]
    row = db.log[KEY]
    assert row["garmin_response"] == {"id": 77, "name": "Tempo 3x10'", "description": "Felt strong"}  # no map
    assert row["undo"] == {"name": "Morning Run", "description": ""}
    assert garmin.calls == []  # Strava-only: no Garmin session opened

    db.lookups["from garmin.write_log where account_id = %s and id = %s"] = [
        {**row, "tool": "update_strava_activity"}]
    writes.undo_write(KEY + "-undo", row["id"])
    assert fake.puts[-1] == (77, {"name": "Morning Run", "description": ""})
    assert garmin.calls == []


def test_strava_update_needs_something_to_change(strava_env):
    with pytest.raises(ToolError, match="Nothing to change"):
        writes.update_strava_activity("", 555, dry_run=True)

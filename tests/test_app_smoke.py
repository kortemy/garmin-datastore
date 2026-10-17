import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("TOKEN_ENC_KEY", "A" * 43 + "=")
os.environ.setdefault("TICK_SECRET", "tick-secret")
os.environ.setdefault("MCP_PATH_SECRET", "path-secret")
os.environ.setdefault("GDS_BASE_URL", "https://gds.example.test")

import pytest
from fastapi.testclient import TestClient

from gds.app import app

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
EXPECTED_TOOLS = {
    "ping", "get_sync_status", "get_daily_wellness", "get_sleep", "get_intraday", "list_activities", "get_activity",
    "get_activity_streams", "get_trend", "get_body", "get_performance", "get_entities", "get_calendar",
    "describe_schema", "query_sql", "request_resync",
}


@pytest.fixture(scope="module")
def c():
    # The MCP session managers can only start once per process, so share one lifespan.
    with TestClient(app) as client:
        yield client


def _rpc(client: TestClient, method: str, params: dict | None = None, id_: int = 1, path="/s/path-secret/mcp"):
    r = client.post(path, headers=MCP_HEADERS,
                    json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


def test_health(c):
    assert c.get("/api/health").json() == {"status": "ok"}


def test_tick_requires_secret(c):
    assert c.post("/api/tick", json={}).status_code == 401
    assert c.post("/api/tick", json={}, headers={"X-GDS-Secret": "wrong"}).status_code == 401


def test_legacy_wrong_path_is_404(c):
    assert c.post("/s/nope/mcp", headers=MCP_HEADERS, json={}).status_code in (401, 404)


def test_legacy_connector_lists_all_tools(c, monkeypatch):
    from gds import spike

    recorded = []
    monkeypatch.setattr(spike, "record_tick", lambda *a, **k: recorded.append(a))
    init = _rpc(c, "initialize",
                {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert init["result"]["serverInfo"]["name"] == "garmin-datastore"
    tools = _rpc(c, "tools/list", id_=2)["result"]["tools"]
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    assert all(t.get("annotations", {}).get("readOnlyHint") for t in tools if t["name"] != "request_resync")
    out = _rpc(c, "tools/call", {"name": "ping", "arguments": {"note": "test"}}, id_=3)
    assert "pong" in out["result"]["content"][0]["text"]
    assert recorded == [("mcp", "ping", {"note": "test"})]


def test_oauth_metadata_is_published(c):
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"].rstrip("/") == "https://gds.example.test"
    assert meta["authorization_endpoint"] == "https://gds.example.test/authorize"
    assert meta["registration_endpoint"] == "https://gds.example.test/register"
    assert "S256" in meta["code_challenge_methods_supported"]
    prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == "https://gds.example.test/mcp"


def test_oauth_mcp_requires_bearer_token(c):
    r = c.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")

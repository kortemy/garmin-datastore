import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("TOKEN_ENC_KEY", "A" * 43 + "=")
os.environ.setdefault("TICK_SECRET", "tick-secret")
os.environ.setdefault("MCP_PATH_SECRET", "path-secret")

import pytest
from fastapi.testclient import TestClient

from gds.app import app

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def c():
    # The MCP session manager can only start once per process, so share one lifespan.
    with TestClient(app) as client:
        yield client


def _rpc(client: TestClient, method: str, params: dict | None = None, id_: int = 1) -> dict:
    r = client.post(
        "/s/path-secret/mcp",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_health(c):
    assert c.get("/api/health").json() == {"status": "ok"}


def test_tick_requires_secret(c):
    assert c.post("/api/tick", json={}).status_code == 401
    assert c.post("/api/tick", json={}, headers={"X-GDS-Secret": "wrong"}).status_code == 401


def test_mcp_wrong_path_is_404(c):
    assert c.post("/s/nope/mcp", headers=MCP_HEADERS, json={}).status_code == 404


def test_mcp_ping_roundtrip(c):
    init = _rpc(
        c,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    )
    assert init["result"]["serverInfo"]["name"] == "garmin-datastore"
    tools = _rpc(c, "tools/list", id_=2)["result"]["tools"]
    assert {t["name"] for t in tools} == {"ping", "spike_status"}
    out = _rpc(c, "tools/call", {"name": "ping", "arguments": {}}, id_=3)
    assert "pong" in out["result"]["content"][0]["text"]

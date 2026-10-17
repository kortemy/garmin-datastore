"""Vercel entrypoint: health, scheduler tick, manual sync, and the MCP connector."""

import hmac
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from gds import engine, spike, status
from gds.config import settings
from gds.db import connect

mcp = MCPServer(
    "garmin-datastore",
    instructions=(
        "Garmin Connect data synced into Supabase. Call get_sync_status first to see how fresh "
        "the data is; say so plainly if data is missing or stale."
    ),
)


@mcp.tool()
def ping(note: str = "") -> str:
    """Connectivity check. Records the call with an optional short note (e.g. 'chat' or
    'scheduled task') and returns 'pong' with the server time."""
    spike.record_tick("mcp", "ping", {"note": note[:100]})
    return f"pong {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"


@mcp.tool()
def get_sync_status() -> dict[str, Any]:
    """How fresh and complete the Garmin data is: account status, last successful sync,
    days covered, backlog, recent sync runs, and any failing items."""
    return status.sync_status()


mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    # Vercel terminates TLS and sets the Host header; the secret path is the guard until OAuth (M4).
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    host="0.0.0.0",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="garmin-datastore", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

_mcp_secret = os.environ.get("MCP_PATH_SECRET", "").strip()
if _mcp_secret:
    app.mount(f"/s/{_mcp_secret}", mcp_app)  # connector URL: /s/<secret>/mcp


def _check_secret(value: str | None) -> None:
    if not value or not hmac.compare_digest(value, settings().tick_secret):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class TickBody(BaseModel):
    source: str = "manual"


@app.post("/api/tick")
def tick(body: TickBody, x_gds_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """Called every 10 min by Supabase pg_cron (fire-and-forget) and by the GitHub backup."""
    _check_secret(x_gds_secret)
    return engine.run_tick(trigger=body.source[:40])


class SyncBody(BaseModel):
    domain: str | None = None
    start: str | None = None  # YYYY-MM-DD
    end: str | None = None


@app.post("/api/sync")
def sync(body: SyncBody, x_gds_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """FR-9: re-queue matching units at top priority, then run a tick right away."""
    _check_secret(x_gds_secret)
    with connect() as conn:
        acct = conn.execute(
            "select id from garmin.accounts where label = %s", (settings().account_label,)
        ).fetchone()
        if not acct:
            raise HTTPException(status_code=404, detail="no account")
        queued = engine.request_resync(conn, acct["id"], body.domain, body.start, body.end)
    return {"queued": queued, "tick": engine.run_tick(trigger="manual_sync")}

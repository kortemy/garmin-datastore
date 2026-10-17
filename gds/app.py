"""Vercel entrypoint (M0 spike): health, tick, Garmin spike endpoints, and a minimal MCP server."""

import hmac
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from gds import spike
from gds.config import settings

mcp = MCPServer(
    "garmin-datastore",
    instructions="M0 spike connector for garmin-datastore. Tools only report spike status for now.",
)


@mcp.tool()
def ping() -> str:
    """Connectivity check. Returns 'pong' and the server time."""
    return f"pong {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"


@mcp.tool()
def spike_status() -> dict[str, Any]:
    """Latest scheduler ticks, Garmin spike results, and token state (no secrets)."""
    return spike.status()


mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    # Vercel terminates TLS and sets the Host header; the secret path is the guard in M0.
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
    sleep_s: float = 0


@app.post("/api/tick")
def tick(body: TickBody, x_gds_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """M0: record that the scheduler reached us, optionally outliving the caller's timeout."""
    _check_secret(x_gds_secret)
    spike.record_tick(body.source, "start", {"sleep_s": body.sleep_s})
    if body.sleep_s:
        time.sleep(min(body.sleep_s, 60))
        spike.record_tick(body.source, "end", {"sleep_s": body.sleep_s})
    return {"status": "ok"}


@app.post("/api/spike/garmin")
def spike_garmin(days: int = 2, x_gds_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _check_secret(x_gds_secret)
    return spike.garmin_probe(days=min(days, 3))


@app.post("/api/spike/refresh")
def spike_refresh(x_gds_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _check_secret(x_gds_secret)
    return spike.forced_refresh()

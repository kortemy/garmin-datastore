"""Connectivity log for the MCP `ping` tool (kept from M0: it proves scheduled tasks reach us)."""

from typing import Any

from psycopg.types.json import Jsonb

from gds.db import connect


def record_tick(source: str, phase: str, detail: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "insert into garmin.spike_ticks (source, phase, detail) values (%s, %s, %s)",
            (source, phase, Jsonb(detail or {})),
        )

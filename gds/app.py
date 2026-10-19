"""Vercel entrypoint: health, scheduler tick, manual sync, OAuth owner login, and the MCP connector.

Connector URL: {GDS_BASE_URL}/mcp (OAuth 2.1). The legacy secret-path URL
(/s/<MCP_PATH_SECRET>/mcp, no OAuth) is off unless LEGACY_SECRET_CONNECTOR=1.
"""

import hmac
import os
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from gds import alerts, engine, oauth, spike, status, strava, tools, workout_builder, writes
from gds.config import settings
from gds.db import connect

INSTRUCTIONS = (
    "Garmin Connect data (wellness, sleep, HRV, readiness, training load, activities with laps/zones/FIT "
    "streams, body measurements, performance metrics, gear, workouts, calendar) synced into a database "
    "every few hours. Call get_sync_status first and say plainly if data is missing or stale; never invent "
    "numbers. Dates are the athlete's local calendar dates (YYYY-MM-DD). Units are in field names.\n"
    "Write tools change the athlete's Garmin account. Only write when asked. First call the tool with "
    "dry_run=true, show the user the returned preview word for word, and call again with dry_run=false only "
    "after they explicitly confirm. Give every distinct write a new idempotency_key (e.g. a UUID) and reuse it "
    "only to retry the same write."
)
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Writes are idempotent per idempotency_key. "Additive" ones only add data; the others change or
# remove something (only things this system created, except activity edits and gear status).
ADD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)
CONFIRM = ("Changes Garmin: call with dry_run=true first, show the user the preview, and repeat with "
           "dry_run=false only after explicit confirmation. idempotency_key: a new UUID per write, the same "
           "one to retry.")


def build_server(**auth_kwargs: Any) -> MCPServer:
    mcp = MCPServer("garmin-datastore", instructions=INSTRUCTIONS, **auth_kwargs)

    @mcp.tool(annotations=READ)
    def ping(note: str = "") -> str:
        """Connectivity check. Records the call with an optional short note and returns 'pong'."""
        spike.record_tick("mcp", "ping", {"note": note[:100]})
        return f"pong {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"

    @mcp.tool(annotations=READ)
    def get_sync_status() -> dict[str, Any]:
        """How fresh and complete the data is: last successful sync, days covered, backfill progress,
        recent sync runs, failing items. Call this first."""
        return status.sync_status()

    @mcp.tool(annotations=READ)
    def get_daily_wellness(start: str | None = None, end: str | None = None,
                           fields: list[str] | None = None) -> dict[str, Any]:
        """One row per day (default: last 7 days): readiness (+factors, morning score), sleep score/stages/
        times, overnight HRV + status, resting HR, Body Battery, stress, steps, intensity minutes, training
        status, acute/chronic load, ACWR, load focus, VO2max. `fields` limits the columns returned."""
        return tools.daily_wellness(start, end, fields)

    @mcp.tool(annotations=READ)
    def get_sleep(start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """Nightly sleep detail (default last 7 nights): stages, score + sub-scores, respiration, HRV, sleep
        need, plus a stage timeline (local time) per night."""
        return tools.sleep(start, end)

    @mcp.tool(annotations=READ)
    def get_intraday(metric: str, date: str, bucket_minutes: int = 15) -> dict[str, Any]:
        """Intraday series for one day, bucketed: hr, stress, body_battery, respiration, spo2, steps,
        floors, hrv, sleep_stage. Use for questions like 'when did stress spike?'."""
        return tools.intraday(metric, date, bucket_minutes)

    @mcp.tool(annotations=READ)
    def list_activities(start: str | None = None, end: str | None = None, activity_type: str | None = None,
                        limit: int = 50) -> dict[str, Any]:
        """Activities (default last 30 days), newest first: type, name, distance, duration, pace, HR,
        training effect/load, RPE, weather, gear. activity_type filters by substring (e.g. 'running')."""
        return tools.list_activities(start, end, activity_type, limit)

    @mcp.tool(annotations=READ)
    def get_activity(activity_id: int, include: list[str] | None = None) -> dict[str, Any]:
        """Full detail for one activity. include: any of laps, zones, sets (strength), weather (default all)."""
        return tools.activity(activity_id, include)

    @mcp.tool(annotations=READ)
    def get_activity_streams(activity_id: int, fields: list[str] | None = None,
                             max_points: int = 500) -> dict[str, Any]:
        """Per-second samples from the activity's FIT file, downsampled to max_points: heart_rate_bpm,
        speed_mps, power_w, altitude_m, distance_m, cadence, vertical_oscillation_mm, ground_contact_ms,
        step_length_mm, temperature_c. Use for pacing/HR-drift analysis."""
        return tools.activity_streams(activity_id, fields, max_points)

    @mcp.tool(annotations=READ)
    def get_trend(metric: str, start: str | None = None, end: str | None = None, period: str = "week",
                  agg: str | None = None) -> dict[str, Any]:
        """Aggregate a metric per day/week/month (default last 90 days, weekly). Metrics: resting_hr, hrv,
        sleep_score, sleep_hours, readiness, steps, stress, body_battery_high, acute_load, chronic_load,
        vo2max, training_load, activity_minutes, running_km, weight_kg. agg: avg|min|max|sum|count."""
        return tools.trend(metric, start, end, period, agg)

    @mcp.tool(annotations=READ)
    def get_body(start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """Weigh-ins (kg, body composition) and blood pressure readings (default last 365 days)."""
        return tools.body(start, end)

    @mcp.tool(annotations=READ)
    def get_performance(metric: str | None = None, start: str | None = None,
                        end: str | None = None) -> dict[str, Any]:
        """Without metric: latest value of every performance metric (race predictions in seconds, lactate
        threshold, FTP, fitness age, VO2max, endurance/hill score). With metric: its history."""
        return tools.performance(metric, start, end)

    @mcp.tool(annotations=READ)
    def get_entities(kind: str, entity_id: str | None = None) -> dict[str, Any]:
        """Garmin objects by kind: device, gear, gear_stats, personal_record, goal, badge, workout,
        workout_detail (full steps), training_plan, course, profile, profile_settings, hr_zones,
        power_zones, event."""
        return tools.entities(kind, entity_id)

    @mcp.tool(annotations=READ)
    def get_calendar(start: str | None = None, end: str | None = None,
                     item_type: str | None = None) -> dict[str, Any]:
        """Garmin calendar items (default: last 7 days to 3 weeks ahead): scheduled workouts, activities,
        naps, events. item_type filters (e.g. 'workout')."""
        return tools.calendar_items(start, end, item_type)

    @mcp.tool(annotations=READ)
    def describe_schema() -> dict[str, Any]:
        """Tables and views available to query_sql, with columns and descriptions."""
        return tools.describe_schema()

    @mcp.tool(annotations=READ)
    def query_sql(sql: str) -> dict[str, Any]:
        """Read-only SQL (SELECT/WITH) over the garmin schema; 5 s timeout, 500 rows max. Call
        describe_schema first. Prefer the dedicated tools for common questions."""
        return tools.query_sql(sql)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=False))
    def request_resync(domain: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        """Queue a re-sync from Garmin (optionally for one domain and/or a date range); the next sync tick,
        within ~10 minutes, fetches it. Use when data looks stale or wrong."""
        with connect() as conn:
            acct = conn.execute("select id from garmin.accounts where label = %s",
                                (settings().account_label,)).fetchone()
            return {"queued_units": engine.request_resync(conn, acct["id"], domain, start, end)}

    _write_tools(mcp)
    return mcp


def _confirm(doc: str) -> str:
    return f"{doc}\n\n{CONFIRM}"


def _write_tools(mcp: MCPServer) -> None:
    """Write tools (requirements §4.6, design §8.3). All logic lives in gds.writes."""

    @mcp.tool(annotations=ADD, description=_confirm(
        "W-1. Log a weigh-in (kg), optionally with body composition. date YYYY-MM-DD and time HH:MM are local "
        "(default now). The preview shows the change from the last weigh-in."))
    def log_weight(idempotency_key: str, weight_kg: float, date: str | None = None, time: str | None = None,
                   body_fat_pct: float | None = None, body_water_pct: float | None = None,
                   muscle_mass_kg: float | None = None, bone_mass_kg: float | None = None,
                   visceral_fat_rating: float | None = None, metabolic_age: int | None = None,
                   dry_run: bool = True) -> dict[str, Any]:
        return writes.log_weight(idempotency_key, weight_kg, date, time, body_fat_pct, body_water_pct,
                                 muscle_mass_kg, bone_mass_kg, visceral_fat_rating, metabolic_age, dry_run)

    @mcp.tool(annotations=ADD, description=_confirm(
        "W-2. Create a structured workout in the Garmin library (it syncs to the watch once scheduled or "
        "sent). Optionally schedule it right away with schedule_date (YYYY-MM-DD). The dry run returns a "
        "step-by-step summary to show the user.\n\n" + workout_builder.SPEC_DOC))
    def create_workout(idempotency_key: str, spec: dict[str, Any], schedule_date: str | None = None,
                       dry_run: bool = True) -> dict[str, Any]:
        return writes.create_workout(idempotency_key, spec, schedule_date, dry_run)

    @mcp.tool(annotations=ADD, description=_confirm(
        "W-3. Put a library workout (workout_id from create_workout or get_entities kind='workout') on the "
        "Garmin calendar on date (YYYY-MM-DD) so it syncs to the watch."))
    def schedule_workout(idempotency_key: str, workout_id: int, date: str, dry_run: bool = True) -> dict[str, Any]:
        return writes.schedule_workout(idempotency_key, workout_id, date, dry_run)

    @mcp.tool(annotations=CHANGE, description=_confirm(
        "W-3. Move a scheduled workout to new_date. Only works for calendar entries this system scheduled "
        "(scheduled_workout_id from schedule_workout, or get_calendar item_id)."))
    def reschedule_workout(idempotency_key: str, scheduled_workout_id: int, new_date: str,
                           dry_run: bool = True) -> dict[str, Any]:
        return writes.reschedule_workout(idempotency_key, scheduled_workout_id, new_date, dry_run)

    @mcp.tool(annotations=CHANGE, description=_confirm(
        "W-3. Remove a scheduled workout from the calendar (the workout stays in the library). Only for "
        "entries this system scheduled."))
    def unschedule_workout(idempotency_key: str, scheduled_workout_id: int, dry_run: bool = True) -> dict[str, Any]:
        return writes.unschedule_workout(idempotency_key, scheduled_workout_id, dry_run)

    @mcp.tool(annotations=CHANGE, description=_confirm(
        "Delete a workout from the Garmin library (and its scheduled dates). Only for workouts this system "
        "created; the athlete's own workouts are never deleted."))
    def delete_workout(idempotency_key: str, workout_id: int, dry_run: bool = True) -> dict[str, Any]:
        return writes.delete_workout(idempotency_key, workout_id, dry_run)

    @mcp.tool(annotations=ADD, description=_confirm(
        "W-4. Add (or with a negative ml, subtract) water intake for a day (default today). Garmin adds it to "
        "the day's total: if the user states a new total, log the difference (the preview shows the synced "
        "total)."))
    def log_hydration(idempotency_key: str, ml: float, date: str | None = None, dry_run: bool = True) -> dict:
        return writes.log_hydration(idempotency_key, ml, date, dry_run)

    @mcp.tool(annotations=ADD, description=_confirm(
        "W-5. Log a blood pressure reading (mmHg, optional pulse bpm and note). date/time local, default now."))
    def log_blood_pressure(idempotency_key: str, systolic: int, diastolic: int, pulse: int | None = None,
                           date: str | None = None, time: str | None = None, notes: str | None = None,
                           dry_run: bool = True) -> dict[str, Any]:
        return writes.log_blood_pressure(idempotency_key, systolic, diastolic, pulse, date, time, notes, dry_run)

    @mcp.tool(annotations=CHANGE, description=_confirm(
        "W-6. Edit an activity: name, description (replaces the current notes), activity_type (Garmin type "
        "key, e.g. 'basketball', 'strength_training'), rpe (1-10), feel (0 very weak, 25, 50 normal, 75, 100 "
        "very strong), add/remove gear by uuid (get_entities kind='gear'). Previous values are kept so "
        "undo_write can restore them."))
    def update_activity(idempotency_key: str, activity_id: int, name: str | None = None,
                        description: str | None = None, activity_type: str | None = None, rpe: int | None = None,
                        feel: int | None = None, add_gear_uuid: str | None = None,
                        remove_gear_uuid: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        return writes.update_activity(idempotency_key, activity_id, name, description, activity_type, rpe, feel,
                                      add_gear_uuid, remove_gear_uuid, dry_run)

    @mcp.tool(annotations=ADD, description=_confirm(
        "W-7. Log a session done without the watch. activity_type is a Garmin type key (e.g. 'basketball', "
        "'strength_training', 'running'); start is local 'YYYY-MM-DDTHH:MM'. Created private; Garmin "
        "estimates calories."))
    def create_manual_activity(idempotency_key: str, activity_type: str, start: str, duration_min: float,
                               name: str, distance_km: float | None = None, description: str | None = None,
                               dry_run: bool = True) -> dict[str, Any]:
        return writes.create_manual_activity(idempotency_key, activity_type, start, duration_min, name,
                                             distance_km, description, dry_run)

    @mcp.tool(annotations=CHANGE, description=_confirm(
        "W-8. Gear. action='create' (brand, model, optional name, gear_type SHOES|BIKE|OTHER, "
        "first_use_date, max_distance_km, default_for activity types), 'retire' / 'activate' (gear_uuid), "
        "'set_defaults' (gear_uuid + default_for: the activity types it is the default for; [] clears)."))
    def manage_gear(idempotency_key: str, action: str, gear_uuid: str | None = None, brand: str | None = None,
                    model: str | None = None, name: str | None = None, gear_type: str = "SHOES",
                    first_use_date: str | None = None, max_distance_km: float | None = None,
                    default_for: list[str] | None = None, dry_run: bool = True) -> dict[str, Any]:
        return writes.manage_gear(idempotency_key, action, gear_uuid, brand, model, name, gear_type,
                                  first_use_date, max_distance_km, default_for, dry_run)

    if strava.configured():
        @mcp.tool(annotations=CHANGE, description=_confirm(
            "W-9. Set the title and/or description of an activity's copy on Strava (Garmin pushes activities to "
            "Strava on upload; later Garmin edits don't follow). activity_id is the GARMIN activity id; the "
            "matching Strava activity is found by start time and the dry run names it and shows its current "
            "title/description. also_garmin=true writes the same text to the Garmin activity. Typical use: "
            "title the session after its planned workout (get_calendar) and summarise what was done "
            "(get_activity)."))
        def update_strava_activity(idempotency_key: str, activity_id: int, name: str | None = None,
                                   description: str | None = None, also_garmin: bool = False,
                                   dry_run: bool = True) -> dict[str, Any]:
            return writes.update_strava_activity(idempotency_key, activity_id, name, description, also_garmin,
                                                 dry_run)

    @mcp.tool(annotations=CHANGE, description=_confirm(
        "Reverse an earlier write by write_id (see get_write_log): deletes a weigh-in, blood pressure reading, "
        "workout, schedule or manual activity this system created, subtracts logged water, or restores an "
        "activity's previous name/description/type/RPE/gear, or a Strava activity's previous title/description."))
    def undo_write(idempotency_key: str, write_id: int, dry_run: bool = True) -> dict[str, Any]:
        return writes.undo_write(idempotency_key, write_id, dry_run)

    @mcp.tool(annotations=READ)
    def get_write_log(limit: int = 20) -> dict[str, Any]:
        """Writes this system made to Garmin, newest first: tool, what was written, status, created ids,
        whether it was undone. Use for 'what did you change?' and to find a write_id for undo_write."""
        return writes.recent_writes(limit)


def _http_app(mcp: MCPServer):
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Vercel terminates TLS and sets Host; OAuth (or the secret path) is the guard.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )


_base_url = os.environ.get("GDS_BASE_URL", "").strip().rstrip("/")
servers: list[MCPServer] = []
oauth_app = None
if _base_url:
    oauth_mcp = build_server(
        auth_server_provider=oauth.DbOAuthProvider(_base_url),
        auth=AuthSettings(
            issuer_url=_base_url,
            resource_server_url=f"{_base_url}/mcp",
            validate_token_resource=False,  # tokens are opaque and only issued by this server
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
    servers.append(oauth_mcp)
    oauth_app = _http_app(oauth_mcp)

_legacy_secret = os.environ.get("MCP_PATH_SECRET", "").strip()
legacy_app = None
if _legacy_secret and os.environ.get("LEGACY_SECRET_CONNECTOR", "0") == "1":
    legacy_mcp = build_server()
    servers.append(legacy_mcp)
    legacy_app = _http_app(legacy_mcp)


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with AsyncExitStack() as stack:
        for s in servers:
            await stack.enter_async_context(s.session_manager.run())
        yield


app = FastAPI(title="garmin-datastore", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


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


@app.post("/api/alerts/test")
def alerts_test(x_gds_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """Send a test alert with the deployed configuration (checks the Vercel env vars)."""
    _check_secret(x_gds_secret)
    email_id = alerts.send_email(
        "[garmin-datastore] test alert (app)",
        "This is a test of garmin-datastore's alert path from the Vercel app.\nNo action needed.",
    )
    return {"sent": email_id is not None, "email_id": email_id}


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


# ---------------------------------------------------------------- OAuth owner login


@app.get("/oauth/login", response_class=HTMLResponse)
def oauth_login_form(request: str) -> HTMLResponse:
    pending = oauth.pending_request(request)
    if not pending:
        return HTMLResponse(oauth.login_page(request, None, "This sign-in link has expired. "
                                                            "Start the connection again from Claude."), 400)
    return HTMLResponse(oauth.login_page(request, pending["client_name"]))


@app.post("/oauth/login")
def oauth_login_submit(request: str = Form(...), password: str = Form(...)):
    redirect, error = oauth.complete_login(request, password, os.environ.get("OWNER_PASSWORD_HASH", ""))
    if error:
        pending = oauth.pending_request(request)
        return HTMLResponse(oauth.login_page(request, pending["client_name"] if pending else None, error), 400)
    return RedirectResponse(redirect, status_code=302)


# MCP apps are mounted last so the routes above take precedence.
if legacy_app is not None:
    app.mount(f"/s/{_legacy_secret}", legacy_app)  # legacy connector URL: /s/<secret>/mcp
if oauth_app is not None:
    app.mount("/", oauth_app)  # /mcp, /authorize, /token, /register, /revoke, /.well-known/*

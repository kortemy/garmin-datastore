# Garmin Datastore — Technical Design

| | |
|---|---|
| **Status** | v0.2: M0 spike results added (§15) |
| **Date** | 2026-10-17 |
| **Requirements** | [requirements.md](requirements.md) v0.3 |

This document describes **how** to build what the requirements describe. FR/NFR/D
numbers refer to the requirements doc.

---

## 1. Overview

```
                ┌──────────────────────── Supabase (free) ─────────────────────────┐
                │                                                                   │
 pg_cron ──────►│ every 10 min: pg_net POST /api/tick  (shared secret from Vault)   │
 (scheduler)    │ daily: staleness watchdog ──► Resend email (independent of Vercel)│
                │                                                                   │
                │ Postgres  schema `garmin`: typed tables, intraday series,          │
                │           work queue, run log, token vault, write log, OAuth       │
                │ Storage   private bucket: raw JSON (gzip) + FIT files               │
                └───────────────▲───────────────────────────────▲───────────────────┘
                                │ psycopg (pooler)               │
                ┌───────────────┴──────── Vercel (Hobby) ────────┴───────────────────┐
                │ one Python (ASGI) app                                               │
                │   /api/tick      sync engine: plan → run work units → persist      │
                │   /api/sync      manual trigger (date range / domain)              │
                │   /mcp           MCP server: read tools + write tools (OAuth)      │
                │   /oauth/*       minimal OAuth 2.1 server for the Claude connector  │
                └───────────────┬────────────────────────────────▲───────────────────┘
                                │ garminconnect (unofficial API)  │ MCP (streamable HTTP)
                         Garmin Connect                    Claude (Fitness project:
                                                           chats + scheduled review)

 GitHub: private repo, CI (lint/tests/migrations), daily backup tick workflow
 Local:  `gds` CLI: one-time Garmin login → encrypted token upload; local dev runs
```

**One codebase, one deployable.** The sync engine, the MCP connector, and the CLI share
the same Python package. Vercel hosts the HTTP entry points. The same engine can run
from the CLI on any machine, which is the fallback if Vercel ever can't reach Garmin
(§12).

## 2. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language | **Python 3.12** | `garminconnect` (≥0.3) requires 3.12; it's the most complete Garmin client (~350 methods incl. all W-1..W-8 writes) |
| Garmin client | **`garminconnect` 0.3.x, pinned** + `curl_cffi` | Post-`garth` auth (DI OAuth tokens, auto-refresh). Pinned because it's unofficial and changes often; upgrades go through CI |
| Web framework | **FastAPI** (ASGI) on Vercel's Python runtime | Single app for tick/sync/MCP/OAuth; first-class on Vercel |
| MCP | Official **`mcp` Python SDK** (FastMCP), streamable HTTP, **stateless** mode | Stateless suits serverless; SDK includes OAuth server plumbing |
| DB access | **psycopg 3** over Supabase's **transaction pooler** | Real transactions, batch upserts, `FOR UPDATE` locks. The REST API (PostgREST) can't do these well |
| Migrations | **Supabase CLI** (`supabase/migrations/*.sql`) | Versioned SQL, applied by CI / `supabase db push` |
| Encryption | `cryptography` (AES-256-GCM) | Garmin token encrypted at rest with a key that only lives in Vercel env |
| Email | **Resend** HTTP API (free tier) | Simplest free sender; called from both Python and Postgres (`pg_net`) |
| Tooling | uv, ruff, pytest, pyright | Fast, standard |

**Vercel Hobby limits that shape the design:** functions run up to **300 s** (Fluid
compute), 2 GB memory, 500 MB Python bundle, cron once a day. So all work is cut into
small **work units** processed in **time-boxed ticks**, and the schedule comes from
Supabase `pg_cron` (D-1).

## 3. Repository layout

```
garmin-datastore/
├─ gds/                          # package at repo root; Vercel entry = gds.app:app (M0)
│  ├─ config.py                  # all settings from env (FR-21)
│  ├─ app.py                     # FastAPI app: routes for tick, sync, health, mcp, oauth
│  ├─ source/                    # FR-23 source adapter
│  │  ├─ base.py                 #   GarminSource protocol (reads + writes), error types
│  │  └─ garminconnect_src.py    #   implementation on garminconnect
│  ├─ auth/token_broker.py       # load/refresh/persist Garmin tokens, single-flight
│  ├─ domains/                   # one module per data domain (registry, §5)
│  ├─ engine/                    # planner, runner, throttle, leases, cursors
│  ├─ store/                     # db (psycopg), raw store (Storage), upsert helpers
│  ├─ mcp/                       # read tools, write tools, workout builder
│  ├─ oauth/                     # OAuth 2.1 provider for the connector
│  ├─ alerts/                    # Alerter protocol + Resend implementation
│  └─ cli.py                     # `gds` CLI: auth, tick, sync, backfill status, doctor
├─ supabase/
│  ├─ config.toml
│  └─ migrations/                # schema, roles, RLS, pg_cron jobs, views
├─ tests/                        # unit (normalizers, planner), integration (Postgres)
│  └─ fixtures/                  # sanitized real Garmin responses
├─ .github/workflows/            # ci.yml, backup-tick.yml
├─ docs/                         # requirements, design, setup guide
├─ .env.example  pyproject.toml  uv.lock  vercel.json  LICENSE (MIT)  README.md
```

## 4. Data model

All tables live in a dedicated Postgres schema **`garmin`**, which is **not exposed**
through Supabase's REST API. Every table has **`account_id uuid`** as the first key
column (FR-22). RLS is enabled on every table with no policies for `anon`/`authenticated`,
so nothing is reachable through Supabase client keys (NFR-2).

### 4.1 Database roles

| Role | Used by | Rights |
|---|---|---|
| `gds_worker` | sync engine, write tools | read/write on `garmin.*` |
| `gds_reader` | MCP read tools and `query_sql` | `SELECT` on the curated views + typed tables only; `statement_timeout = 5s` |
| `postgres` | migrations only | owner |

> **As built (M6):** `gds_reader` is a **NOLOGIN** role with read policies, entered via
> `SET LOCAL ROLE` from the worker (no extra password). The deployed app logs in as
> **`gds_worker`** through the pooler (BYPASSRLS, DML on `garmin.*`, no Vault, no DDL);
> `postgres` is used only from the owner's machine (`gds migrate`, `gds setup`, CLI). See §23.

### 4.2 Core / operational tables

| Table | Purpose |
|---|---|
| `accounts` | One row per Garmin account: Garmin user id, display name, home timezone, units, backfill start date, status (`active` / `needs_reauth` / `cooldown`), cooldown until |
| `garmin_tokens` | Encrypted token blob (AES-GCM ciphertext + nonce), access-token expiry, `version` counter, updated_at |
| `leases` | Single-row-per-name locks with expiry (`tick:<account>`) so only one tick runs at a time |
| `work_units` | The sync queue: `(account_id, domain, scope_key)` unique; status `pending/running/done/empty/failed`; attempts, `next_attempt_at`, last_error, last_fetched_at, `content_hash`, priority |
| `sync_runs` | Run log (FR-12): trigger, start/end, status, units done/failed/empty, requests, bytes, per-domain counts (jsonb), errors |
| `raw_objects` | Index of raw payloads in Storage: `(account_id, domain, scope_key)` → path, content_hash, bytes raw/gzip, fetched_at |
| `write_log` | Every Garmin write (FR-19): tool, input, preview, Garmin response, status, created entity kind/id, `created_by_system` flag (FR-18), idempotency key |
| `alerts` | Sent alerts with condition + dedupe window |
| `oauth_clients`, `oauth_codes`, `oauth_tokens` | Connector OAuth state (hashed tokens only) |

### 4.3 Typed data tables

Daily-keyed tables use `calendar_date date` (the Garmin local calendar date) and store
`utc_offset_min` taken from that day's own payload, which handles travel (NFR-5).

| Table | Grain | Contents |
|---|---|---|
| `daily_summary` | account × date | Steps, distance, floors, calories (active/BMR/total), intensity minutes, resting HR, min/max HR, avg/max stress + stress durations, Body Battery high/low/charged/drained, SpO2 avg/low, respiration avg, hydration, utc offset |
| `sleep` | account × date | Start/end (UTC + local), total/deep/light/REM/awake seconds, stage %, score + sub-scores + qualifier, avg respiration/SpO2/HR/stress overnight, Body Battery change, restless moments, nap list (jsonb) |
| `hrv` | account × date | Last-night avg, 5-min high, weekly avg, baseline low/high, status, feedback |
| `training_readiness` | account × reading timestamp | Every intraday reading: score, level, feedback short/long, all factor %s, recovery time, acute load |
| `training_status` | account × date | Status/phrase, acute/chronic load, ACWR + % + status, weekly load, load-balance focus + targets, VO2max (run/cycle), heat/altitude acclimation |
| `performance_metrics` | account × date × metric | Long-tail scalars in one table: race predictions (5k/10k/HM/M), endurance score, hill score, fitness age, lactate threshold HR/pace, FTP, etc. (`value numeric`, `detail jsonb`) |
| `intraday_series` | account × date × metric | **One row per metric per day, values as arrays** (§4.4) |
| `activities` | account × activity_id | Summary: type, name, description, start (UTC + local + offset), duration/moving/elapsed, distance, elevation, speed/pace, HR avg/max, power, cadence, calories, training effect (aerobic/anaerobic + label), training load, VO2max, gear ids, device, location name, manual flag, `fit_object` path |
| `activity_laps` / `activity_splits` | activity × index | Per lap/split metrics |
| `activity_zones` | activity × zone type × zone | Time in HR/power zones |
| `activity_exercise_sets` | activity × set | Strength sets: exercise category/name, reps, weight, duration |
| `activity_weather` | activity | Temperature, humidity, wind, conditions |
| `weigh_ins` | account × timestamp | Weight, BMI, body fat %, body water %, bone/muscle mass, visceral fat, metabolic age, source |
| `blood_pressure` | account × timestamp | Systolic, diastolic, pulse, notes |
| `entities` | account × kind × entity_id | **Snapshot domains** as jsonb docs + a few typed columns: profile, settings, devices, gear (+ stats), personal records, goals, badges, challenges, workouts, training plans, calendar items |

### 4.4 Intraday data: arrays, not rows

Storing one row per sample (HR every ~2 min, stress/Body Battery every ~3 min, etc.)
would cost ~1M+ rows a year and blow the free-tier database (requirements §9). Instead:

```sql
create table garmin.intraday_series (
  account_id     uuid not null,
  calendar_date  date not null,
  metric         text not null,        -- 'hr','stress','body_battery','respiration','spo2','steps','sleep_stage','hrv','intensity'
  start_utc      timestamptz not null, -- first sample
  utc_offset_min int not null,
  ts_offsets_s   int[] not null,       -- seconds since start_utc, one per sample
  vals           real[] not null,      -- ("values" is reserved in SQL)
  primary key (account_id, calendar_date, metric)
);
```

A view `garmin.v_intraday_points` unnests the arrays into `(ts_utc, ts_local, value)`
rows for ad-hoc SQL, and a SQL function `garmin.intraday_bucketed(metric, date, bucket)`
returns averages per 15 min / 1 h for Claude. This replaces the truncated 24-slot
Notion timelines with full-resolution data at a fraction of the size.

Per-second **activity streams** (GPS, HR, power) stay inside the **FIT file** in Storage.
They're parsed on demand by an MCP tool (`get_activity_streams`, downsampled) rather
than expanded into rows.

### 4.5 Raw storage

> **Changed in M1:** raw payloads live **in Postgres** (`garmin.raw_objects.payload_gz`,
> gzip `bytea`, TOAST compression off) instead of a Storage bucket. The measured volume
> (§13, ~50 MB for full history) fits the database comfortably. Raw data is written in
> the **same transaction** as its normalised rows, and Storage would have needed a
> second secret (service key) on Vercel. FIT files (M2) will be judged the same way.

Every Garmin response is stored as canonical JSON, gzipped, keyed by
`(account_id, domain, scope_key)` (FR-2). Only the **latest** version is kept, since
Garmin is the source of truth. The SHA-256 `content_hash` on each work unit lets the
engine skip re-normalising unchanged payloads, which keeps the frequent recent-window
re-fetches cheap. If a normaliser throws, the raw payload is still saved and the unit
retries, so a mapping bug never loses data. Cold storage for old data (D-9) would move
rows by `scope_key` date.

### 4.6 Views for consumers

Curated views are the stable contract for Claude and `query_sql`, so table internals
can change without breaking prompts:

- `v_daily_wellness`: one row per day joining summary + sleep + HRV + latest readiness
  + training status. **Superset of the Notion Wellness Log columns** (FR-3).
- `v_activities`: activities with type, gear names, and zone summary.
- `v_body`: weigh-ins and blood pressure.
- `v_sync_health`: last success per domain, backlog size, backfill %, account status.

## 5. Domain registry

Each data domain is a small module declaring **how to fetch** and **how to normalise**.
The engine is generic. Adding a domain means adding a module and a migration.

```python
@domain(
    name="sleep",
    scope=Scope.DAILY,            # DAILY | RANGE | ACTIVITY | SNAPSHOT
    recent_window_days=3,         # re-fetched on every incremental run (FR-7)
    priority=10,
)
def fetch(src: GarminSource, date: str) -> Any:
    return src.call("get_sleep_data", date)

def normalize(raw: Any, ctx: NormalizeCtx) -> Rows:
    return Rows(sleep=[...], intraday_series=[...])   # upserted by the store
```

| Scope | Unit key | Examples | Incremental behaviour |
|---|---|---|---|
| `DAILY` | calendar date | summary, sleep, HRV, stress, Body Battery, HR, respiration, SpO2, steps, readiness, training status, hydration, intensity minutes, perf metrics | Re-fetch last N days each run; backfill one date per unit |
| `RANGE` | month | weigh-ins, blood pressure, Body Battery events | Re-fetch current + previous month |
| `ACTIVITY` | activity id | details, laps, splits, zones, exercise sets, weather, FIT file | New activities from the list endpoint; re-fetch activities modified in the last 2 days |
| `SNAPSHOT` | `singleton` | profile, settings, devices, gear + stats, PRs, goals, badges, challenges, workouts, training plans, calendar | Once a day (calendar every run, since writes touch it) |

The exact endpoint list per domain comes from the payload probe (§13) and is kept in
one table in code, so "everything" (FR-1) is auditable in a single file. Endpoints
that return nothing for the account (e.g. women's health) are marked `empty` and
re-checked monthly.

## 6. Sync engine

### 6.1 The tick

`pg_cron` calls `POST /api/tick` **every 10 minutes** (D-1). A tick:

1. **Acquires the lease** `tick:<account>` (`UPDATE … WHERE expires_at < now()
   RETURNING`). If held, it exits immediately. One tick at a time per account.
2. Checks account status: `needs_reauth` → exit; `cooldown` → exit until the cooldown ends.
3. **Plans**: ensures work units exist for anything due:
   - every **3 h** (configurable): recent-window units for DAILY/RANGE domains, the
     activity list scan, and SNAPSHOT domains once a day;
   - **backfill**: all DAILY dates from `backfill_start` to today and all discovered
     activities, generated once and worked **newest → oldest**, so the most useful
     data lands first.
4. **Runs** units by priority (incremental > write follow-ups > backfill) until a
   **time budget of ~240 s** is used, leaving headroom under the 300 s limit.
5. Writes the `sync_runs` row, persists the token if it changed, releases the lease.

Outside the 3-hourly window and after backfill, most ticks find nothing due and finish
in about a second.

### 6.2 Work unit execution

```
fetch (throttled) → gzip + hash → if hash changed: upload raw, normalise, upsert in one transaction
                                 → mark unit done / empty
```

- **Idempotent** (FR-8): every typed table upserts on its natural key, and raw objects
  are overwritten by path.
- **Isolated** (NFR-4): a failing unit never fails the tick. It gets
  `attempts += 1` and `next_attempt_at` with exponential backoff (5 min → 6 h), and goes
  `failed` after 6 attempts (visible in `v_sync_health`, retried by a manual sync).
- **Schema drift**: normalisers read defensively (missing keys → null, same `get(...)`
  helper style as today's collector). Unknown fields are preserved in raw.

### 6.3 Throttling & Garmin errors

| Condition | Response |
|---|---|
| Normal | Token bucket: ~1 request/s with jitter, at most 2 concurrent; shared across all units in a tick |
| HTTP 429 | Stop the tick; account `cooldown` for 15 min, doubling up to 4 h |
| HTTP 403 / Cloudflare challenge | Stop; cooldown 1 h; after 3 in a row → alert "Garmin blocking cloud requests" (§12) |
| 401 / refresh fails | Account `needs_reauth`, stop everything, alert with re-auth steps (FR-11). **Never retries login.** |
| 404 / empty | Unit `empty` |
| 5xx / timeout | Unit retry with backoff |

### 6.4 Backfill estimate

From the probe (§13): about 600 days × ~25 deduplicated day-scoped endpoints ≈ 15,000
requests, plus 171 activities × ~16 requests ≈ 2,700, so **~18,000 requests**. At ~1
request/s inside 240 s ticks every 10 min, that's about **13 h of wall-clock time**,
using ~5 h of function time. That's well within Hobby limits. The tick interval can be
lowered to 5 min during backfill to roughly halve that.

### 6.5 Manual sync (FR-9)

`POST /api/sync` (shared secret) and `gds sync` accept `{domain?, start?, end?}`. They
reset matching units to `pending` with top priority. The next tick (or the call
itself, if short) processes them. The MCP tool `request_resync` does the same from Claude.

**Sync now** (`sync_now` tool, `gds sync-now`): for "I just finished, fetch it". It queues
`activity_index/recent`, the daily domains of the last `days` local days (default 1), and
still-pending detail units of activities from the last 2 days, all at top priority. Then it
runs a tick in the call with a **75 s budget**. A unit processed at manual priority passes that
priority to the units its normaliser creates (`ensure_units(boost=…)`), so a new activity's
summary, laps, zones and FIT come in the same run. Re-queued finished units keep their own
priority. Calls closer than 60 s apart are refused. If another tick holds the lease, the units
stay queued at top priority for it or for the next scheduled tick.

## 7. Garmin authentication & token broker

### 7.1 How tokens work (garminconnect ≥0.3)

`garminconnect` 0.3 dropped `garth`. It now logs in through Garmin's mobile SSO flow
and holds **DI OAuth tokens**: a bearer token (JWT, **~21.5 h** lifetime as measured), a
refresh token, and a client id. `login(tokenstore)` accepts **inline JSON** as well as a path,
and `client.dumps()` serialises the current tokens. The access token is refreshed
automatically **15 min before expiry**, and Garmin **may return a new refresh token**
on refresh.

That last point drives the design: if two processes refresh the same refresh token at
once, one can end up holding a dead token, which means a forced re-auth.

### 7.2 Design

- **Separate token family for the cloud.** `gds auth` does a **fresh login** (email +
  password; no MFA, D-4) on my machine rather than copying `~/.garminconnect`. The local
  `garmin-connect-mcp` and the cloud worker then never share a refresh token.
- `gds auth` encrypts the token JSON with `TOKEN_ENC_KEY` (AES-256-GCM) and upserts
  it into `garmin_tokens`. The password is used once in memory and never stored (NFR-2).
- **Token broker** (`get_source(account)`), used by both the tick and MCP tools:
  1. Read the token row; decrypt.
  2. If the access token expires within 20 min: `SELECT … FOR UPDATE` on the row,
     re-read (someone else may have refreshed), refresh via the client if still stale,
     persist the new encrypted tokens and bump `version`, commit.
  3. Return a client whose tokens are fresh for the next ≥20 min. Ticks are ≤5 min,
     so the library's own auto-refresh never fires mid-run and there's no concurrent
     refresh.
  4. After any run, compare `dumps()` with what was loaded and persist if different
     (belt and braces).
- **Re-auth** (FR-11): on a refresh failure, the account goes to `needs_reauth`, and an
  email says "run `gds auth` on your PC". Nothing in the cloud ever attempts a
  password login.

## 8. MCP connector

### 8.1 Transport & auth

- FastMCP mounted at `/mcp`, **streamable HTTP, stateless**: each request
  authenticates, opens a DB connection from the pool, and does its work.
- **OAuth 2.1** with Dynamic Client Registration, which is what Claude's custom
  connectors support. We implement the SDK's `OAuthAuthorizationServerProvider` backed
  by the `oauth_*` tables. The `/oauth/authorize` page is a minimal **owner login**
  (password checked against `OWNER_PASSWORD_HASH`, argon2). Access tokens last 1 h and
  refresh tokens 90 days; both are stored hashed.
- Later, for multi-user, the owner login is swapped for Supabase Auth. The provider
  interface stays the same (FR-22/23).
- *(M0 only: a long random secret URL path to test connectivity before OAuth exists.
  It is removed in M4.)*

### 8.2 Read tools

All run as `gds_reader` and return compact JSON with units in field names (`_kg`, `_s`, `_bpm`).

| Tool | Purpose |
|---|---|
| `get_sync_status` | Freshness, backfill %, account status. Claude calls this first and states data gaps honestly |
| `get_daily_wellness(start, end, fields?)` | Rows from `v_daily_wellness`, the main recovery tool |
| `get_sleep(start, end)` | Sleep detail incl. stage timeline summary |
| `get_intraday(metric, date, bucket="15m")` | Bucketed series, e.g. "when did stress spike?" |
| `list_activities(start, end, type?)` | Activity summaries |
| `get_activity(activity_id, include=[laps, splits, zones, sets, weather])` | Activity detail |
| `get_activity_streams(activity_id, fields, max_points=500)` | Parsed from FIT, downsampled |
| `get_trend(metric, start, end, period="week", agg="avg")` | Server-side aggregation (HRV, RHR, load, sleep, weight…) |
| `get_body(start, end)` | Weigh-ins, body composition, blood pressure |
| `get_entities(kind)` | Gear, devices, PRs, goals, workouts, calendar |
| `describe_schema` / `query_sql(sql)` | Escape hatch: read-only SQL on views, 5 s timeout, 500-row cap |
| `request_resync(domain?, start?, end?)` | Queue a manual re-sync (FR-9) |
| `sync_now(days=1)` | Fetch the newest activities + the last `days` days now, in the call (§6.5) |

### 8.3 Write tools (§4.6 of requirements)

| Tool | Garmin method(s) | Req |
|---|---|---|
| `log_weight(weight_kg, date?, time?, body_fat_pct?, …)` | `add_weigh_in_with_timestamps` / `add_body_composition` | W-1 |
| `create_workout(spec, schedule_date?)` | `upload_workout` (JSON from `workout_builder`) | W-2 |
| `schedule_workout(workout_id, date)`, `reschedule_workout`, `unschedule_workout`, `delete_workout` | `schedule_workout`, `reschedule_workout`, `unschedule_workout`, `delete_workout` | W-3 |
| `log_hydration(ml, date?)` | `add_hydration_data` | W-4 |
| `log_blood_pressure(sys, dia, pulse, time?, notes?)` | `set_blood_pressure` | W-5 |
| `update_activity(activity_id, name?, description?, activity_type?, rpe?, feel?, add/remove_gear_uuid?)` | `update_activity` (one PUT), `add/remove_gear_to/from_activity` | W-6 |
| `create_manual_activity(activity_type, start, duration_min, name, distance_km?)` | `create_manual_activity_from_json` | W-7 |
| `manage_gear(action, …)` | `create_gear`, `set_gear_status`, `update_gear` (defaults) | W-8 |
| `undo_write(write_id)`, `get_write_log` | the matching delete / restore | FR-18/19 |

*(Updated in M5; see §21 for what changed from the original plan.)*

**Workout spec.** `create_workout` takes a **simple, documented JSON spec** that Claude
can write reliably (steps of warm-up / run / recover / repeat(n) / cool-down with
`duration` (time/distance/lap-button) and `target` (pace range / HR zone / HR range /
none), or strength exercises with sets/reps/weight). A `workout_builder` module
translates it into Garmin's workout JSON and validates it (unknown exercise names are
rejected with suggestions).

**Safety rails (FR-17 to FR-19):**
- Write tools carry MCP annotations (`readOnlyHint: false`, `destructiveHint` where
  relevant), so Claude's client applies its per-tool approval prompts. Tool
  descriptions instruct Claude to restate exactly what will be written and get explicit
  confirmation first.
- Every call takes an **idempotency key**, so a retried call doesn't log the weight twice.
- **Ownership check**: `reschedule/unschedule/delete` only act on entities whose
  `write_log` row says `created_by_system`. Anything else is refused with an explanation.
- No delete tools for activities, weigh-ins, or health records not created by the system.
- After success, the affected domain/date is queued for **immediate re-sync** (and run
  inline when it's a single cheap unit), so Supabase reflects the write (FR-19).

### 8.4 Strava title & description (W-9, milestone M5b)

Write-only: no Strava data is synced or stored (requirements §6). It reuses the M5 write
path (`writes.execute`: dry run, idempotency key, `write_log`, undo).

**Auth.** My own Strava API app (strava.com/settings/api), "Authorization Callback Domain"
set to `gds.lilic.dev` (a bare domain; Strava always allows `localhost`, which the CLI flow
below uses, so the domain only matters for a possible hosted flow later). `STRAVA_CLIENT_ID` and
`STRAVA_CLIENT_SECRET` go in `.env` → Vercel via `gds setup` (secret via a hidden prompt).
`gds strava-auth` runs on my machine: it opens Strava's consent page with scopes
`activity:write,activity:read_all` (`read_all` so private activities can be matched),
catches the redirect on a one-shot `localhost` listener, exchanges the code, and stores the
token **encrypted** (same `TOKEN_ENC_KEY`, AES-256-GCM) in a new `garmin.strava_tokens` row
(account, ciphertext, nonce, athlete id, scopes, status, updated_at). RLS on, no reader access.
Access tokens last ~6 h and a refresh may return a new refresh token, so refreshes follow
the Garmin broker's rule: only under `SELECT … FOR UPDATE`, only with < 20 min left, and
the new pair is persisted before use. A 401 / revoked grant sets `status = needs_reauth`
and sends an alert ("run `gds strava-auth`"). Without the env vars the Strava tool isn't
registered, so other deployments are unaffected (FR-20/21).

**Tool.** `update_strava_activity(idempotency_key, activity_id, name?, description?,
also_garmin=false, dry_run=true)`, where `activity_id` is the **Garmin** activity id.
1. Look up the Garmin activity (`activities`: start UTC, elapsed time, type).
2. Find its Strava copy: `GET /athlete/activities?after=…&before=…` around the start, keep
   activities whose `start_date` is within 2 min of Garmin's start; ties prefer Garmin uploads
   (`external_id` `garmin_ping_…`), then an exact start (§22). **Exactly one** match or the tool refuses,
   saying why (not uploaded yet, several candidates).
3. Preview: the Strava activity (id, current title, start, type) and the new title/description;
   with `also_garmin`, the same change on the Garmin activity.
4. Write: `PUT /api/v3/activities/{id}` with `{name, description}`. `undo` keeps the previous
   values, so `undo_write` restores them. With `also_garmin`, the Garmin edit is a second,
   linked write (`<key>:garmin`) through `update_activity`.

`write_log.entity_kind = 'strava_activity'`, `entity_id` = Strava id. Calls go through a small
`StravaClient` (httpx, already installed with the MCP SDK; explicit User-Agent) with errors
mapped like the Garmin source: 429 → retry later, 401 → re-auth, 404 → no such activity.
Strava's per-app rate limits are orders of magnitude above this use.

**Composing the text** stays with Claude (requirements §6, coaching logic): it reads the
planned workout from the activity's `workout_id` / `workout_name` (Garmin's own link, §22;
`get_calendar` for scheduled ones) and what was done from `get_activity`, then writes a short
title and description for confirmation.

**Before building:** read Strava's current API agreement (requirements §11); W-9 is dropped
if it rules this use out.

## 9. Scheduling & monitoring

| Job | Where | Schedule | What |
|---|---|---|---|
| `gds_tick` | Supabase `pg_cron` + `pg_net` | every 10 min | `POST {VERCEL_URL}/api/tick` with `X-GDS-Secret` (secret stored in Supabase **Vault**) |
| `gds_watchdog` | Supabase `pg_cron` | daily 06:00 UTC | If no successful run in 24 h, account `needs_reauth`, or units stuck failed → email via **Resend directly from Postgres** (`pg_net`), so alerts work even if Vercel is down |
| `backup-tick` | GitHub Actions | daily 04:17 UTC | `curl` the tick endpoint, in case `pg_cron` breaks (repo secrets `GDS_BASE_URL`, `TICK_SECRET`, set by `gds setup`). A failed run makes GitHub email the owner |
| CI | GitHub Actions | on push/PR | ruff + pytest; gitleaks over the whole history; every migration applied with `gds migrate` to a fresh `supabase/postgres` service container, then role/job/privilege checks |

**Alerting from the app** (FR-13): `Alerter` protocol with a `ResendAlerter`
implementation. Alert conditions: re-auth needed, Garmin blocking (403 ×3), repeated
tick failures. Deduped through the `alerts` table (one email per condition per 24 h).
The weekly Claude review also calls `get_sync_status` and mentions stale data
(soft backup, D-3).

## 10. Security

| Secret | Lives in | Used by |
|---|---|---|
| `DATABASE_URL` (pooler, `postgres`) | local `.env` only | `gds migrate`, `gds setup`, local CLI |
| `WORKER_DATABASE_URL` (pooler, `gds_worker`) | local `.env` → Vercel env as `DATABASE_URL` | app (read tools drop to `gds_reader` via `SET LOCAL ROLE`) |
| `TOKEN_ENC_KEY` | Vercel env, local `.env` (for `gds auth`) | token broker |
| `TICK_SECRET` | Vercel env + Supabase Vault + GitHub secret | `/api/tick`, `/api/sync`, backup tick |
| `OWNER_PASSWORD_HASH` | Vercel env | OAuth owner login (tokens are opaque, hashed in the DB; no signing key) |
| `RESEND_API_KEY`, `ALERT_EMAIL` | Vercel env + Supabase Vault | alerts |
| `STRAVA_CLIENT_ID` / `_SECRET` | Vercel env | W-9 |

- No secrets in git: `.env` is git-ignored, `.env.example` is committed, and CI runs a
  secret scanner (gitleaks) so the repo stays publishable (FR-24).
- Garmin token encrypted at rest. A DB dump alone doesn't expose it.
- `garmin` schema isn't exposed via REST. RLS is on with no permissive policies.
  The Storage bucket is private.
- `query_sql` runs as `gds_reader`, read-only transaction, statement timeout, row cap.
- All HTTP endpoints authenticate (shared secret or OAuth). The health endpoint
  returns only `ok`.

## 11. Time handling (NFR-5)

- Daily rows are keyed by Garmin's `calendarDate`, never by "today in CET".
- Each day stores `utc_offset_min` from its own payload (`startTimestampGMT` vs
  `startTimestampLocal`, the same method as today's `_offset_ms`). Travel days are
  correct by construction.
- Garmin's "*Local" epoch-ms fields (already shifted to wall time) are converted with
  that day's offset and stored as proper `timestamptz` UTC plus local wall time.
- Activities store `start_utc`, `start_local`, and `utc_offset_min` from
  `startTimeGMT`/`startTimeLocal`.
- The "every 3 h" schedule and the 08:00 watchdog use the account's home timezone from
  config (an IANA zone in CET/CEST, set at deploy), but nothing data-related depends on it.

## 12. Risks & fallbacks

| Risk | Detection | Fallback |
|---|---|---|
| **Garmin blocks Vercel IPs** (biggest risk) | M0 spike; 403 alerts in production | The engine is host-agnostic: run `gds tick` from **GitHub Actions** (different IP ranges, free minutes) or from an always-on home device (Raspberry Pi / NAS), with the same DB and the same code |
| `curl_cffi` binary fails on Vercel | M0 spike | Use the library's non-impersonating strategy; else the fallback hosts above |
| Refresh-token rotation conflicts | Token `version` + refresh audit in `sync_runs` | Single-flight broker (§7.2); separate token family from local MCP |
| Library breaking change | CI with recorded fixtures; pinned version | Upgrade deliberately; the source adapter isolates call sites |
| Write endpoint misbehaves | Write log + immediate re-sync shows the result | Disable that tool by config flag |
| Claude scheduled tasks can't use custom connectors | M0 spike | Weekly review falls back to the Supabase MCP connector (read-only views) for reads, with writes only in interactive chats |
| Free-tier volume | `v_sync_health` shows DB + Storage size | Cold storage (D-9) |

## 13. Measured payload sizes

Measured on 2026-10-17 with a local **read-only probe** (`garminconnect` 0.3.19):
every `get_*`/`download_*` method that takes a date, a date range, an activity id, or
nothing, called for the 3 most recent days and the 5 most recent activities. About
390 requests ran at ~1 request/s with **no rate limiting** (from a home IP; cloud IPs
are tested in M0).

### 13.1 What the account has

- **~260 read endpoints** in the library: 34 per-date, 50 per-range, 101 parameterless,
  26 per-activity, and 48 with other signatures (reviewed by hand when building the
  registry). About 75% return data for this account. Empty ones include women's health,
  ECG, golf, jet lag, health snapshots, and heat/altitude acclimation.
- **171 activities** over 20 months (Feb 2025 → Oct 2026), about 8–9 a month: running
  121, soccer 24, strength 9, padel 7, basketball 4, open-water swim 3, walking 2,
  indoor cardio 1. Test fixtures must cover each of these types.

### 13.2 Sizes

| Data | Raw | Gzipped | Notes |
|---|---|---|---|
| Daily endpoints with data (29) | 253 KB/day | **42 KB/day** | Sleep is the largest (107 KB raw). Several endpoints duplicate each other (`get_stats` = `get_user_summary`, `get_all_day_stress` = `get_stress_data`), and these get deduplicated in the registry |
| Per activity: JSON (19 endpoints) | ~650 KB | **~90 KB** | `get_activity_details` (per-sample metric charts) is ~95% of it |
| Per activity: FIT (original zip) | — | **~50 KB** (7–110 KB) | TCX/GPX downloads are skipped since they duplicate the FIT |
| Snapshot endpoints (76 with data) | 680 KB | **82 KB** total | Latest version only, so this doesn't grow |

### 13.3 Projection

| | Full backfill (20 months) | Growth per year |
|---|---|---|
| Storage: raw JSON gz (daily) | ~25 MB | ~15 MB |
| Storage: activities JSON gz + FIT | ~25 MB | ~15 MB |
| **Storage total** | **~50 MB of 1 GB (5%)** | ~30 MB |
| DB: intraday arrays (~2,500 samples/day × 8 B) | ~8–12 MB | ~5–7 MB |
| DB: daily, sleep, activity, and other typed rows | ~3–5 MB | ~2–3 MB |
| **DB total** | **~10–20 MB of 500 MB (2–4%)** | ~7–10 MB |

That's about 5× below the requirements estimate. **The free tier covers decades of
data at this activity level, so cold storage (D-9) is unlikely to be needed.**

### 13.4 Token observations

- The access token (JWT) lifetime is **~21.5 h**, so the cloud worker refreshes about
  once a day.
- The refresh token is opaque, so its lifetime is unknown. Whether it rotates on
  refresh gets observed in M0. The single-flight broker (§7.2) is designed for the
  worst case.

## 14. Milestones

Each milestone ends with something running, and with this doc updated where reality
differed.

| # | Milestone | Scope | Exit criteria |
|---|---|---|---|
| **M0** | **Spike: de-risk the cloud** | Install git + gh; create private repo; Supabase + Vercel projects; minimal FastAPI on Vercel that loads a cloud token and makes ~50 Garmin calls; `pg_cron` → Vercel tick; Claude custom connector (secret URL) from a chat **and** a cloud scheduled task | Go/no-go on Vercel ↔ Garmin. Token lifetime/rotation observed. Connector reachable from scheduled tasks (or fallback chosen) |
| **M1** | **Core sync: daily wellness** | Schema v1, roles, RLS; token broker + `gds auth`; engine (lease, planner, runner, throttle); DAILY domains incl. intraday series; raw store; run log; basic Resend alert | `v_daily_wellness` matches the Notion log for the last 30 days, field by field. Runs every 3 h unattended |
| **M2** | **Activities + backfill** | ACTIVITY domains + FIT files; backfill planner; progress in `v_sync_health` | Full 20-month backfill complete; activity count matches Garmin Connect |
| **M3** | **Everything else** | RANGE + SNAPSHOT domains: weigh-ins, BP, hydration, perf metrics, PRs, gear, devices, workouts, calendar, badges, goals… | Every endpoint in the registry is synced or explicitly `empty` |
| **M4** | **Claude connector: reads** | OAuth; read tools; `query_sql`; update `fitness-project-instructions.md` + `weekly-review-task.md` | Weekly review produced via the connector with ≥ Notion-level detail (success criterion 3) |
| **M5** | **Claude connector: writes** | W-1..W-3, then W-4..W-8 where they work; workout builder; write log; ownership checks | Log weight + create & schedule a workout from chat; it appears on the watch and in Supabase (criterion 4) |
| **M5b** | **Strava titles & descriptions (W-9)** *(added 2026-10-19)* | Check Strava API terms; Strava app + `gds strava-auth`; `strava_tokens`; `update_strava_activity` with matching, preview, undo (§8.4) | From chat: a real activity's Strava title/description set from its planned workout after a preview, then restored with `undo_write` |
| **M6** | **Ops & publish-readiness** | Watchdog, backup tick, gitleaks, README setup guide, `gds doctor`, licence | Fresh-fork deploy following README only (criterion 6); forced-failure alert test (criterion 5) |
| **M7** | **Cut-over** *(timing per D-6)* | Switch Fitness project fully; disable local scheduled task + Notion sync; archive Notion DB | Criterion 7 |

## 15. Open technical questions: M0 results (2026-10-17)

**Verdict: go.** Vercel ↔ Garmin works, so the architecture stands as designed.

| # | Question | Result |
|---|---|---|
| 1 | Does Garmin accept DI-token requests from Vercel IPs? | **Yes.** From `dub1` (AWS eu-west-1): 51 requests → 50 ok + 1 empty, then 32/32 ok on a second run. Median 166 ms, no 429/403 at ~1 req/s |
| 2 | Do refresh tokens rotate? | **Yes.** A forced refresh returned a new refresh token, the next call worked, and the persisted token worked on a fresh run. **The single-flight broker (§7.2) is required.** Access-token lifetime is ~20–21.5 h |
| 3 | Do cloud scheduled tasks get custom connectors? | **Yes.** A Claude chat, Claude Code, and a **cloud scheduled task** all called `ping` (rows logged 12:11–12:18 UTC). Tested with the M0 secret-URL connector. **Re-test once OAuth lands (M4)** |
| 4 | Does `curl_cffi` load on Vercel? | **Yes** (0.16.3) |
| 5 | Does Vercel build from `pyproject.toml` + `uv.lock`? | **Yes.** Zero-config FastAPI with `[tool.vercel] entrypoint = "gds.app:app"`. The package lives at repo root (`gds/`), not `src/` |
| — | Does `pg_net` dropping the request stop the tick? | **No.** With a 5 s `pg_net` timeout, the tick kept running and finished 20 s later. The scheduler is fire-and-forget |
| — | Does `pg_cron` → Vercel work? | **Yes.** `gds_tick` runs every 10 min |

**Other findings**

- **Garmin login is rate-limited even from a home IP.** During `gds auth` the first two
  login strategies (mobile + curl_cffi, mobile + requests) returned **429**, and a later
  strategy succeeded. Re-auth may need retries. This confirms the rule that the cloud never
  does password logins.
- `mcp` resolved to **SDK 2.3** (`MCPServer`, not 1.x `FastMCP`). Its session manager can
  start only once per process, so tests share one lifespan.
- New Vercel projects default to Vercel Authentication on **all** `*.vercel.app` URLs.
  The project is set to `prod_deployment_urls_and_all_previews`, so the production alias
  is reachable and our own secrets protect the endpoints.
- `vercel link`/`deploy` append `.vercel` and `.env*` to `.gitignore`. That's harmless
  (`.env.example` is already tracked), so we leave it.
- Windows: PowerShell blocks `.ps1` scripts (including npm's `vercel.ps1`) by default.
  Call `vercel.cmd`.

## 16. M1 results (2026-10-17)

- **Parity with Notion: 100%.** Over the last 30 days, all 1,392 overlapping fields of
  `v_daily_wellness` match the Notion Wellness Log exactly (`scripts/parity_check.py`).
  The new store fills 18 values Notion had empty (mostly VO2max on days without a new
  estimate) and keeps full-resolution intraday data instead of truncated hourly strings.
- **Domains:** 16 daily domains. 12 are normalised (summary, sleep, HRV, readiness incl.
  every intraday reading + morning flag, training status + VO2max, stress, Body Battery,
  HR, respiration, steps, floors, SpO2, hydration). 4 are raw-only until M3
  (intensity minutes, RHR, Body Battery events, fitness age).
- **On Vercel:** one tick = ~150 units / ~210 requests in ~220 s, no 429s, so the
  37-day window (592 units) needs about 4 ticks.
- **Scheduling:** `gds_tick` every 10 min. Incremental passes (last 3 days) every 3 h;
  other ticks drain the backlog or exit in about a second.
- **Domain:** production is served at **https://gds.lilic.dev** (Vercel custom domain).
  The `pg_cron` tick URL (Vault `gds_tick_url`), the CLI (`GDS_BASE_URL` in `.env`), and
  the Claude connector all use it. The `*.vercel.app` alias still answers, with the same
  secret checks. The OAuth issuer (M4) will be this domain.
- **Alerting:** app alerts (re-auth needed, repeated 403s) and a daily in-database
  watchdog (`garmin.watchdog()`, 06:00 UTC via `pg_cron` + `pg_net` → Resend). Both
  record to `garmin.alerts` and email from **`alerts@mail.lilic.dev`** (Resend domain,
  EU region, verified, sending-only key restricted to that domain).
  `POST /api/alerts/test` and `select garmin.watchdog(true)` send test emails through each
  path; both were delivered on 2026-10-17. Resend's API sits behind Cloudflare and
  rejects urllib's default User-Agent (error 1010), so the app sends its own.

## 17. M2 results (2026-10-17, backfill in progress)

- **Activities: 171 indexed, matching Garmin's own `count_activities()` (171).** First
  activity 2025-02-09. `activity_index` has two units: `all` (full history, refreshed
  weekly to pick up edits) and `recent` (newest 30, every incremental pass). Activities
  that started in the last 2 days have their sub-resources re-fetched, since names, notes
  and RPE get edited after the fact.
- **11 per-activity domains:** summary detail (description, RPE, feel), laps, HR zones,
  power zones (only if the activity has power), exercise sets (strength/cardio only),
  weather (outdoor only), gear, the original FIT zip, plus raw-only details, typed
  splits and split summaries. Units that can't exist are never created
  (`units_for()`), which saves ~25% of the requests.
- **FIT files in Postgres** (`activity_files`, `bytea`, TOAST off): 171 files =
  9.8 MB (about 58 KB each), all downloaded on Vercel in one tick. Same reasoning as raw
  JSON (§4.5): small, transactional, no second secret.
- **Weather** comes from Garmin in °F / mph and is stored as given; `temp_c` and
  `apparent_temp_c` are generated columns.
- **Fixtures** are scrubbed of owner name/ids, GPS coordinates (fixed placeholders)
  and place names (`scripts/make_fixtures.py`), so the repo stays publishable.
- **Full backfill** from 2025-01-01: 10,480 daily + 1,633 activity units. It runs
  unattended at ~200 units per 10-minute tick (~10 h total); progress is in
  `v_sync_health.backfill_pct`.

## 18. M3 results (2026-10-17)

- **179 domains in total:** 16 daily, 53 monthly range, 96 snapshot, 2 per-entity,
  1 activity index, 11 per-activity. `garmin.v_domain_coverage` audits each one
  (`synced` / `empty` / `in_progress` / `failing`). At the end of M3: 81 synced,
  26 explicitly empty, 72 in progress (the history backfill), **0 failing**.
- **New typed data:** weigh-ins (grams → kg), blood pressure (mapped defensively, no
  readings yet; verify with the first write in M5), endurance/hill score, VO2max history
  (121 days), calendar items incl. naps and 2 months of planned workouts, race events,
  Body Battery events, and `performance_metrics` (race predictions, lactate threshold HR +
  raw speed, running FTP, fitness age). Snapshot lists (devices, gear + stats, PRs,
  goals, badges, workouts + full step definitions, plans, courses, profile, zones) are
  `entities`, **replaced per kind** so deletions in Garmin propagate.
- **Unit quirks found:** weights in grams; calendar activities in ms and cm while naps
  are in seconds; naps have no id (keyed by start time); weather in °F/mph; lactate
  threshold speed in an undocumented unit (stored raw).
- **Window limits:** five `get_daily_*_stats` endpoints reject a month, so they're fetched
  in 7-day chunks, and any range endpoint that returns 400 for a month falls back to
  chunks automatically.
- **Not synced (documented in code):** ECG and hourly intensity minutes (rejected for any
  input), daily fitness age range (bad parameter semantics; fitness age comes from the
  daily endpoint instead), `get_activities` (the index covers it), `get_sync_timestamp`
  (noise), `get_full_name` (duplicate of profile).
- **Planner:** unit creation now runs only when the day, backfill start or registry
  changes (`accounts.planned_signature`); ordinary ticks skip the ~13k-row check.
- **Renormalize from raw:** `gds renormalize <domain…>` rebuilds typed rows from stored
  payloads with zero Garmin calls; used to map Body Battery events and fitness age after
  the fact (FR-2 in practice).
- **Deploy race:** a tick on the previous build saw units for domains it didn't know and
  failed them. Unknown domains are now skipped and retried (version skew), never failed.

## 19. M4 results (2026-10-17)

- **Connector: `https://gds.lilic.dev/mcp`, OAuth 2.1.** The MCP SDK serves `/authorize`,
  `/token`, `/register` (dynamic client registration), `/revoke` and the RFC 8414 / RFC 9728
  metadata; `gds/oauth.py` stores clients, pending requests, codes and tokens in Postgres
  (SHA-256 hashes only). PKCE S256; single-use login requests and codes; opaque tokens, 1 h
  access / 90 d refresh rotated on every use (old pair revoked); revocation by token family.
- **Owner login:** `/oauth/login` shows which client is asking and accepts the owner password
  (scrypt hash in `OWNER_PASSWORD_HASH`, set with `gds owner-password`); 10 failures/hour lock
  it, and each failure costs 1 s.
- **Verified end to end:** `scripts/oauth_e2e.py` (19 checks, local against the real DB:
  registration → login → PKCE exchange → tool calls → refresh rotation → revocation, and the
  reader role cannot read `garmin_tokens`), then live from **Claude chat, a cloud scheduled
  task, and Claude Code**. This answers §15 question 3 for OAuth too.
- **16 tools:** `get_sync_status`, `get_daily_wellness`, `get_sleep`, `get_intraday`,
  `list_activities`, `get_activity`, `get_activity_streams` (FIT parsed with `fitdecode`;
  running cadence doubled to steps/min), `get_trend`, `get_body`, `get_performance`,
  `get_entities` (large kinds list ids/names; full documents per id, nulls dropped),
  `get_calendar`, `describe_schema`, `query_sql`, `request_resync`, `ping`. Read tools run as
  `gds_reader` in read-only transactions (5 s, 500 rows); `query_sql` additionally rejects
  non-SELECT text for clearer errors. Tool errors use the SDK's `ToolError`.
- **Legacy secret-path connector retired** (`LEGACY_SECRET_CONNECTOR=0`) and its secret
  rotated.
- **Fitness project prompts** for the connector: `docs/claude/fitness-project-instructions.md`
  and `docs/claude/weekly-review-task.md` (FR-15).
- SDK quirk: `/revoke` requires the `client_secret` form field to be present even for public
  clients.

## 20. M7 results, partial (2026-10-17)

- **Local path switched off (FR-16, criterion 7):** the Windows scheduled task
  `Garmin Wellness Collector` (every 4 h, ran `../garmin-connect-mcp/wellness-report.cmd`:
  `wellness_report.py` → Notion "Garmin Wellness Log") is unregistered. The Notion log no longer
  updates; Supabase is the only store. The task definition is backed up to
  `../garmin-connect-mcp/garmin-wellness-collector.task.xml` (`Register-ScheduledTask -Xml` to
  restore).
- **Still open:** archive the Notion database (not delete, per FR-16).

## 21. M5 results (2026-10-19)

- **12 write tools + `get_write_log`** on the connector (§8.3), all in `gds/writes.py`. Every
  write: build + validate a plan (DB reads only) → `dry_run=true` returns the preview (the
  **default**, so a bare call never writes; FR-17) → claim the idempotency key in
  `garmin.write_log` → Garmin call on a broker client through `Source.write` (404 is an error
  there, library `ValueError`s become `Invalid`) → log response, created entity and undo data →
  resync the affected units **inline**, then queue them again 2 min later in case Garmin's read
  side lags (FR-19). A failed write frees its key for a retry; a response without the expected
  id (`Unclear`) keeps the key reserved so a retry can't write twice. Annotations: additive
  tools `destructiveHint=false`, tools that change or remove `true`; all `idempotentHint=true`.
- **Ownership (FR-18):** reschedule / unschedule / delete_workout / undo only act on entities
  whose `write_log` row is `created_by_system` and not yet undone. Refusals were checked live
  against the athlete's own workouts and plan-created calendar entries.
- **`undo_write`:** deletes a weigh-in / BP reading / workout / schedule / manual activity the
  system created, subtracts logged water, or restores an activity's previous name, notes,
  type, RPE/feel (explicit nulls clear them) and gear.
- **Workout builder** (`gds/workout_builder.py`, `SPEC_DOC` is the tool description): steps
  warmup/run/recover/rest/cooldown/other ending on time, distance or lap button; targets pace
  (faster limit in `targetValueOne`, as Garmin's own workouts store it), HR zone/range, power
  zone/range, cadence, speed; repeats (nesting ≤ 2, `skip_last_rest`); strength exercises
  from the library's 1,527-entry catalog (case/hyphen-insensitive, suggestions on a miss),
  sets as repeat groups with rest, weight in grams. A real Garmin-made "4x4" workout is the
  test fixture; live uploads of a run and a strength workout came back step-for-step identical.
- **Times:** local date/time in, aware datetimes out, in Garmin's own timezone setting
  (`s_user_timezone`, an IANA name such as Europe/Paris; `accounts.home_timezone`, here "CET",
  is only a fallback). Manual activities send this name as their `timeZoneUnitDTO`.

**Live verification** (real account, labelled `GDS TEST`, all removed again; write ids 1–17):

| Use case | Result |
|---|---|
| W-1 weigh-in | ok (JSON endpoint, `MANUAL`); with body fat via FIT upload (`INDEX_SCALE`). `samplePk` found by a same-day lookup; both undone |
| W-2 / W-3 workouts | run + strength uploaded, scheduled, rescheduled (id kept). The scheduled run **appeared on the watch** after a sync (criterion 4), then was unscheduled and deleted via `undo_write`; the strength one via `delete_workout` |
| W-4 hydration | +250 ml then −250 ml; daily total back to empty |
| W-5 blood pressure | ok; **normaliser verified** on the real payload. Garmin sends `category` (AHA, e.g. ELEVATED) and `categoryName` (the account's ESH scale, e.g. NORMAL); we store `categoryName` |
| W-6 activity edit | name, notes, type, RPE (stored ×10), feel, add/remove gear in one call; undo restored all |
| W-7 manual activity | private `other` activity created, deleted by undo |
| W-8 gear | linking/unlinking on an activity verified; **create / retire / set defaults not run live** (Garmin has no gear delete, so a test pair would stay) |

**Sync fixes found while testing**
- `replace_missing` compared keys as text; a `timestamptz` key (blood pressure) prints
  differently in Python and Postgres, so a fresh BP row was deleted right after its upsert.
  Keys are now compared by type (`unnest` of typed arrays, types from the catalog). No real
  rows were affected (the table was empty).
- Weigh-in and BP months now carry a `between` replace scope, so readings deleted in Garmin
  (or undone) disappear here too. Months with no readings now count as `empty` again (empty
  lists don't count as typed rows).
- `Domain.on_empty`: an activity whose gear was all unlinked kept its old gear; an empty
  `activity_gear` payload now clears it (once, on the transition).
- Deleting a manual activity or a workout drops its rows/units/raw explicitly (the activity
  index and the workouts list don't see deletions of per-id children).

## 22. M5b results (2026-10-19)

- **`update_strava_activity`** (W-9, §8.4) is live when `STRAVA_CLIENT_ID` / `_SECRET` are set.
  Strava-only plans run without a Garmin session (`Plan.garmin = False`), so a Garmin re-auth
  doesn't block them. Only `{id, name, description}` of Strava's response is logged (the full
  response carries maps and segment efforts).
- **Auth:** `gds strava-auth` asks for the app credentials (secret hidden) if `.env` lacks them,
  opens the consent page with a one-shot `127.0.0.1` callback, refuses partial scopes, and stores
  the token encrypted (`strava_tokens`, its own AES-GCM associated data so a Garmin ciphertext
  can't stand in for it). Granted scopes: `read,activity:write,activity:read_all`.
- **Matching, as observed:** the Strava copy's `start_date` equals Garmin's UTC start to the
  second. Strava's `external_id` for Garmin uploads is `garmin_ping_<upload id>`, **not** the Garmin
  activity id, so ties prefer Garmin uploads, then an exact start; otherwise the tool refuses.
- **Activity → planned workout:** `get_activity` carries `metadataDTO.associatedWorkoutId` and
  `summaryDTO.directWorkoutComplianceScore`; now `activities.workout_id` / `workout_compliance`,
  and `v_activities.workout_name` (current library name; a workout edited over time keeps one id,
  e.g. "2x10" was once 6x3 and 4x5). Backfilled with `renormalize activity_summary` (172 rows).
  This account starts workouts from the library on the watch rather than scheduling them, so the
  calendar rarely has them; the workout id is the reliable link.
- **Live test** (write 20 → undo 21): Strava activity of the 2026-10-14 "2x10" run retitled with a
  description from its planned workout, read back from Strava, then restored to its previous title
  ("2x10 threshold") and empty description.

## 23. M6 results (2026-10-19)

- **`gds_worker` login (§4.1):** migration `m6_worker_role` creates a NOLOGIN role (BYPASSRLS; DML on
  all `garmin` tables + sequences, default privileges for later tables; `SET` to `gds_reader`; no Vault,
  no DDL). `gds setup` generates `WORKER_DATABASE_URL` (pooler user `gds_worker.<ref>`), sets the
  password as a **SCRAM-SHA-256 verifier computed locally** (tested against RFC 7677's example
  exchange), so no plaintext reaches Postgres or its logs, and pushes it to Vercel as `DATABASE_URL`.
  The admin URL stays on the owner's machine. Verified as the worker before switching: a 36-request
  resync tick, all 19 `oauth_e2e` checks (including the `gds_reader` drop). After the deploy, the
  only app sessions in `pg_stat_activity` are `gds_worker` via Supavisor, and pg_cron, manual and
  backup ticks all succeed.
- **`gds migrate`:** applies `supabase/migrations/*.sql` in order, one transaction each, recorded
  in Supabase's own ledger (`supabase_migrations.schema_migrations`). A migration counts as applied
  if its version **or name** is there, because the Supabase MCP records its own timestamps
  (all 16 on production matched by name).
- **`gds doctor`:** one command checks `.env`, the admin connection, pending migrations, Vault
  (`gds_tick_url` / `gds_tick_secret` compared server-side with `.env`, alert secrets present),
  both pg_cron jobs and a pg_cron tick in the last 30 min, the Garmin token, sync freshness
  (≤ 4 h ok, ≤ 24 h warn) and backfill, Strava, the worker login (and that it can't reach Vault),
  the app (`/api/health`, tick refuses no-secret requests, OAuth issuer = `GDS_BASE_URL`, a hint
  when Vercel Deployment Protection blocks production), and the GitHub backup secrets + last run.
  Values are compared, never printed. All green on production.
- **CI** (`.github/workflows/ci.yml`): ruff + pytest; gitleaks 8.30.1 over the full history
  (`.gitleaks.toml` allowlists Garmin `*I18nKey` / `badgeKey` fixture fields, the only two
  findings); all migrations from scratch on `supabase/postgres:17.11.0.003`, a second run as a
  no-op, then checks for the pg_cron jobs, the worker's BYPASSRLS, default privileges on a new
  table, no Vault access, and `garmin.watchdog()`. Green on the first run.
- **Backup tick** (`backup-tick.yml`, daily 04:17 UTC, skips without secrets): dispatched once,
  run 305 (`github_backup`) ok.
- **Fresh-fork gaps fixed:** `gds setup` now stores `gds_tick_url` in Vault (it was set by hand
  in M0); a new account had no `backfill_start`, so only the last 3 days would sync. `gds auth`
  now defaults it to two years back (`--since` to choose), and `gds backfill --since` changes it
  later. The legacy secret-path connector is **off unless** `LEGACY_SECRET_CONNECTOR=1`, and
  `gds connector-url` prints the OAuth URL. `.env.example` lists every setting.
- **README** is a step-by-step setup guide (Supabase → `migrate` → `owner-password` + `setup` →
  deploy → `GDS_BASE_URL` + setup + redeploy → `auth` → `call tick` + `doctor` → Claude connector →
  optional Strava) plus day-to-day operations. MIT licence was already in place.
- **Forced failure (criterion 5): passed.** A throwaway account `m6-forced-failure` got an expired
  token with an invalid refresh token. One local tick: Garmin answered the refresh with 400 →
  `AuthFailed` → account `needs_reauth` → app alert "needs_reauth" with step-by-step re-auth
  instructions; then `garmin.watchdog()` sent its own email from Postgres. Resend reports **both
  delivered** within seconds (13:50 UTC). Test account deleted; the two `alerts` rows are kept
  with `account_id` cleared.
- **Criterion 6 (fresh fork from the README alone): not yet run end to end.** Every step has run
  for real except on a clean account: migrations from zero in CI; `setup`, `doctor`, the backup
  tick and the worker login on production. A literal rehearsal needs a second Supabase project, a
  second Vercel project and a Garmin login, so it's left as a separate, owner-attended task.
- Also fixed: `scripts/oauth_e2e.py` still expected 16 tools; it now accepts the M5 write tools (30).

## 24. Sync now (2026-10-20)

- New MCP tool **`sync_now(days=1)`** and CLI **`gds sync-now [--days N]`** (§6.5). They pull
  fresh data on request instead of waiting for the 10-min tick or the 3-hourly incremental pass.
  The tool returns `queued_units`, `fetched_units`, `failed_units`, `still_queued` (top-priority
  units left over for the next tick) and the 3 newest stored activities, so Claude can tell
  whether the new one arrived. The server instructions tell Claude to call it when the user has
  just finished an activity.
- `run_tick(trigger, budget_s=None)`: the budget can be set per call (the lease TTL follows it).
- `store.ensure_units(..., boost=None)`: with a boost, new and still-pending units are raised to
  that priority. Checked against production in a rolled-back transaction: new → 200, pending →
  200, done without requeue → unchanged, done + requeue → pending at its own 9, and a backing-off
  pending unit without a boost keeps its attempts.
- Local run against production: 17 units queued (16 daily domains for today + the activity
  index), all 17 fetched, 0 left queued.

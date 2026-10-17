# Garmin Datastore — Requirements

| | |
|---|---|
| **Status** | v0.3 — open questions resolved, ready for design |
| **Date** | 2026-10-17 |
| **Owner** | kortemy@gmail.com |
| **Builds on** | `../garmin-connect-mcp` (local wellness collector + Notion bridge) |

---

## 1. Problem

Garmin recovery data reaches the Fitness project through a local Windows job
(`wellness_report.py` → `notion_sync.py` → Notion "Garmin Wellness Log"). It has
four limitations:

1. **It depends on my PC.** The job runs every 4h only while the PC is awake, so data
   lags or goes missing when the machine is off or asleep.
2. **It captures a small slice.** About 48 summarised fields per day, hourly timelines
   for only the last 2 days, no activity details, no history beyond what's been run.
3. **Notion is a poor datastore.** 2,000-char cell limits (timelines are truncated
   JSON strings), no real querying, no joins, no aggregation.
4. **It's one-way.** Claude can read recovery data but can't act on it. I can't say
   "I weighed 106.4 kg this morning" or "put Thursday's tempo run on my watch".

## 2. Goal

A **cloud-hosted, always-on service** that:

- copies **all** of my Garmin Connect data, including full history, into a **Supabase**
  database I own;
- lets **Claude write back to Garmin** on my instruction (weight, workouts, and so on);
- runs on **Vercel + Supabase (+ GitHub)** free tiers, with no dependence on my local machine;
- is built as a **reusable, publishable codebase**: other technical users can deploy
  their own copy, and the design doesn't block turning it into a multi-user product later.

Supabase becomes the single source of Garmin data for the Fitness project and for
anything I build later.

## 3. Users & consumers

- **Me**: the operator and, for now, the only Garmin account.
- **Claude (Fitness project)**: the weekly scheduled review and on-demand chats
  **read** the data (replacing Notion) and **write** to Garmin when I ask.
- **Other technical users (later)**: fork the repo and deploy their own instance
  against their own Garmin account, Supabase project, and Vercel project.
- **Future product (not now)**: a hosted multi-user version. v1 only has to *not
  prevent* this (see §4.7).

---

## 4. Functional requirements

### 4.1 Data coverage — "everything"

**FR-1. Sync every data domain Garmin Connect exposes for the account**, at full
available resolution. At minimum:

| Domain | Examples |
|---|---|
| **Activities** | Activity list & summaries, full details/metrics charts, laps, splits, HR/power time-in-zone, weather, strength exercise sets, gear used |
| **Activity files** | Original **FIT** file for every activity |
| **Daily summary** | Steps, distance, calories, floors, intensity minutes |
| **Sleep** | Sleep score, stages, timings, sleep-level timeline, respiration, SpO2, restless moments |
| **Heart rate** | Resting HR, intraday HR (full resolution) |
| **HRV** | Nightly HRV, status, weekly average, baseline, readings |
| **Stress & Body Battery** | Intraday stress and Body Battery (full resolution), Body Battery events |
| **Readiness & training** | Training readiness (all intraday readings + factors), training status, training load / ACWR, load balance, VO2max, race predictions, endurance score, hill score, fitness age, lactate threshold |
| **Other wellness** | Respiration, Pulse Ox / SpO2, hydration, blood pressure |
| **Body** | Weigh-ins, body composition |
| **Records & goals** | Personal records, goals, badges, challenges |
| **Plans & workouts** | Saved workouts, training plans, calendar/scheduled workouts |
| **Profile & equipment** | User profile & settings, devices, gear + gear stats |
| **Women's health** | Included only if the account has data (otherwise skipped silently) |

**FR-2. Preserve raw responses.** Keep the raw JSON of every Garmin response as
received, so nothing is lost even if a field isn't mapped to a column yet, and tables
can be rebuilt later without re-calling Garmin.

**FR-3. Provide queryable, typed tables** for the commonly used data: one row per
day for daily wellness, one row per activity, plus intraday time series. At minimum,
**field parity with the current Notion Wellness Log** (all ~48 columns) plus activities.

**FR-4. Store activity files** (original FIT) in Supabase Storage, linked to their
activity row.

### 4.2 Sync behaviour

**FR-5. Full-history backfill.** One-time import of everything back to the start of
the Garmin account. It must be:
- **chunked and resumable**: progress is saved, and each run continues where the last stopped;
- **polite to Garmin**: throttled, backs off on rate limits, never hammers the API;
- **observable**: I can see how far it has got (e.g. "2019-03 of 2014→2026, 41%").

**FR-6. Incremental sync every ~3–4 hours**, without my PC and without a paid plan
(see Decision D-1 for the trigger).

**FR-7. Re-fetch a recent window on each run** (e.g. the last few days), because Garmin
revises recent data after the fact (sleep is scored later, readiness updates through
the day, late watch syncs).

**FR-8. Idempotent writes to the database.** Re-running any sync, for any date range,
updates in place and never duplicates.

**FR-9. Manual triggers.** I can (a) run a sync now and (b) re-sync a specific date
range or data domain on demand.

### 4.3 Authentication

**FR-10. One-time local login, then hands-off.** I authenticate locally once (the
account has no MFA, so email + password, as today with `authenticate.cmd`) and upload
the resulting token. The worker then uses and **refreshes the token on its own** and
persists the refreshed token.

**FR-11. Clear re-auth signal.** If the token becomes invalid, sync stops cleanly,
records why, and notifies me with the exact steps to re-authenticate. It never retries
in a loop that could lock the account.

### 4.4 Monitoring

**FR-12. Run log.** Every sync run records start/end time, status, what was
fetched/updated per domain, and errors.

**FR-13. Failure alerting.** I'm notified **by email** (the simplest free option; see
D-3) if a run fails, if re-auth is needed, or if no successful sync has happened for
more than ~24h. The alert channel is pluggable so other deployers can swap it.

### 4.5 Claude access & migration

**FR-14. A single Claude connector.** The Fitness project reaches the system through
one hosted MCP connector that provides both **read tools** (wellness, activities,
trends, plus flexible read-only querying) and **write tools** (§4.6). It works from
cloud scheduled tasks as well as interactive chats. *(Why one connector: writes need
custom tools anyway, and curated read tools are easier for Claude to use than raw
tables.)*

**FR-15. Update the Fitness project assets**, `fitness-project-instructions.md` and
`weekly-review-task.md`, to use the new connector, including a short description of
the available data.

**FR-16. Retire the old path** once Supabase is proven: the Notion sync, the
`Garmin Wellness Collector` scheduled task, and the Notion database (archived, not
deleted). Timing to be decided at the end of the project.

### 4.6 Writing to Garmin

Use cases, in priority order:

| # | Use case | Example request to Claude |
|---|---|---|
| **W-1** | **Log weight / body composition** | "I weighed 106.4 kg this morning, 22% body fat" |
| **W-2** | **Create structured workouts** (runs with warm-up / intervals / cool-down and pace or HR targets; strength workouts with exercises, sets, reps) | "Build a 6×800 m interval session at 4:30/km with 2 min jog recoveries" |
| **W-3** | **Schedule workouts on the Garmin calendar** so they sync to the watch, and **reschedule/remove** ones this system created | "Put next week's plan on my watch" / "Move Thursday's tempo to Friday" |
| W-4 | Log hydration | "I've had 2.5 L today" |
| W-5 | Log blood pressure | "BP 128/82, pulse 61" |
| W-6 | Annotate activities: name, notes/description, RPE, correct activity type, assign gear | "Rename yesterday's session to 'Basketball — pickup' and note my left knee was sore" |
| W-7 | Create manual activities for sessions done without the watch | "Log a 60-min basketball session last Tuesday" |
| W-8 | Manage gear: add new shoes, retire old ones, set defaults per activity type | "Add new Pegasus 41, retire the old pair" |

W-1 to W-3 are **must-have** for v1. W-4 to W-8 are **should-have**, included where
the Garmin API supports them reliably (to be confirmed in the design).

**FR-17. Writes are explicit and confirmed.** Claude only writes when I ask, and
states exactly what it will write before doing it (the connector's tool descriptions
require this).

**FR-18. No destructive writes to my data.** The system never deletes activities,
health records, or anything it didn't create. It *may* modify/remove workouts and
calendar entries **it created itself** (W-3).

**FR-19. Writes are audited and reflected.** Every write is logged (what, when, request,
Garmin's response), and the affected data is re-synced into Supabase right after, so
the database matches Garmin.

### 4.7 Reusability & future-proofing

**FR-20. Deploy-your-own.** A technical user can fork the repo, create their own free
Supabase + Vercel projects, follow the README, and have a working instance against
their own Garmin account. Setup is scripted where possible.

**FR-21. No personal values in code.** Everything account-, user-, or deployment-specific
(credentials, timezone, units, alert email, schedule) comes from config/env, with a
committed `.env.example`.

**FR-22. Account-scoped data model.** Every table is keyed by an account/user id even
though v1 has one account, so multi-user is a feature addition, not a rewrite.

**FR-23. Swappable data source.** Garmin access sits behind an internal interface
(sync engine ↔ "source adapter"), so the unofficial-API adapter can later be replaced
or joined by Garmin's **official** API. A real multi-user product would need that,
since the unofficial API isn't licensed for third-party use. Same principle for storage
and alerting where it's cheap.

**FR-24. Publish-ready repo.** Private for now, but kept publishable at any time: no
secrets in git history, a licence file, README, tests, and CI.

---

## 5. Non-functional requirements

**NFR-1. Controlled writes to Garmin.** Sync is read-only. Writes happen only through
the explicit write tools in §4.6 and follow FR-17 to FR-19.

**NFR-2. Security.**
- No Garmin password stored anywhere, only the OAuth token, and only server-side
  (encrypted at rest).
- Supabase service key lives only in Vercel env vars, never in the repo or client code.
- Row-level security on. Read tools use a **read-only** database role.
- All endpoints are authenticated: sync triggers by shared secret, and the Claude
  connector by proper auth. **No unauthenticated write path, ever.**
- No secrets committed to git (the current `notion.env` pattern isn't repeated).

**NFR-3. Reliability.** A failed or partial run never corrupts or loses data, and the
next run catches up automatically. Freshness target: data **≤ 4h** behind Garmin
under normal conditions.

**NFR-4. Resilient to Garmin changes.** Unknown or new fields don't break the sync
(they land in raw JSON). An endpoint failing doesn't stop the other domains (same
"never hard-fail on one metric" principle as today's collector).

**NFR-5. Correct time handling.** Timestamps are stored in UTC with the local offset
preserved. Daily data is keyed by the **Garmin calendar date** (local), matching what
the Garmin app shows. Home timezone is CET/CEST, but **travel must be handled**: the
offset is taken from each day's or activity's own Garmin data, never assumed from a
fixed timezone. Units follow the Garmin profile (metric).

**NFR-6. Free to run.** The whole system runs on the **free tiers** of Vercel,
Supabase, and GitHub for a single account with full history (see §9 for volume).
Exceeding a free limit must be visible in advance, never a surprise failure.

**NFR-7. Maintainable by one person.** Small codebase, documented setup, schema
changes as versioned migrations, one-command local dev run, automated tests for the
mapping/normalisation logic.

---

## 6. Out of scope (non-goals)

- **Destructive writes to Garmin**: deleting activities or health records (FR-18).
- **Multi-user hosting in v1.** One account per deployment. Multi-user is *designed
  for* (§4.7) but not built.
- **Strava sync.** Strava stays as the Fitness project's live connector.
- **A UI or dashboard.** The database and Claude are the interface for v1.
- **Real-time sync** (within minutes of a watch upload).
- **Coaching/analysis logic.** Claude does the reasoning; this project supplies data
  and actions.
- **Replacing the local `garmin-connect-mcp` server.** It keeps working for ad-hoc
  local use; this project doesn't touch it.

## 7. Constraints & assumptions

- **No official Garmin API for personal use.** We rely on the unofficial Garmin
  Connect web API via the Python `garminconnect` library (as today). It can change
  without notice, and its write endpoints are less battle-tested than its reads.
- **Python**, because `garminconnect` is the most complete client and today's code
  already uses it.
- **Vercel** (worker + connector), **Supabase** (database, storage, scheduler), and
  **GitHub** (repo, CI, backup scheduler), all on free tiers.
- **No MFA** on the Garmin account.

## 8. Success criteria

1. Full Garmin history is in Supabase: activity count matches Garmin Connect, and
   daily wellness has no gaps where Garmin has data.
2. **Two weeks of unattended sync with the PC off**, every run logged, data ≤ 4h
   behind, **$0 spent**.
3. The weekly Fitness review runs through the new connector with **at least the same
   information** it gets from Notion today, and the timelines are no longer truncated.
4. From a Claude chat I can **log my weight** and **create + schedule a structured
   workout** that then appears on my watch, and both show up in Supabase.
5. A forced failure (e.g. invalidated token) produces an email within 24h with clear
   re-auth instructions.
6. A fresh fork can be deployed by following the README alone.
7. The Notion sync and the local scheduled task are switched off.

## 9. Data volume (estimate)

Rough figures for **one year** of one active user (about 5–7 activities a week,
watch worn 24/7). These are ballpark numbers from typical Garmin payload sizes, to be
**measured in the design phase** by pulling a real week and extrapolating.

| Data | Raw (uncompressed) | Stored (compressed) |
|---|---|---|
| Daily wellness JSON, all endpoints (~150–300 KB/day) | 55–110 MB | 10–25 MB |
| Activity JSON: details, laps, splits (~300 activities) | 60–150 MB | 10–30 MB |
| FIT files (~300 activities) | 30–60 MB | 30–60 MB (FIT is already compact) |
| Typed tables (daily rows + intraday series stored per day) | — | 10–30 MB |
| **Total per year** | **~150–320 MB** | **~60–145 MB** |

**What this means for the free tier** (Supabase: 500 MB database, 1 GB file storage):

- Stored carefully (compressed raw JSON and FIT files in **Storage**, typed tables
  compact in the **database**), one year is roughly 20–50 MB of database and
  40–90 MB of storage. **Several years of history fit within the free tier.**
- Stored naively (one database row per heartbeat sample, uncompressed JSON in
  rows), a single year could approach 300 MB of database and blow the limit within
  2 years. The design has to avoid this.
- Per-second activity streams (GPS, HR, cadence) are the biggest optional item.
  They stay inside the FIT files rather than being expanded into database rows, and
  can be parsed on demand.

**For this account (~20 months of history, activities not continuous):** expect
roughly **60–150 MB of database + storage for the full backfill** at the stored
(compressed) sizes, so about 15–30% of the free tier, and less where months have few
activities. Growth is roughly 60–145 MB per year.

> **Measured (design doc §13):** real payloads are about 5× smaller than this estimate.
> The full 20-month backfill is **~50 MB of Storage (5%)** and **~10–20 MB of database
> (2–4%)**, growing ~40 MB a year in total.

**If volume becomes a problem later:** move data older than ~1 year to **cold
storage** (e.g. compressed archive files in Storage or another free bucket), keeping
summaries queryable in the database. Not built in v1. The design only has to make it
possible (raw data partitioned by date).

---

## 10. Decisions

| # | Topic | Decision |
|---|---|---|
| D-1 | **Scheduling** | Stay free. Use **Supabase `pg_cron` + `pg_net`** to call the Vercel worker every ~3h for incremental sync, and every few minutes while the backfill is running. Optional **GitHub Actions** schedule as a backup trigger. *(Vercel Hobby cron is once a day, so it isn't enough on its own.)* |
| D-2 | **Hosting cost** | Free tiers only (NFR-6). Re-evaluate only if measured volume says otherwise. |
| D-3 | **Alerts** | **Email.** The worker sends the alert, not Claude: Claude can't push notifications on its own schedule. Free option: Resend (free tier, can send to your own address without a custom domain). Pluggable, so phone push (e.g. ntfy) can be added later. As a soft backup, the weekly Claude review also reports if data looks stale. |
| D-4 | **MFA** | None on the account. |
| D-5 | **Repo** | New **private** GitHub repo, `garmin-datastore`, under my account. Publish-ready (FR-24). |
| D-6 | **Cut-over** | Decide at the end of the project. |
| D-7 | **Writes** | In scope: W-1 to W-3 must-have, W-4 to W-8 should-have. |
| D-8 | **Reusability** | Single-tenant deployments now; account-scoped schema + swappable source adapter for later. |
| D-9 | **History** | ~20 months, activities not continuous. Full backfill fits the free tier (§9). Cold storage for 1y+ data only if volume becomes a problem. |
| D-10 | **Licence** | MIT. |
| D-11 | **Units & timezone** | Metric, from the Garmin profile. Home timezone CET/CEST; per-day offsets from Garmin data to handle travel (NFR-5). |

## 11. Risks

| Risk | Impact | Mitigation (detail in design doc) |
|---|---|---|
| Garmin blocks/challenges logins or requests from cloud IPs | Sync and writes stop working from Vercel | Token-based auth only (no password login in the cloud); conservative request rates; **test this first** in a spike before building on it |
| Garmin rate-limits during backfill | Backfill takes longer | Throttling + backoff + resumable cursor; spread backfill over days if needed |
| Garmin changes/removes endpoints | Missing domains or broken writes | Raw JSON storage; per-domain isolation; write tools fail loudly with Garmin's error |
| Write endpoints unsupported/unstable in `garminconnect` | Some W-4 to W-8 use cases dropped | Confirm per use case in the design; must-haves (W-1 to W-3) verified in the spike |
| A wrong write (e.g. weight logged on the wrong date) | Bad data in Garmin | Confirm-before-write, audit log, writes limited to what the system created or appended |
| Data volume exceeds Supabase free tier | Failed writes or cost | Compact storage layout (§9); measure early; usage visible in run log |
| Vercel function time limits | Long jobs cut off | All work in small resumable units |
| Token expiry/invalidation | Sync stops | FR-10/11: auto-refresh + email alert |
| Claude custom connector auth requirements | Connector harder to set up for scheduled tasks | Verify in the design which auth works for cloud scheduled tasks |
| Supabase free projects pause after a week of inactivity | DB offline | Regular syncs keep it active; staleness alert catches it |

---

## 12. Open questions

None at the requirements level. Remaining unknowns, such as Garmin's behaviour from
cloud IPs, which write endpoints work, and connector auth for scheduled tasks, are
technical and belong to the design doc.

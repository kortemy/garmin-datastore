# garmin-datastore

Sync **all** of your Garmin Connect data into your own Supabase database, from a worker on
Vercel, and let Claude read it and write back to Garmin (log weight, create and schedule
workouts, annotate activities) through an MCP connector.

- Every 10 minutes Supabase `pg_cron` calls the worker. It syncs ~180 Garmin endpoints:
  daily wellness and sleep with full intraday series, activities with laps, zones and the
  original FIT files, body measurements, performance metrics, gear, workouts, calendar and more.
  The full history is backfilled automatically.
- Claude connects with OAuth to `https://<your-deployment>/mcp`: read tools, read-only SQL, and
  confirmed, logged, undoable write tools.
- A daily watchdog inside the database emails you if the sync stops or Garmin needs you to log
  in again. A daily GitHub Action is a backup trigger.
- Runs on the **free tiers** of Supabase, Vercel and GitHub.

Built on the unofficial Garmin Connect API via
[`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect). It's meant for
personal use with your own account, one account per deployment. Background:
[requirements](docs/requirements.md), [technical design](docs/design.md).

## Setup

About 30 minutes. The commands work the same on Windows, macOS and Linux; run them from the
repository folder. `gds` below is short for `uv run python -m gds.cli`.

### 1. What you need

- A Garmin Connect account **without two-factor authentication**.
- Free accounts at [Supabase](https://supabase.com), [Vercel](https://vercel.com) and GitHub.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (it installs Python 3.12 for you),
  [Node.js](https://nodejs.org) for the Vercel CLI (`npm install -g vercel`), and git.
  The [GitHub CLI](https://cli.github.com) (`gh auth login`) is optional; with it, setup also
  configures the backup trigger.
- Optional: a [Resend](https://resend.com) account for alert emails (free).

### 2. Fork and clone

Fork this repository on GitHub (a private fork is fine), then:

```
git clone https://github.com/<you>/garmin-datastore.git
cd garmin-datastore
uv sync
```

Forks start with GitHub Actions disabled: open your fork's **Actions** tab and enable workflows,
so CI and the backup trigger run.

### 3. Supabase project

1. Create a new project. Pick a region near you and save the database password.
2. Click **Connect** at the top of the project, choose **Transaction pooler**, and copy the URI
   (port 6543).
3. Copy `.env.example` to `.env` and put the URI into `DATABASE_URL`, with your database
   password in place of `[YOUR-PASSWORD]`. Special characters in the password must be
   URL-encoded (for example `@` → `%40`).

Leave everything else in `.env` as it is; the next steps fill it in.

### 4. Create the database schema

```
gds migrate
```

This applies `supabase/migrations/` in order: the `garmin` schema (not exposed through
Supabase's REST API, row-level security on every table), the `gds_worker` and `gds_reader`
roles, and the `pg_cron` jobs for the tick and the watchdog.

### 5. Connector password and first setup

```
gds owner-password
vercel login
gds setup
```

- `owner-password` asks (hidden) for the password you'll type when Claude connects. Only its
  hash is stored.
- `setup` generates the remaining secrets into `.env`, sets the app's own database login
  (`gds_worker`), stores the scheduler's secret in Supabase Vault, creates and links a Vercel
  project called `garmin-datastore` (`--project <name>` to change it, `--team <slug>` for a
  team), and pushes the settings to Vercel. Secrets are never printed.

For alert emails, first add `RESEND_API_KEY` (a sending-only key is enough) and `ALERT_EMAIL` to
`.env`. Without your own domain in Resend, `ALERT_EMAIL` must be your Resend account's email.

### 6. Deploy

```
vercel deploy --prod
```

Copy the production URL it prints (for example `https://garmin-datastore-you.vercel.app`, or a
custom domain you add in Vercel) into `.env` as `GDS_BASE_URL`, then run setup again and redeploy,
so the scheduler, the OAuth issuer and the backup trigger all use it:

```
gds setup
vercel deploy --prod
```

In Vercel → project → **Settings → Deployment Protection**, Vercel Authentication must not cover
production: **Standard Protection** is right; **All Deployments** blocks the scheduler.

### 7. Log in to Garmin once

```
gds auth
```

Your Garmin email and password are used once, on your machine, to get a token. The token is
stored encrypted in the database and refreshed automatically by the worker; your password is
never stored, and the cloud never logs in with it. Garmin sometimes answers the first login
attempts with "too many requests"; the library retries with other methods. If it still fails,
wait a few minutes and run it again.

History is synced from two years back by default. For more (or less), add `--since 2021-01-01`,
or change it any time later with `gds backfill --since 2021-01-01`.

### 8. Check it

```
gds call tick
gds doctor
```

`call tick` runs one sync on Vercel and prints what it did. `doctor` checks every part of the
deployment and says how to fix anything that's wrong. Within a few minutes the scheduler takes
over and backfills the history in the background at about 200 work units per 10-minute tick
(roughly 10 hours for two years). `doctor`'s **sync** line shows the progress.

### 9. Connect Claude

In Claude: **Settings → Connectors → Add custom connector**, URL `https://<your-deployment>/mcp`
(`gds connector-url` prints it). Claude opens a sign-in page; enter your owner password from
step 5. The connector works in chats, projects and scheduled tasks.

Write tools (weight, workouts, scheduling, activity edits, gear) always show a preview
first and only write after you confirm; `undo_write` reverts what the system created.

### 10. Optional: Strava titles and descriptions

To let Claude title and describe activities on Strava: create an API application at
[strava.com/settings/api](https://www.strava.com/settings/api) (authorization callback domain:
your deployment's domain), then run `gds strava-auth`, `gds setup`, and deploy.

## Running it

| When | What to do |
|---|---|
| Alert email "needs re-authentication" | `gds auth`. The next tick picks up the new token. |
| Something looks off | `gds doctor`; the Claude connector's `get_sync_status` shows the same health |
| Re-fetch some data | `gds call sync --body '{"domain": "sleep", "start": "2026-01-01", "end": "2026-01-31"}'`, or ask Claude (`request_resync`) |
| Test the alert emails | `gds call alerts/test` (from the app) and, in the Supabase SQL editor, `select garmin.watchdog(true);` (from the database) |
| Update to a newer version | `git pull`, `gds migrate`, `vercel deploy --prod`, `gds doctor` |
| Changed `.env` | `gds setup`, then `vercel deploy --prod` |

The tick and watchdog jobs run inside Supabase (`pg_cron`). Free Supabase projects can be paused
after a week of inactivity; the worker's database traffic every 10 minutes should prevent that,
and if it ever happens, the daily backup trigger fails and GitHub emails you about it.

**Costs.** One account with ~2 years of history uses about 5% of Supabase's free storage and a
few percent of its database quota; Vercel and GitHub usage stays well inside their free tiers.

## Development

```
uv run --python 3.12 pytest -q
uv run --python 3.12 ruff check gds tests scripts
gds tick          # one sync tick on your machine, against the real database
```

Tests use recorded, scrubbed Garmin responses (`tests/fixtures/`) and need no network or
database. CI runs them, a secret scan over the whole git history (gitleaks), and all migrations
against a fresh Supabase Postgres. New migrations go in `supabase/migrations/` as
`<UTC timestamp>_<name>.sql` and are applied with `gds migrate`.

## License

[MIT](LICENSE)

"""Operations helpers for the CLI (M6): migrations, the worker login, and `gds doctor`.

Nothing here prints or returns a secret. Checks compare secrets server-side or by presence only.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
USER_AGENT = "garmin-datastore-cli/0.1"

# ---------------------------------------------------------------- migrations


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    out = []
    for p in sorted(directory.glob("*.sql")):
        m = re.fullmatch(r"(\d{14})_(\w+)\.sql", p.name)
        if not m:
            raise ValueError(f"Unexpected migration file name: {p.name} (want <14-digit timestamp>_<name>.sql)")
        out.append(Migration(m[1], m[2], p))
    return out


_LEDGER = """create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
    version text primary key, statements text[], name text)"""


def applied_migrations(conn: psycopg.Connection) -> set[str]:
    """Versions and names already recorded in Supabase's own ledger (`supabase db push` and the
    Supabase MCP write there too, under their own timestamps, so a name match counts as applied)."""
    if conn.execute("select to_regclass('supabase_migrations.schema_migrations') as t").fetchone()[0] is None:
        return set()
    rows = conn.execute("select version, name from supabase_migrations.schema_migrations").fetchall()
    return {v for row in rows for v in row if v}


def pending_migrations(conn: psycopg.Connection, files: list[Migration] | None = None) -> list[Migration]:
    done = applied_migrations(conn)
    return [m for m in (files or migration_files()) if m.version not in done and m.name not in done]


def apply_migration(conn: psycopg.Connection, m: Migration) -> None:
    """Run one migration file and record it, in a single transaction."""
    sql = m.path.read_text(encoding="utf-8")
    with conn.transaction():
        conn.execute(_LEDGER)
        conn.execute(sql)  # no parameters: psycopg sends multi-statement text as is
        conn.execute(
            "insert into supabase_migrations.schema_migrations (version, name, statements) values (%s, %s, %s)",
            (m.version, m.name, [sql]),
        )


# ---------------------------------------------------------------- worker login


def scram_verifier(password: str, iterations: int = 4096, salt: bytes | None = None) -> str:
    """Postgres SCRAM-SHA-256 verifier, so `alter role ... password` never carries the plaintext."""
    salt = salt or secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    stored_key = hashlib.sha256(hmac.new(salted, b"Client Key", "sha256").digest()).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()

    def b64(b: bytes) -> str:
        return base64.b64encode(b).decode()

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def worker_url(admin_url: str, password: str, role: str = "gds_worker") -> str:
    """The admin connection string with the user swapped for the worker role. Supabase's pooler
    names users `<role>.<project-ref>`; a direct connection uses the bare role."""
    u = urllib.parse.urlsplit(admin_url)
    user = urllib.parse.unquote(u.username or "postgres")
    _, dot, ref = user.partition(".")
    host = u.hostname or ""
    if u.port:
        host += f":{u.port}"
    netloc = f"{urllib.parse.quote(role + dot + ref, safe='')}:{urllib.parse.quote(password, safe='')}@{host}"
    return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def url_password(url: str) -> str:
    return urllib.parse.unquote(urllib.parse.urlsplit(url).password or "")


# ---------------------------------------------------------------- doctor

OK, WARN, FAIL, INFO = "ok", "warn", "FAIL", "info"


@dataclass
class Check:
    level: str
    name: str
    detail: str


def _http(method: str, url: str, body: bytes | None = None, timeout: float = 20) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, prepare_threshold=None, connect_timeout=10, autocommit=True)


def check_env(env: dict[str, str]) -> Iterator[Check]:
    required = ["DATABASE_URL", "TOKEN_ENC_KEY", "TICK_SECRET", "MCP_PATH_SECRET", "GDS_BASE_URL"]
    missing = [k for k in required if not env.get(k) or "<" in env[k] or "YOUR-PASSWORD" in env[k]]
    if missing:
        yield Check(FAIL, ".env", f"missing or placeholder: {', '.join(missing)} (see README, step 3-5)")
    else:
        yield Check(OK, ".env", "required settings present")
    if not env.get("WORKER_DATABASE_URL"):
        yield Check(WARN, "worker login",
                    "WORKER_DATABASE_URL not in .env: run `gds setup` (Vercel would use the admin login)")
    if not env.get("OWNER_PASSWORD_HASH"):
        yield Check(WARN, "connector password",
                    "OWNER_PASSWORD_HASH not set: run `gds owner-password`, then `gds setup`")
    if not (env.get("RESEND_API_KEY") and env.get("ALERT_EMAIL")):
        yield Check(WARN, "alerts", "RESEND_API_KEY / ALERT_EMAIL not set: failures are recorded but not emailed")


def check_database(env: dict[str, str]) -> Iterator[Check]:
    try:
        conn = _connect(env["DATABASE_URL"])
    except (psycopg.Error, KeyError) as e:
        yield Check(FAIL, "database", f"cannot connect with DATABASE_URL: {str(e).strip().splitlines()[-1][:200]}")
        return
    with conn:
        yield Check(OK, "database", conn.execute("select current_setting('server_version')").fetchone()[0])

        pending = pending_migrations(conn)
        if pending:
            yield Check(FAIL, "migrations", f"{len(pending)} pending ({pending[0].name}…): run `gds migrate`")
            return
        yield Check(OK, "migrations", f"all {len(migration_files())} applied")

        yield from _check_vault(conn, env)
        yield from _check_cron(conn)
        yield from _check_account(conn, env)


def _check_vault(conn: psycopg.Connection, env: dict[str, str]) -> Iterator[Check]:
    def secret_matches(name: str, expected: str) -> bool | None:
        row = conn.execute("select decrypted_secret = %s from vault.decrypted_secrets where name = %s",
                           (expected, name)).fetchone()
        return None if row is None else bool(row[0])

    base = env.get("GDS_BASE_URL", "").rstrip("/")
    for name, expected, fix in [
        ("gds_tick_url", f"{base}/api/tick", "GDS_BASE_URL changed or setup not run"),
        ("gds_tick_secret", env.get("TICK_SECRET", ""), "TICK_SECRET changed or setup not run"),
    ]:
        match = secret_matches(name, expected)
        if match is None:
            yield Check(FAIL, f"vault {name}", "missing: run `gds setup`")
        elif not match:
            yield Check(FAIL, f"vault {name}", f"differs from .env ({fix}): run `gds setup`")
        else:
            yield Check(OK, f"vault {name}", "matches .env")
    have = {r[0] for r in conn.execute(
        "select name from vault.secrets where name in ('gds_resend_api_key', 'gds_alert_email')")}
    if len(have) < 2:
        yield Check(WARN, "vault alerts", "watchdog email not configured (RESEND_API_KEY / ALERT_EMAIL, then setup)")
    else:
        yield Check(OK, "vault alerts", "watchdog can email")


def _check_cron(conn: psycopg.Connection) -> Iterator[Check]:
    jobs = {r[0]: r[1] for r in conn.execute(
        "select jobname, active from cron.job where jobname in ('gds_tick', 'gds_watchdog')")}
    for name in ("gds_tick", "gds_watchdog"):
        if name not in jobs:
            yield Check(FAIL, f"pg_cron {name}", "job missing: re-run the migrations")
        elif not jobs[name]:
            yield Check(FAIL, f"pg_cron {name}", "job is inactive")
        else:
            yield Check(OK, f"pg_cron {name}", "scheduled")
    row = conn.execute(
        "select max(started_at) > now() - interval '30 minutes' from garmin.sync_runs where trigger = 'pg_cron'"
    ).fetchone()
    if row[0]:
        yield Check(OK, "scheduled ticks", "a pg_cron tick reached the app in the last 30 min")
    else:
        yield Check(FAIL, "scheduled ticks",
                    "no pg_cron tick reached the app in 30 min: check the deployment, GDS_BASE_URL and `gds setup`")


def _check_account(conn: psycopg.Connection, env: dict[str, str]) -> Iterator[Check]:
    label = env.get("GDS_ACCOUNT_LABEL") or "default"
    acct = conn.execute(
        """select a.id, a.status, a.status_reason, t.access_expires_at, t.updated_at as token_updated
           from garmin.accounts a left join garmin.garmin_tokens t on t.account_id = a.id where a.label = %s""",
        (label,),
    ).fetchone()
    if acct is None:
        yield Check(FAIL, "garmin account", f"no account '{label}': run `gds auth`")
        return
    acct_id, status, reason, expires, token_updated = acct
    if expires is None and token_updated is None:
        yield Check(FAIL, "garmin token", "none stored: run `gds auth`")
    elif status == "needs_reauth":
        yield Check(FAIL, "garmin token", f"rejected by Garmin ({reason}): run `gds auth`")
    else:
        yield Check(OK, "garmin token", f"account '{label}' {status}; refreshed {token_updated:%Y-%m-%d %H:%M} UTC")

    h = conn.execute(
        """select last_success_at, now() - last_success_at as age, units_failed, units_pending, backfill_pct
           from garmin.v_sync_health where account_id = %s""", (acct_id,)).fetchone()
    if h is not None:
        last, age, failed, pending, pct = h
        if last is None:
            yield Check(WARN, "sync", "no successful tick yet (the first few ticks plan and start the backfill)")
        else:
            hours = age.total_seconds() / 3600
            level = OK if hours <= 4 else WARN if hours <= 24 else FAIL
            yield Check(level, "sync", f"last success {hours:.1f} h ago; backfill {pct or 0}%, "
                                       f"{pending} pending, {failed} failed")
    alerts = conn.execute(
        "select count(*), max(created_at) from garmin.alerts where created_at > now() - interval '7 days'").fetchone()
    if alerts[0]:
        yield Check(INFO, "alerts", f"{alerts[0]} in the last 7 days, latest {alerts[1]:%Y-%m-%d %H:%M} UTC")

    if env.get("STRAVA_CLIENT_ID"):
        row = conn.execute("select status from garmin.strava_tokens where account_id = %s", (acct_id,)).fetchone()
        if row is None:
            yield Check(WARN, "strava", "STRAVA_CLIENT_ID set but not authorized: run `gds strava-auth`")
        elif row[0] != "active":
            yield Check(WARN, "strava", f"token {row[0]}: run `gds strava-auth`")
        else:
            yield Check(OK, "strava", "authorized")


def check_worker(env: dict[str, str]) -> Iterator[Check]:
    url = env.get("WORKER_DATABASE_URL")
    if not url:
        return
    try:
        with _connect(url) as conn:
            user = conn.execute("select current_user").fetchone()[0]
            conn.execute("select 1 from garmin.accounts limit 1")
            vault = conn.execute("select has_schema_privilege('vault', 'usage')").fetchone()[0]
    except psycopg.Error as e:
        yield Check(FAIL, "worker login", f"cannot connect as the worker: {str(e).strip().splitlines()[-1][:200]}")
        return
    if vault:
        yield Check(WARN, "worker login", f"{user} can use the vault schema; it shouldn't")
    else:
        yield Check(OK, "worker login", f"connects as {user}, reads garmin.*, no Vault access")


def check_app(env: dict[str, str]) -> Iterator[Check]:
    base = env.get("GDS_BASE_URL", "").rstrip("/")
    if not base or "<" in base:
        return
    try:
        code, body = _http("GET", f"{base}/api/health")
    except (urllib.error.URLError, TimeoutError) as e:
        yield Check(FAIL, "app", f"{base} unreachable: {e}")
        return
    if code in (401, 403):
        yield Check(FAIL, "app", f"GET /api/health returned {code}: Vercel Deployment Protection covers production; "
                                 "set it to Standard Protection (README step 6)")
        return
    if code != 200:
        yield Check(FAIL, "app", f"GET /api/health returned {code}: deploy first (README step 6)")
        return
    yield Check(OK, "app", f"{base} is up")

    code, _ = _http("POST", f"{base}/api/tick", b"{}")
    yield (Check(OK, "app auth", "tick endpoint refuses requests without the secret") if code == 401
           else Check(FAIL, "app auth", f"tick endpoint without the secret returned {code}, expected 401"))

    code, body = _http("GET", f"{base}/.well-known/oauth-authorization-server")
    if code != 200:
        yield Check(WARN, "connector", "OAuth metadata not served: set GDS_BASE_URL + OWNER_PASSWORD_HASH, "
                                       "run `gds setup`, redeploy")
    else:
        issuer = json.loads(body).get("issuer", "").rstrip("/")
        yield (Check(OK, "connector", f"OAuth ready; add {base}/mcp as a Claude connector") if issuer == base
               else Check(WARN, "connector", f"OAuth issuer is {issuer}, not {base}: fix GDS_BASE_URL on Vercel"))


def _gh(*args: str) -> subprocess.CompletedProcess | None:
    exe = shutil.which("gh")
    if not exe:
        return None
    return subprocess.run([exe, *args], cwd=ROOT, capture_output=True, text=True, timeout=30)


def check_github() -> Iterator[Check]:
    res = _gh("secret", "list", "--json", "name")
    if res is None:
        yield Check(INFO, "github backup", "gh CLI not found; skipping the backup-tick checks")
        return
    if res.returncode != 0:
        yield Check(INFO, "github backup", f"can't list repo secrets ({res.stderr.strip()[:120]})")
        return
    names = {s["name"] for s in json.loads(res.stdout or "[]")}
    missing = {"GDS_BASE_URL", "TICK_SECRET"} - names
    if missing:
        yield Check(WARN, "github backup", f"repo secrets missing: {', '.join(sorted(missing))}: run `gds setup`")
        return
    runs = _gh("run", "list", "--workflow", "backup-tick.yml", "--limit", "1", "--json", "conclusion,createdAt")
    last = json.loads(runs.stdout or "[]") if runs and runs.returncode == 0 else []
    if not last:
        yield Check(OK, "github backup", "secrets set; no run yet (daily, or run it from the Actions tab)")
    elif last[0]["conclusion"] in ("success", ""):
        state = last[0]["conclusion"] or "running"
        yield Check(OK, "github backup", f"secrets set; last run {last[0]['createdAt']} {state}")
    else:
        yield Check(WARN, "github backup", f"last run {last[0]['createdAt']} ended {last[0]['conclusion']}")


def run_doctor(env: dict[str, str], emit: Callable[[Check], None]) -> bool:
    """Run every check, emitting each as it completes. True when nothing failed."""
    ok = True
    for group in (check_env(env), check_database(env), check_worker(env), check_app(env), check_github()):
        for c in group:
            ok &= c.level != FAIL
            emit(c)
    return ok

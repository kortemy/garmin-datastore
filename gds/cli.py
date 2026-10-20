"""`gds` command line, run on your own machine.

    uv run python -m gds.cli migrate  # apply supabase/migrations to the database
    uv run python -m gds.cli setup    # secrets: .env, worker login, Vercel env, Supabase Vault, GitHub
    uv run python -m gds.cli auth     # one-time Garmin login, stores an encrypted token
    uv run python -m gds.cli doctor   # check every part of the deployment
    uv run python -m gds.cli strava-auth  # optional: one-time Strava authorization (W-9)
    uv run python -m gds.cli tick             # one sync tick on this machine
    uv run python -m gds.cli sync-now         # fetch the newest activities + today's data right away
    uv run python -m gds.cli call tick        # POST to the deployed app (GDS_BASE_URL)
    uv run python -m gds.cli connector-url    # Claude connector URL (OAuth)

Secrets are typed into hidden prompts or generated locally. They are never printed.
"""

import argparse
import datetime as dt
import getpass
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
VERCEL_VARS = ["DATABASE_URL", "TOKEN_ENC_KEY", "TICK_SECRET", "MCP_PATH_SECRET"]
# Optional alerting settings: pushed to Vercel and mirrored into Vault for the DB watchdog.
OPTIONAL_VARS = {
    "RESEND_API_KEY": "gds_resend_api_key",
    "ALERT_EMAIL": "gds_alert_email",
    "ALERT_FROM": "gds_alert_from",
}
# Optional, Vercel only: connector OAuth (owner password hash, public base URL).
VERCEL_ONLY_VARS = ["OWNER_PASSWORD_HASH", "GDS_BASE_URL", "LEGACY_SECRET_CONNECTOR", "STRAVA_CLIENT_ID",
                    "STRAVA_CLIENT_SECRET"]


def _vault_upsert(conn, name: str, value: str, description: str) -> None:
    row = conn.execute("select id from vault.secrets where name = %s", (name,)).fetchone()
    if row:
        conn.execute("select vault.update_secret(%s, %s)", (row[0], value))
    else:
        conn.execute("select vault.create_secret(%s, %s, %s)", (value, name, description))


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def write_env(values: dict[str, str]) -> None:
    lines = ["# garmin-datastore secrets. Never commit this file.", ""]
    lines += [f"{k}={v}" for k, v in values.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_env_into_process() -> None:
    for k, v in read_env().items():
        os.environ.setdefault(k, v)


def _vercel() -> str:
    exe = shutil.which("vercel.cmd") or shutil.which("vercel")
    if not exe:
        sys.exit("Vercel CLI not found. Install it with: npm install -g vercel")
    return exe


def cmd_setup(args: argparse.Namespace) -> None:
    import psycopg
    from psycopg import sql

    from gds import crypto, ops

    env = read_env()

    url = env.get("DATABASE_URL", "")
    if not url or "YOUR-PASSWORD" in url or "<" in url:
        sys.exit(
            "\nDATABASE_URL in .env still needs your Supabase connection string and database password.\n"
            "See README step 3, save .env, and run setup again."
        )

    print("Checking the database connection...", end=" ", flush=True)
    try:
        with psycopg.connect(url, prepare_threshold=None, connect_timeout=10) as conn:
            conn.execute("select 1")
    except psycopg.OperationalError as e:
        reason = str(e).strip().splitlines()[-1]
        sys.exit(f"failed.\n{reason}\nCheck the password in .env (special characters must be URL-encoded).")
    print("ok")

    with psycopg.connect(url, prepare_threshold=None) as conn:
        pending = ops.pending_migrations(conn)
    if pending:
        sys.exit(f"{len(pending)} database migration(s) pending. Run first: uv run python -m gds.cli migrate")

    generated = {
        "TOKEN_ENC_KEY": crypto.new_key,
        "TICK_SECRET": lambda: secrets.token_urlsafe(32),
        "MCP_PATH_SECRET": lambda: secrets.token_urlsafe(24),
        "WORKER_DATABASE_URL": lambda: ops.worker_url(url, secrets.token_urlsafe(32)),
    }
    for name, make in generated.items():
        if not env.get(name):
            env[name] = make()
    env["GDS_ACCOUNT_LABEL"] = env.get("GDS_ACCOUNT_LABEL") or "default"
    write_env(env)
    print(f"Wrote {ENV_FILE.name} (git-ignored).")

    base = _configured(env, "GDS_BASE_URL").rstrip("/")
    optional = [k for k in OPTIONAL_VARS if env.get(k)]
    print("Setting the worker login and storing secrets in Supabase Vault...", end=" ", flush=True)
    with psycopg.connect(url, prepare_threshold=None) as conn:
        # A SCRAM verifier, not the password: the plaintext never reaches the server or its logs.
        verifier = ops.scram_verifier(ops.url_password(env["WORKER_DATABASE_URL"]))
        conn.execute(sql.SQL("alter role gds_worker with login password {}").format(sql.Literal(verifier)))
        _vault_upsert(conn, "gds_tick_secret", env["TICK_SECRET"], "garmin-datastore tick auth")
        if base:
            _vault_upsert(conn, "gds_tick_url", f"{base}/api/tick", "garmin-datastore tick endpoint")
        for k in optional:
            _vault_upsert(conn, OPTIONAL_VARS[k], env[k], f"garmin-datastore {k}")
    print("ok" + (f" (+ {', '.join(optional)})" if optional else ""))
    if not base:
        print("  GDS_BASE_URL isn't set yet, so the scheduler has no URL to call. After your first deploy,\n"
              "  put the production URL in .env and run setup again.")

    vercel = _vercel()
    if (ROOT / ".vercel" / "project.json").exists():
        print("Vercel project already linked.")
    else:
        link = [vercel, "link", "--yes", "--project", args.project]
        if args.team:
            link += ["--scope", args.team]
        print(f"Linking Vercel project '{args.project}'...")
        subprocess.run(link, cwd=ROOT, check=True)

    # The deployed app logs in as gds_worker; the admin DATABASE_URL stays on this machine.
    values = {**env, "DATABASE_URL": env["WORKER_DATABASE_URL"]}
    for name in VERCEL_VARS + optional + [k for k in VERCEL_ONLY_VARS if _configured(env, k)]:
        print(f"Setting Vercel env {name} (production)...", end=" ", flush=True)
        subprocess.run(
            [vercel, "env", "add", name, "production", "--force", "--sensitive", "--yes"],
            cwd=ROOT,
            input=values[name],
            text=True,
            check=True,
            capture_output=True,
        )
        print("ok")

    if base and not args.no_github:
        _github_secrets({"GDS_BASE_URL": base, "TICK_SECRET": env["TICK_SECRET"]})

    print("\nSetup complete. Redeploy for changed Vercel settings to take effect, then check with:\n"
          "  uv run python -m gds.cli doctor")


def _configured(env: dict[str, str], name: str) -> str:
    """A .env value, or '' while it's still the .env.example placeholder."""
    value = env.get(name, "")
    return "" if "<" in value else value


def _github_secrets(values: dict[str, str]) -> None:
    """Repo secrets for the daily backup tick (.github/workflows/backup-tick.yml)."""
    gh = shutil.which("gh")
    if not gh:
        print("GitHub CLI not found: add GDS_BASE_URL and TICK_SECRET as repository secrets by hand "
              "(Settings > Secrets and variables > Actions) for the backup tick.")
        return
    for name, value in values.items():
        print(f"Setting GitHub secret {name}...", end=" ", flush=True)
        res = subprocess.run([gh, "secret", "set", name], cwd=ROOT, input=value, text=True, capture_output=True)
        print("ok" if res.returncode == 0 else f"skipped ({res.stderr.strip()[:150]})")


def cmd_migrate(args: argparse.Namespace) -> None:
    """Apply pending supabase/migrations/*.sql in order, each in its own transaction."""
    import psycopg

    from gds import ops

    url = read_env().get("DATABASE_URL") or os.environ.get("DATABASE_URL", "")  # CI has no .env
    if not url or "<" in url:
        sys.exit("Set DATABASE_URL in .env first (README, step 3).")
    with psycopg.connect(url, prepare_threshold=None, autocommit=True) as conn:
        pending = ops.pending_migrations(conn)
        if not pending:
            print("Database is up to date.")
            return
        for m in pending:
            print(f"{'Would apply' if args.dry_run else 'Applying'} {m.path.name}...", end=" ", flush=True)
            if not args.dry_run:
                ops.apply_migration(conn, m)
            print("ok")


def cmd_doctor(_: argparse.Namespace) -> None:
    """Check the whole deployment: .env, database, migrations, Vault, pg_cron, token, sync, app, GitHub."""
    from gds import ops

    marks = {ops.OK: " ok ", ops.WARN: "warn", ops.FAIL: "FAIL", ops.INFO: "info"}

    def emit(c: ops.Check) -> None:
        print(f"[{marks[c.level]}] {c.name:<22} {c.detail}", flush=True)

    if not ops.run_doctor(read_env(), emit):
        sys.exit(1)
    print("\nAll required checks passed.")


def cmd_auth(args: argparse.Namespace) -> None:
    load_env_into_process()
    from garminconnect import Garmin

    from gds import tokens
    from gds.config import settings
    from gds.db import connect

    # A fresh login gives the cloud its own token family, separate from
    # ~/.garminconnect used by the local garmin-connect-mcp (design §7.2).
    os.environ.pop("GARMINTOKENS", None)
    print("\nGarmin Connect login. Your password is used once and never stored.")
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden): ")

    g = Garmin(email=email, password=password)
    g.login()
    token_json = g.client.dumps()
    del password

    with connect() as conn:
        acct = tokens.ensure_account(conn, settings().account_label)
        tokens.save(conn, acct, token_json, event="login")
        since = conn.execute(
            "update garmin.accounts set backfill_start = coalesce(%s::date, backfill_start, %s::date) where id = %s "
            "returning backfill_start",
            (args.since, _default_since(), acct),
        ).fetchone()["backfill_start"]
    expiry = tokens.access_expiry(token_json)
    print(f"Logged in. Encrypted token stored for account '{settings().account_label}'.")
    if expiry:
        print(f"Access token valid until {expiry:%Y-%m-%d %H:%M} UTC (refreshes automatically).")
    print(f"History is synced from {since} (change with: uv run python -m gds.cli backfill --since YYYY-MM-DD).")


def _default_since() -> dt.date:
    today = dt.date.today()
    return today.replace(year=today.year - 2, day=1)


def _iso_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {value}") from e


def cmd_backfill(args: argparse.Namespace) -> None:
    """Set how far back history is synced. The next tick creates the missing work units."""
    load_env_into_process()
    from gds import tokens
    from gds.config import settings
    from gds.db import connect

    with connect() as conn:
        acct = tokens.account_id(conn, settings().account_label)
        old = conn.execute("select backfill_start from garmin.accounts where id = %s", (acct,)).fetchone()
        conn.execute("update garmin.accounts set backfill_start = %s where id = %s", (args.since, acct))
    print(f"History start: {old['backfill_start'] or 'recent days only'} -> {args.since}. "
          "The next ticks fetch the missing days (about 200 work units per tick).")


def cmd_strava_auth(_: argparse.Namespace) -> None:
    """One-time Strava authorization for W-9 (design §8.4). Asks for the API app's client id
    and secret if .env lacks them (secret hidden), opens Strava's consent page, and catches the
    redirect on a one-shot local listener. The token is stored encrypted in the database."""
    import time
    import urllib.parse
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    env = read_env()
    changed = False
    if not env.get("STRAVA_CLIENT_ID"):
        env["STRAVA_CLIENT_ID"] = input("Strava Client ID (from strava.com/settings/api): ").strip()
        changed = True
    if not env.get("STRAVA_CLIENT_SECRET"):
        env["STRAVA_CLIENT_SECRET"] = getpass.getpass("Strava Client Secret (hidden): ").strip()
        changed = True
    if changed:
        write_env(env)
        print("Saved the Strava app credentials to .env.")
    load_env_into_process()
    from gds import strava, tokens
    from gds.config import settings
    from gds.db import connect

    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            url = urllib.parse.urlparse(self.path)
            if url.path != "/strava/callback":
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()})
            ok = "code" in result and result.get("state") == state
            body = ("Strava authorized for garmin-datastore. You can close this tab." if ok
                    else "Authorization was cancelled or didn't match; check the terminal.")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Callback)  # Strava always allows localhost redirects
    server.timeout = 5
    redirect = f"http://127.0.0.1:{server.server_port}/strava/callback"
    url = strava.authorize_url(redirect, state)
    print("\nOpening Strava in your browser. Approve both permissions (view and edit activities).")
    print(f"If it doesn't open, visit:\n  {url}\n")
    webbrowser.open(url)
    deadline = time.monotonic() + 300
    while not result and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()

    if not result:
        sys.exit("No answer from Strava within 5 minutes; run strava-auth again.")
    if result.get("state") != state:
        sys.exit("The reply didn't match this request (state mismatch); run strava-auth again.")
    if "code" not in result:
        sys.exit(f"Strava didn't authorize: {result.get('error', 'cancelled')}.")
    granted = set(result.get("scope", "").split(","))
    missing = set(strava.SCOPES.split(",")) - granted
    if missing:
        sys.exit(f"Strava granted {', '.join(sorted(granted)) or 'nothing'}; missing {', '.join(sorted(missing))}. "
                 "Run strava-auth again and leave both activity boxes ticked.")

    token = strava.exchange_code(result["code"])
    token["scope"] = result["scope"]
    with connect() as conn:
        acct = tokens.account_id(conn, settings().account_label)
        strava.save_token(conn, acct, token)
    athlete = (token.get("athlete") or {}).get("id")
    print(f"Strava authorized (athlete {athlete}). Token stored encrypted.")
    print("Next: uv run python -m gds.cli setup   (pushes STRAVA_CLIENT_ID / _SECRET to Vercel), then deploy.")


def _base_url(args: argparse.Namespace) -> str:
    base = args.base or os.environ.get("GDS_BASE_URL", "")
    if not base:
        sys.exit("Set GDS_BASE_URL in .env (your deployment's URL) or pass --base.")
    return base.rstrip("/")


def cmd_call(args: argparse.Namespace) -> None:
    """POST to a secret-protected endpoint of the deployed app and print the JSON reply."""
    import json
    import urllib.error
    import urllib.request

    load_env_into_process()  # also provides GDS_BASE_URL
    url = f"{_base_url(args)}/api/{args.path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        data=(args.body or "{}").encode(),
        method="POST",
        headers={"Content-Type": "application/json", "X-GDS-Secret": os.environ["TICK_SECRET"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            print(json.dumps(json.loads(resp.read()), indent=2, default=str))
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")


def cmd_owner_password(_: argparse.Namespace) -> None:
    """Set the password that authorizes Claude (or any MCP client) to connect. Only its
    scrypt hash is stored (OWNER_PASSWORD_HASH in .env); run `setup` to push it to Vercel."""
    from gds.oauth import hash_password

    print("\nOwner password for the Claude connector (12+ characters). Input is hidden.")
    first = getpass.getpass("New owner password: ")
    if len(first) < 12:
        sys.exit("Too short: use at least 12 characters.")
    if getpass.getpass("Repeat: ") != first:
        sys.exit("The two entries don't match.")
    env = read_env()
    env["OWNER_PASSWORD_HASH"] = hash_password(first)
    write_env(env)
    print("Saved the hash to .env. Next: uv run python -m gds.cli setup   (pushes it to Vercel)")


def cmd_tick(args: argparse.Namespace) -> None:
    """Run one sync tick on this machine (same engine as Vercel; useful for debugging)."""
    import json
    import logging

    logging.basicConfig(level=logging.INFO)
    load_env_into_process()
    from gds import engine

    print(json.dumps(engine.run_tick(trigger=args.trigger), indent=2, default=str))


def cmd_sync_now(args: argparse.Namespace) -> None:
    """Same as the connector's sync_now tool: newest activities + the last N days, right away."""
    import json
    import logging

    logging.basicConfig(level=logging.INFO)
    load_env_into_process()
    from gds import engine

    print(json.dumps(engine.sync_now(args.days), indent=2, default=str))


def cmd_renormalize(args: argparse.Namespace) -> None:
    """Rebuild typed rows for domains from stored raw payloads (no Garmin calls)."""
    load_env_into_process()
    from gds import engine
    from gds.config import settings
    from gds.db import connect

    with connect(autocommit=True) as conn:
        acct = conn.execute("select id from garmin.accounts where label = %s", (settings().account_label,)).fetchone()
        for name in args.domains:
            print(name, engine.renormalize(conn, acct["id"], name))


def cmd_connector_url(args: argparse.Namespace) -> None:
    load_env_into_process()
    if args.legacy:  # only served with LEGACY_SECRET_CONNECTOR=1
        print(f"{_base_url(args)}/s/{os.environ['MCP_PATH_SECRET']}/mcp")
    else:
        print(f"{_base_url(args)}/mcp")


def main() -> None:
    parser = argparse.ArgumentParser(prog="gds")
    sub = parser.add_subparsers(dest="command", required=True)
    p_setup = sub.add_parser("setup", help="create secrets and push them to Vercel + Supabase Vault")
    p_setup.add_argument("--project", default="garmin-datastore", help="Vercel project name")
    p_setup.add_argument("--team", default=None, help="Vercel team slug (optional)")
    p_setup.add_argument("--no-github", action="store_true", help="don't set the backup-tick repo secrets")
    p_setup.set_defaults(func=cmd_setup)
    p_mig = sub.add_parser("migrate", help="apply pending database migrations")
    p_mig.add_argument("--dry-run", action="store_true", help="list pending migrations without applying them")
    p_mig.set_defaults(func=cmd_migrate)
    sub.add_parser("doctor", help="check the whole deployment").set_defaults(func=cmd_doctor)
    p_auth = sub.add_parser("auth", help="one-time Garmin login")
    p_auth.add_argument("--since", type=_iso_date, default=None,
                        help="sync history from this date (first login default: 2 years back)")
    p_auth.set_defaults(func=cmd_auth)
    p_back = sub.add_parser("backfill", help="change how far back history is synced")
    p_back.add_argument("--since", type=_iso_date, required=True, help="YYYY-MM-DD")
    p_back.set_defaults(func=cmd_backfill)
    sub.add_parser("strava-auth", help="one-time Strava authorization (W-9)").set_defaults(func=cmd_strava_auth)
    p_call = sub.add_parser("call", help="POST to a protected endpoint, e.g. `call spike/garmin`")
    p_call.add_argument("path")
    p_call.add_argument("--body", default=None, help="JSON body")
    p_call.add_argument("--base", default=None, help="deployed app URL (or GDS_BASE_URL)")
    p_call.set_defaults(func=cmd_call)
    sub.add_parser("owner-password", help="set the connector owner password (stores a hash)").set_defaults(
        func=cmd_owner_password)
    p_tick = sub.add_parser("tick", help="run one sync tick locally")
    p_tick.add_argument("--trigger", default="local_cli")
    p_tick.set_defaults(func=cmd_tick)
    p_now = sub.add_parser("sync-now", help="fetch the newest activities and today's data now (locally)")
    p_now.add_argument("--days", type=int, default=1, help="daily data for the last N days (1-7)")
    p_now.set_defaults(func=cmd_sync_now)
    p_ren = sub.add_parser("renormalize", help="rebuild typed rows from stored raw payloads")
    p_ren.add_argument("domains", nargs="+")
    p_ren.set_defaults(func=cmd_renormalize)
    p_url = sub.add_parser("connector-url", help="print the Claude connector URL")
    p_url.add_argument("--base", default=None, help="deployed app URL (or GDS_BASE_URL)")
    p_url.add_argument("--legacy", action="store_true", help="the old secret-path URL (contains a secret)")
    p_url.set_defaults(func=cmd_connector_url)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

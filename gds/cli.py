"""`gds` command line, run on your own machine.

    uv run python -m gds.cli setup    # secrets: .env, Vercel env vars, Supabase Vault
    uv run python -m gds.cli auth     # one-time Garmin login, stores an encrypted token
    uv run python -m gds.cli tick             # one sync tick on this machine
    uv run python -m gds.cli call tick        # POST to the deployed app (GDS_BASE_URL)
    uv run python -m gds.cli connector-url    # Claude connector URL (contains a secret)

Secrets are typed into hidden prompts or generated locally. They are never printed.
"""

import argparse
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
VERCEL_ONLY_VARS = ["OWNER_PASSWORD_HASH", "GDS_BASE_URL", "LEGACY_SECRET_CONNECTOR"]


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

    from gds import crypto

    env = read_env()

    url = env.get("DATABASE_URL", "")
    if not url or "YOUR-PASSWORD" in url:
        sys.exit(
            "\nDATABASE_URL in .env still needs your database password.\n"
            "Open .env, replace [YOUR-PASSWORD] with it, save, and run setup again."
        )

    print("Checking the database connection...", end=" ", flush=True)
    try:
        with psycopg.connect(url, prepare_threshold=None, connect_timeout=10) as conn:
            conn.execute("select 1")
    except psycopg.OperationalError as e:
        reason = str(e).strip().splitlines()[-1]
        sys.exit(f"failed.\n{reason}\nCheck the password in .env (special characters must be URL-encoded).")
    print("ok")

    generated = {
        "TOKEN_ENC_KEY": crypto.new_key,
        "TICK_SECRET": lambda: secrets.token_urlsafe(32),
        "MCP_PATH_SECRET": lambda: secrets.token_urlsafe(24),
    }
    for name, make in generated.items():
        if not env.get(name):
            env[name] = make()
    env["GDS_ACCOUNT_LABEL"] = env.get("GDS_ACCOUNT_LABEL") or "default"
    write_env(env)
    print(f"Wrote {ENV_FILE.name} (git-ignored).")

    optional = [k for k in OPTIONAL_VARS if env.get(k)]
    print("Storing secrets in Supabase Vault...", end=" ", flush=True)
    with psycopg.connect(env["DATABASE_URL"], prepare_threshold=None) as conn:
        _vault_upsert(conn, "gds_tick_secret", env["TICK_SECRET"], "garmin-datastore tick auth")
        for k in optional:
            _vault_upsert(conn, OPTIONAL_VARS[k], env[k], f"garmin-datastore {k}")
    print("ok" + (f" (+ {', '.join(optional)})" if optional else ""))

    vercel = _vercel()
    if (ROOT / ".vercel" / "project.json").exists():
        print("Vercel project already linked.")
    else:
        link = [vercel, "link", "--yes", "--project", args.project]
        if args.team:
            link += ["--scope", args.team]
        print(f"Linking Vercel project '{args.project}'...")
        subprocess.run(link, cwd=ROOT, check=True)

    for name in VERCEL_VARS + optional + [k for k in VERCEL_ONLY_VARS if env.get(k)]:
        print(f"Setting Vercel env {name} (production)...", end=" ", flush=True)
        subprocess.run(
            [vercel, "env", "add", name, "production", "--force", "--sensitive", "--yes"],
            cwd=ROOT,
            input=env[name],
            text=True,
            check=True,
            capture_output=True,
        )
        print("ok")

    print("\nSetup complete. Next: uv run python -m gds.cli auth")


def cmd_auth(_: argparse.Namespace) -> None:
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
    expiry = tokens.access_expiry(token_json)
    print(f"Logged in. Encrypted token stored for account '{settings().account_label}'.")
    if expiry:
        print(f"Access token valid until {expiry:%Y-%m-%d %H:%M} UTC (refreshes automatically).")


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
    print(f"{_base_url(args)}/s/{os.environ['MCP_PATH_SECRET']}/mcp")


def main() -> None:
    parser = argparse.ArgumentParser(prog="gds")
    sub = parser.add_subparsers(dest="command", required=True)
    p_setup = sub.add_parser("setup", help="create secrets and push them to Vercel + Supabase Vault")
    p_setup.add_argument("--project", default="garmin-datastore", help="Vercel project name")
    p_setup.add_argument("--team", default=None, help="Vercel team slug (optional)")
    p_setup.set_defaults(func=cmd_setup)
    sub.add_parser("auth", help="one-time Garmin login").set_defaults(func=cmd_auth)
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
    p_ren = sub.add_parser("renormalize", help="rebuild typed rows from stored raw payloads")
    p_ren.add_argument("domains", nargs="+")
    p_ren.set_defaults(func=cmd_renormalize)
    p_url = sub.add_parser("connector-url", help="print the Claude connector URL (contains a secret)")
    p_url.add_argument("--base", default=None, help="deployed app URL (or GDS_BASE_URL)")
    p_url.set_defaults(func=cmd_connector_url)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

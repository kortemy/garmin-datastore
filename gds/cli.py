"""`gds` command line, run on your own machine.

    uv run python -m gds.cli setup    # secrets: .env, Vercel env vars, Supabase Vault
    uv run python -m gds.cli auth     # one-time Garmin login, stores an encrypted token
    uv run python -m gds.cli call spike/garmin --base https://<app>.vercel.app
    uv run python -m gds.cli connector-url --base https://<app>.vercel.app

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

    print("Storing the tick secret in Supabase Vault...", end=" ", flush=True)
    with psycopg.connect(env["DATABASE_URL"], prepare_threshold=None) as conn:
        row = conn.execute("select id from vault.secrets where name = 'gds_tick_secret'").fetchone()
        if row:
            conn.execute("select vault.update_secret(%s, %s)", (row[0], env["TICK_SECRET"]))
        else:
            conn.execute(
                "select vault.create_secret(%s, 'gds_tick_secret', 'garmin-datastore tick auth')",
                (env["TICK_SECRET"],),
            )
    print("ok")

    vercel = _vercel()
    link = [vercel, "link", "--yes", "--project", args.project]
    if args.team:
        link += ["--team", args.team]
    print(f"Linking Vercel project '{args.project}'...")
    subprocess.run(link, cwd=ROOT, check=True)

    for name in VERCEL_VARS:
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
        sys.exit("Pass --base https://<your-app>.vercel.app or set GDS_BASE_URL.")
    return base.rstrip("/")


def cmd_call(args: argparse.Namespace) -> None:
    """POST to a secret-protected endpoint of the deployed app and print the JSON reply."""
    import json
    import urllib.error
    import urllib.request

    load_env_into_process()
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
    p_url = sub.add_parser("connector-url", help="print the Claude connector URL (contains a secret)")
    p_url.add_argument("--base", default=None, help="deployed app URL (or GDS_BASE_URL)")
    p_url.set_defaults(func=cmd_connector_url)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

"""OAuth 2.1 authorization server for the Claude connector (design §8.1).

The MCP SDK provides the protocol endpoints (/authorize, /token, /register, /revoke and the
.well-known metadata). This module stores clients, pending requests, codes and tokens in
Postgres and gates authorization behind a single owner password (scrypt hash in
OWNER_PASSWORD_HASH). Only SHA-256 hashes of codes and tokens are stored.
"""

import base64
import datetime as dt
import hashlib
import hmac
import html
import secrets
import time
import uuid
from typing import Any

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg.types.json import Jsonb

from gds.db import connect

ACCESS_TTL = dt.timedelta(hours=1)
REFRESH_TTL = dt.timedelta(days=90)
CODE_TTL = dt.timedelta(minutes=5)
REQUEST_TTL = dt.timedelta(minutes=10)
MAX_FAILURES_PER_HOUR = 10
SUBJECT = "owner"


# ---------------------------------------------------------------- owner password


def hash_password(password: str, *, n: int = 2**15, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.urlsafe_b64encode(b).decode()  # noqa: E731
    return f"scrypt${n}${r}${p}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.urlsafe_b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r),
                                p=int(p), maxmem=128 * 1024 * 1024, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---------------------------------------------------------------- provider


class DbOAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider on Postgres."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    # -- clients (dynamic registration)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        def q():
            with connect() as conn:
                return conn.execute("select info from garmin.oauth_clients where client_id = %s",
                                    (client_id,)).fetchone()
        row = await anyio.to_thread.run_sync(q)
        return OAuthClientInformationFull.model_validate(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        info = client_info.model_dump(mode="json", exclude_none=True)

        def q():
            with connect() as conn:
                conn.execute("""insert into garmin.oauth_clients (client_id, info) values (%s, %s)
                                on conflict (client_id) do update set info = excluded.info""",
                             (client_info.client_id, Jsonb(info)))
        await anyio.to_thread.run_sync(q)

    # -- authorization: park the request, send the browser to the owner login page

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        data = params.model_dump(mode="json")

        def q():
            with connect() as conn:
                conn.execute("delete from garmin.oauth_requests where expires_at < now()")
                conn.execute("""insert into garmin.oauth_requests (id, client_id, params, expires_at)
                                values (%s, %s, %s, %s)""",
                             (request_id, client.client_id, Jsonb(data), _now() + REQUEST_TTL))
        await anyio.to_thread.run_sync(q)
        return f"{self.base_url}/oauth/login?request={request_id}"

    # -- codes

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        def q():
            with connect() as conn:
                return conn.execute("""select data, expires_at from garmin.oauth_codes
                                       where code_hash = %s and client_id = %s""",
                                    (_h(authorization_code), client.client_id)).fetchone()
        row = await anyio.to_thread.run_sync(q)
        if not row:
            return None
        return AuthorizationCode(code=authorization_code, expires_at=row["expires_at"].timestamp(), **row["data"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        def q():
            with connect() as conn:
                deleted = conn.execute("delete from garmin.oauth_codes where code_hash = %s returning 1",
                                       (_h(authorization_code.code),)).fetchone()
                if not deleted:  # single use: a replayed code gets nothing
                    raise TokenError("invalid_grant", "authorization code already used")
                return _issue(conn, client.client_id, authorization_code.scopes, authorization_code.resource)
        return await anyio.to_thread.run_sync(q)

    # -- refresh tokens (rotated on every use)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        row = await anyio.to_thread.run_sync(lambda: _load_token(refresh_token, "refresh", client.client_id))
        if not row:
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=row["scopes"],
                            expires_at=int(row["expires_at"].timestamp()), resource=row["resource"], subject=SUBJECT)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token, scopes: list[str]):
        def q():
            with connect() as conn:
                family = conn.execute("""delete from garmin.oauth_tokens where token_hash = %s and kind = 'refresh'
                                         returning family""", (_h(refresh_token.token),)).fetchone()
                if not family:
                    raise TokenError("invalid_grant", "refresh token already used")
                conn.execute("delete from garmin.oauth_tokens where family = %s", (family["family"],))
                return _issue(conn, client.client_id, scopes or refresh_token.scopes, refresh_token.resource)
        return await anyio.to_thread.run_sync(q)

    # -- access tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await anyio.to_thread.run_sync(lambda: _load_token(token, "access", None))
        if not row:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"],
                           expires_at=int(row["expires_at"].timestamp()), resource=row["resource"], subject=SUBJECT)

    async def revoke_token(self, token) -> None:
        def q():
            with connect() as conn:
                conn.execute("""delete from garmin.oauth_tokens where family in
                                (select family from garmin.oauth_tokens where token_hash = %s)""", (_h(token.token),))
        await anyio.to_thread.run_sync(q)


def _load_token(token: str, kind: str, client_id: str | None) -> dict[str, Any] | None:
    with connect() as conn:
        return conn.execute(
            """select client_id, scopes, resource, expires_at from garmin.oauth_tokens
               where token_hash = %s and kind = %s and expires_at > now()
                 and (%s::text is null or client_id = %s)""",
            (_h(token), kind, client_id, client_id),
        ).fetchone()


def _issue(conn, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
    access, refresh, family = secrets.token_urlsafe(32), secrets.token_urlsafe(48), uuid.uuid4()
    conn.execute("delete from garmin.oauth_tokens where expires_at < now()")
    for token, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
        conn.execute("""insert into garmin.oauth_tokens
                          (token_hash, kind, family, client_id, scopes, resource, expires_at)
                        values (%s, %s, %s, %s, %s, %s, %s)""",
                     (_h(token), kind, family, client_id, scopes, resource, _now() + ttl))
    return OAuthToken(access_token=access, expires_in=int(ACCESS_TTL.total_seconds()),
                      scope=" ".join(scopes) or None, refresh_token=refresh)


# ---------------------------------------------------------------- owner login page


def pending_request(request_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return conn.execute(
            """select r.client_id, r.params, c.info ->> 'client_name' as client_name
               from garmin.oauth_requests r join garmin.oauth_clients c using (client_id)
               where r.id = %s and r.expires_at > now()""",
            (request_id,),
        ).fetchone()


def complete_login(request_id: str, password: str, password_hash: str) -> tuple[str | None, str | None]:
    """Check the owner password; on success issue a code and return (redirect_url, None)."""
    with connect() as conn:
        failures = conn.execute(
            "select count(*) as n from garmin.oauth_login_failures where at > now() - interval '1 hour'"
        ).fetchone()["n"]
        if failures >= MAX_FAILURES_PER_HOUR:
            return None, "Too many failed attempts. Try again in an hour."
        if not password_hash or not verify_password(password, password_hash):
            conn.execute("insert into garmin.oauth_login_failures default values")
            time.sleep(1)  # slow down guessing
            return None, "Wrong password."
        req = conn.execute(
            "delete from garmin.oauth_requests where id = %s and expires_at > now() returning client_id, params",
            (request_id,),
        ).fetchone()
        if not req:
            return None, "This sign-in link has expired. Start the connection again from Claude."
        p = req["params"]
        code = secrets.token_urlsafe(32)
        data = {
            "scopes": p.get("scopes") or [],
            "client_id": req["client_id"],
            "code_challenge": p["code_challenge"],
            "redirect_uri": p["redirect_uri"],
            "redirect_uri_provided_explicitly": p["redirect_uri_provided_explicitly"],
            "resource": p.get("resource"),
            "subject": SUBJECT,
        }
        conn.execute("insert into garmin.oauth_codes (code_hash, client_id, data, expires_at) values (%s, %s, %s, %s)",
                     (_h(code), req["client_id"], Jsonb(data), _now() + CODE_TTL))
    return construct_redirect_uri(p["redirect_uri"], code=code, state=p.get("state")), None


def login_page(request_id: str, client_name: str | None, error: str | None = None) -> str:
    who = html.escape(client_name or "An application")
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Garmin Datastore — sign in</title>
<style>
:root {{ color-scheme: light dark; --bg:#f6f6f4; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff; --accent:#2f6fde;
  --err:#b42318; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141413; --fg:#ecebe7; --muted:#a3a29c; --card:#1f1f1d;
  --accent:#7aa5ff; --err:#ff8a7a; }} }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
  font:16px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; padding:16px; box-sizing:border-box; }}
main {{ background:var(--card); padding:28px; border-radius:14px; width:100%; max-width:380px;
  box-shadow:0 1px 3px rgba(0,0,0,.08); }}
h1 {{ font-size:20px; margin:0 0 6px; }} p {{ margin:0 0 16px; color:var(--muted); }}
label {{ display:block; font-weight:600; margin-bottom:6px; }}
input {{ width:100%; box-sizing:border-box; padding:10px 12px; font:inherit; border-radius:8px;
  border:1px solid color-mix(in srgb, var(--fg) 25%, transparent); background:transparent; color:inherit; }}
button {{ margin-top:16px; width:100%; padding:11px; font:inherit; font-weight:600; border:0; border-radius:8px;
  background:var(--accent); color:#fff; cursor:pointer; }} .err {{ color:var(--err); }}
</style></head><body><main>
<h1>Garmin Datastore</h1>
<p><strong>{who}</strong> wants read and write access to your Garmin data.</p>{err}
<form method="post" action="/oauth/login">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<label for="password">Owner password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Allow access</button>
</form></main></body></html>"""



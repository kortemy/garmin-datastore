"""End-to-end OAuth check against the real database, run locally (no deployment involved).

Runs the full client flow Claude uses (dynamic registration -> /authorize -> owner login ->
/token with PKCE -> MCP tool call -> refresh rotation -> revoke) with a throwaway owner
password generated for this run only, then deletes everything it created.

    uv run python scripts/oauth_e2e.py
"""

import base64
import hashlib
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gds.cli import load_env_into_process  # noqa: E402

load_env_into_process()
os.environ["GDS_BASE_URL"] = "https://testserver"  # the SDK requires an HTTPS issuer
PASSWORD = secrets.token_urlsafe(18)  # throwaway, never stored
from gds import oauth  # noqa: E402

os.environ["OWNER_PASSWORD_HASH"] = oauth.hash_password(PASSWORD, n=2**12)

from fastapi.testclient import TestClient  # noqa: E402

from gds.app import app  # noqa: E402
from gds.db import connect  # noqa: E402

MCP = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
REDIRECT = "http://localhost:9999/callback"


def check(cond: bool, label: str) -> None:
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        raise SystemExit(1)


def mcp_call(c: TestClient, token: str, method: str, params: dict | None = None):
    return c.post("/mcp", headers={**MCP, "Authorization": f"Bearer {token}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def main() -> None:
    client_id = None
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        try:
            reg = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "oauth e2e test",
                                            "token_endpoint_auth_method": "none",
                                            "grant_types": ["authorization_code", "refresh_token"],
                                            "response_types": ["code"]})
            check(reg.status_code == 201, f"dynamic client registration ({reg.status_code})")
            client_id = reg.json()["client_id"]

            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            auth = c.get("/authorize", params={"response_type": "code", "client_id": client_id,
                                               "redirect_uri": REDIRECT, "code_challenge": challenge,
                                               "code_challenge_method": "S256", "state": "st4te"})
            check(auth.status_code == 302 and "/oauth/login?request=" in auth.headers["location"],
                  "authorize redirects to owner login")
            request_id = parse_qs(urlparse(auth.headers["location"]).query)["request"][0]

            page = c.get("/oauth/login", params={"request": request_id})
            check(page.status_code == 200 and "oauth e2e test" in page.text, "login page shows the client name")

            bad = c.post("/oauth/login", data={"request": request_id, "password": "not-it"})
            check(bad.status_code == 400 and "Wrong password" in bad.text, "wrong password is refused")

            ok = c.post("/oauth/login", data={"request": request_id, "password": PASSWORD})
            check(ok.status_code == 302 and ok.headers["location"].startswith(REDIRECT), "owner login redirects back")
            q = parse_qs(urlparse(ok.headers["location"]).query)
            check(q.get("state") == ["st4te"], "state is round-tripped")
            code = q["code"][0]

            replay = c.post("/oauth/login", data={"request": request_id, "password": PASSWORD})
            check(replay.status_code == 400, "login request is single use")

            tok = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                         "client_id": client_id, "code_verifier": verifier})
            check(tok.status_code == 200, f"code exchange with PKCE ({tok.status_code})")
            tokens = tok.json()
            check(tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600, "access token, 1 h")

            again = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                           "client_id": client_id, "code_verifier": verifier})
            check(again.status_code == 400, "authorization code is single use")

            listed = mcp_call(c, tokens["access_token"], "tools/list")
            n_tools = len(listed.json()["result"]["tools"]) if listed.status_code == 200 else 0
            check(n_tools == 16, "MCP tools/list with bearer")
            args = {"fields": ["readiness_score", "sleep_score"]}
            called = mcp_call(c, tokens["access_token"], "tools/call",
                              {"name": "get_daily_wellness", "arguments": args})
            body = called.json()["result"]
            check(called.status_code == 200 and not body.get("isError"), "get_daily_wellness via OAuth")
            print("     ", str(body.get("structuredContent", body))[:160])
            sq = mcp_call(c, tokens["access_token"], "tools/call",
                          {"name": "query_sql", "arguments": {"sql": "select count(*) as n from garmin.v_activities"}})
            check(sq.status_code == 200 and not sq.json()["result"].get("isError"), "query_sql as reader role")
            denied = mcp_call(c, tokens["access_token"], "tools/call",
                              {"name": "query_sql", "arguments": {"sql": "select * from garmin.garmin_tokens"}})
            check(denied.json()["result"].get("isError") is True, "reader role cannot read the Garmin token table")

            ref = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                         "client_id": client_id})
            check(ref.status_code == 200, "refresh token exchange")
            new = ref.json()
            reuse = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                           "client_id": client_id})
            check(reuse.status_code == 400, "old refresh token is rotated out")
            check(mcp_call(c, tokens["access_token"], "tools/list").status_code == 401, "old access token revoked")

            # The SDK's RevocationRequest needs the client_secret field present, even empty.
            rev = c.post("/revoke", data={"token": new["access_token"], "client_id": client_id, "client_secret": ""})
            check(rev.status_code == 200, "revocation endpoint")
            check(mcp_call(c, new["access_token"], "tools/list").status_code == 401, "revoked token no longer works")
        finally:
            with connect() as conn:
                if client_id:
                    conn.execute("delete from garmin.oauth_clients where client_id = %s", (client_id,))
                conn.execute("delete from garmin.oauth_login_failures where at > now() - interval '10 minutes'")
            print("cleaned up test client, tokens and login-failure rows")


if __name__ == "__main__":
    main()

"""Strava, write-only (W-9, design §8.4): set an activity's title and description.

Nothing from Strava is synced or stored except what a write needs: the matching activity's
id, and its previous title/description in `write_log.undo` so `undo_write` can restore them.

The token is stored encrypted in `garmin.strava_tokens`. Strava access tokens last ~6 h and
a refresh may return a new refresh token, so — as with Garmin — refreshes happen only under a
row lock, only when < 20 min are left, and the new pair is saved before use.
"""

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import psycopg

from gds import crypto
from gds.alerts import raise_alert
from gds.config import settings
from gds.tools import ToolError

API = "https://www.strava.com/api/v3"
OAUTH = "https://www.strava.com/oauth"
SCOPES = "activity:write,activity:read_all"  # read_all: private activities must be matchable too
REFRESH_MARGIN = dt.timedelta(minutes=20)
MATCH_WINDOW = dt.timedelta(minutes=2)
HEADERS = {"User-Agent": "garmin-datastore/1.0"}
_AAD = b"gds-strava-token-v1"


def configured() -> bool:
    s = settings()
    return bool(s.strava_client_id and s.strava_client_secret)


# ---------------------------------------------------------------- tokens


def save_token(conn: psycopg.Connection, acct: uuid.UUID, token: dict[str, Any]) -> None:
    """Store Strava's token response ({access_token, refresh_token, expires_at, athlete?, scope?})."""
    keep = {k: token.get(k) for k in ("access_token", "refresh_token", "expires_at")}
    ciphertext, nonce = crypto.encrypt(json.dumps(keep), settings().token_enc_key, _AAD)
    athlete = (token.get("athlete") or {}).get("id")
    conn.execute(
        """insert into garmin.strava_tokens (account_id, athlete_id, scopes, ciphertext, nonce, access_expires_at)
           values (%s, %s, %s, %s, %s, to_timestamp(%s))
           on conflict (account_id) do update
             set athlete_id = coalesce(excluded.athlete_id, garmin.strava_tokens.athlete_id),
                 scopes = coalesce(excluded.scopes, garmin.strava_tokens.scopes),
                 ciphertext = excluded.ciphertext, nonce = excluded.nonce,
                 access_expires_at = excluded.access_expires_at, status = 'active', updated_at = now()""",
        (acct, athlete, token.get("scope"), ciphertext, nonce, keep["expires_at"]),
    )


def _post_token(data: dict[str, str]) -> httpx.Response:
    s = settings()
    return httpx.post(f"{OAUTH}/token", headers=HEADERS, timeout=20,
                      data={"client_id": s.strava_client_id, "client_secret": s.strava_client_secret, **data})


def exchange_code(code: str) -> dict[str, Any]:
    """Authorization code → token (used once by `gds strava-auth`)."""
    r = _post_token({"code": code, "grant_type": "authorization_code"})
    if r.status_code != 200:
        raise RuntimeError(f"Strava refused the authorization code: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def _needs_reauth(conn: psycopg.Connection, acct: uuid.UUID, why: str) -> ToolError:
    conn.execute("update garmin.strava_tokens set status = 'needs_reauth', updated_at = now() where account_id = %s",
                 (acct,))
    raise_alert(conn, acct, "strava_needs_reauth",
                f"Strava no longer accepts the stored authorization ({why}).\n\n"
                "Re-authorize on your PC:\n  uv run python -m gds.cli strava-auth")
    return ToolError("Strava's authorization expired or was revoked; the owner must run `gds strava-auth`. "
                     "Nothing was written.")


def access_token(conn: psycopg.Connection, acct: uuid.UUID) -> str:
    """A Strava access token valid for ≥ 20 min, refreshing it under a row lock if needed."""
    if not configured():
        raise ToolError("Strava isn't set up for this deployment (STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET).")
    rejected = None
    with conn.transaction():
        token = _locked_token(conn, acct)
        expires = dt.datetime.fromtimestamp(float(token.get("expires_at") or 0), dt.UTC)
        if expires - dt.datetime.now(dt.UTC) >= REFRESH_MARGIN:
            return token["access_token"]
        try:
            r = _post_token({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
        except httpx.HTTPError as e:
            raise ToolError(f"Couldn't reach Strava to refresh its token ({type(e).__name__}); nothing was written.") \
                from e
        if r.status_code in (400, 401, 403):
            rejected = f"refresh HTTP {r.status_code}"
        elif r.status_code != 200:
            raise ToolError(f"Strava token refresh failed (HTTP {r.status_code}); nothing was written.")
        else:
            fresh = r.json()
            save_token(conn, acct, fresh)  # may carry a new refresh token: persist before use
            return fresh["access_token"]
    # Outside the locked transaction, so the status change and alert are committed.
    raise _needs_reauth(conn, acct, rejected)


def _locked_token(conn: psycopg.Connection, acct: uuid.UUID) -> dict[str, Any]:
    """The decrypted token, row-locked until the caller's transaction ends."""
    row = conn.execute(
        "select ciphertext, nonce, status from garmin.strava_tokens where account_id = %s for update", (acct,)
    ).fetchone()
    if not row:
        raise ToolError("Strava isn't authorized yet; the owner must run `gds strava-auth`.")
    if row["status"] == "needs_reauth":
        raise ToolError("Strava needs re-authorization; the owner must run `gds strava-auth`.")
    return json.loads(crypto.decrypt(bytes(row["ciphertext"]), bytes(row["nonce"]), settings().token_enc_key, _AAD))


# ---------------------------------------------------------------- API


class StravaClient:
    def __init__(self, token: str, conn: psycopg.Connection | None = None, acct: uuid.UUID | None = None):
        self.http = httpx.Client(base_url=API, timeout=20,
                                 headers={**HEADERS, "Authorization": f"Bearer {token}"})
        self.conn, self.acct = conn, acct

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            r = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise ToolError(f"Couldn't reach Strava ({type(e).__name__})"
                            + ("; the update may or may not have been applied." if method != "GET" else ".")) from e
        if r.status_code == 401:
            if self.conn is not None and self.acct is not None:
                raise _needs_reauth(self.conn, self.acct, "API 401")
            raise ToolError("Strava rejected the token (401).")
        if r.status_code == 403:
            raise ToolError("Strava refused: the authorization lacks a needed scope (activity:write / "
                            "activity:read_all). Run `gds strava-auth` again and keep both boxes ticked.")
        if r.status_code == 404:
            raise ToolError(f"Strava has no such activity ({path}).")
        if r.status_code == 429:
            raise ToolError("Strava's rate limit was hit; try again in 15 minutes. Nothing was written.")
        if r.status_code >= 400:
            raise ToolError(f"Strava returned HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def activities_between(self, after: dt.datetime, before: dt.datetime) -> list[dict[str, Any]]:
        return self._call("GET", "/athlete/activities", params={
            "after": int(after.timestamp()), "before": int(before.timestamp()), "per_page": 50}) or []

    def activity(self, strava_id: int) -> dict[str, Any]:
        return self._call("GET", f"/activities/{strava_id}")

    def update(self, strava_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/activities/{strava_id}", json=fields)

    def close(self) -> None:
        self.http.close()


def _start(a: dict[str, Any]) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(a["start_date"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def pick_match(candidates: list[dict[str, Any]], start_utc: dt.datetime, garmin_id: int) -> dict[str, Any]:
    """The one Strava activity that is this Garmin activity: same start (± 2 min); if several,
    the ones Garmin uploaded (external_id `garmin_ping_<upload id>` — not the activity id), then
    the closest start. Refuses when it can't be sure."""
    near = [a for a in candidates if (s := _start(a)) and abs(s - start_utc) <= MATCH_WINDOW]
    if len(near) > 1:
        near = [a for a in near if str(a.get("external_id") or "").startswith("garmin")] or near
    if len(near) > 1:  # e.g. a duplicate upload: only an exact start is unambiguous
        exact = [a for a in near if _start(a) == start_utc.replace(microsecond=0)]
        near = exact if len(exact) == 1 else near
    if not near:
        raise ToolError(f"No Strava activity starts within 2 minutes of this one ({start_utc:%Y-%m-%d %H:%M} UTC). "
                        "It may not have been uploaded to Strava yet.")
    if len(near) > 1:
        ids = ", ".join(str(a.get("id")) for a in near)
        raise ToolError(f"Several Strava activities start at that time ({ids}); not guessing which one.")
    return near[0]


def find_activity(client: StravaClient, start_utc: dt.datetime, garmin_id: int) -> dict[str, Any]:
    window = dt.timedelta(hours=1)
    return pick_match(client.activities_between(start_utc - window, start_utc + window), start_utc, garmin_id)


def authorize_url(redirect_uri: str, state: str) -> str:
    q = httpx.QueryParams({"client_id": settings().strava_client_id, "redirect_uri": redirect_uri,
                           "response_type": "code", "approval_prompt": "force", "scope": SCOPES, "state": state})
    return f"{OAUTH}/authorize?{q}"

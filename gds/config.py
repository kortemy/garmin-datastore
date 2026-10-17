"""All settings come from the environment (FR-21). See .env.example."""

import os
from dataclasses import dataclass
from functools import lru_cache


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    database_url: str
    token_enc_key: str
    tick_secret: str
    mcp_path_secret: str
    account_label: str
    tick_budget_s: float
    request_interval_s: float
    incremental_every_h: float
    recent_window_days: int
    resend_api_key: str
    alert_email: str
    alert_from: str


@lru_cache
def settings() -> Settings:
    return Settings(
        database_url=_req("DATABASE_URL"),
        token_enc_key=_req("TOKEN_ENC_KEY"),
        tick_secret=_req("TICK_SECRET"),
        mcp_path_secret=_req("MCP_PATH_SECRET"),
        account_label=_opt("GDS_ACCOUNT_LABEL", "default"),
        tick_budget_s=float(_opt("GDS_TICK_BUDGET_S", "240")),
        request_interval_s=float(_opt("GDS_REQUEST_INTERVAL_S", "1.0")),
        incremental_every_h=float(_opt("GDS_INCREMENTAL_EVERY_H", "3")),
        recent_window_days=int(_opt("GDS_RECENT_WINDOW_DAYS", "3")),
        resend_api_key=_opt("RESEND_API_KEY"),
        alert_email=_opt("ALERT_EMAIL"),
        alert_from=_opt("ALERT_FROM", "Garmin Datastore <onboarding@resend.dev>"),
    )

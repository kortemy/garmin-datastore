"""All settings come from the environment (FR-21). See .env.example."""

import os
from dataclasses import dataclass
from functools import lru_cache


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    token_enc_key: str
    tick_secret: str
    mcp_path_secret: str
    account_label: str


@lru_cache
def settings() -> Settings:
    return Settings(
        database_url=_req("DATABASE_URL"),
        token_enc_key=_req("TOKEN_ENC_KEY"),
        tick_secret=_req("TICK_SECRET"),
        mcp_path_secret=_req("MCP_PATH_SECRET"),
        account_label=os.environ.get("GDS_ACCOUNT_LABEL", "default"),
    )

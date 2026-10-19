"""Dummy settings so modules that read `gds.config.settings()` work without a real .env."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("TOKEN_ENC_KEY", "A" * 43 + "=")
os.environ.setdefault("TICK_SECRET", "tick-secret")
os.environ.setdefault("MCP_PATH_SECRET", "path-secret")
os.environ.setdefault("GDS_BASE_URL", "https://gds.example.test")

"""
Configuration for DOM & Bookmap Analyzer.
Reads from the parent Risk-Management/.env for shared Dhan credentials,
or from a local .env if running standalone.
"""

import os
from pathlib import Path

# Try parent .env first (shared with Risk-Management), then local .env
_parent_env = Path(__file__).resolve().parent.parent / ".env"
_local_env = Path(__file__).resolve().parent / ".env"

try:
    from dotenv import load_dotenv
    if _parent_env.exists():
        load_dotenv(_parent_env)
    if _local_env.exists():
        load_dotenv(_local_env, override=True)
except ImportError:
    pass  # dotenv not installed; rely on actual env vars


class Config:
    # Dhan API (shared with Risk-Management)
    DHAN_CLIENT_ID: str = os.getenv("DHAN_CLIENT_ID", "")
    DHAN_ACCESS_TOKEN: str = os.getenv("DHAN_ACCESS_TOKEN", "")

    # Server
    HOST: str = os.getenv("DOM_HOST", "0.0.0.0")
    PORT: int = int(os.getenv("DOM_PORT", "5556"))

    @classmethod
    def validate(cls) -> list[str]:
        errors = []
        if not cls.DHAN_CLIENT_ID:
            errors.append("DHAN_CLIENT_ID is required")
        if not cls.DHAN_ACCESS_TOKEN:
            errors.append("DHAN_ACCESS_TOKEN is required")
        return errors

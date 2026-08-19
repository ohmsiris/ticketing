"""
Loads configuration from environment variables (and a local .env file, if
present) into one place so every other module just does:

    from app.config import settings
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root before we read anything. In production
# (Railway/Render) real env vars are already set and this is a no-op.
load_dotenv()

# Timezone used for all "human" dates/times: working hours, the daily digest,
# and due dates. Everything scheduling-related is anchored to this.
TIMEZONE = "Asia/Bangkok"


@dataclass(frozen=True)
class Settings:
    line_channel_secret: str
    line_channel_access_token: str
    ohm_line_user_id: str
    mom_line_user_id: str
    anthropic_api_key: str
    db_path: str
    port: int


def _load() -> Settings:
    return Settings(
        line_channel_secret=os.environ.get("LINE_CHANNEL_SECRET", ""),
        line_channel_access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
        ohm_line_user_id=os.environ.get("OHM_LINE_USER_ID", ""),
        mom_line_user_id=os.environ.get("MOM_LINE_USER_ID", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        db_path=os.environ.get("DB_PATH", "data/tickets.db"),
        port=int(os.environ.get("PORT", "8000")),
    )


settings = _load()

# Make sure the directory for the SQLite file exists (e.g. "data/").
Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

"""
Loads configuration from environment variables (and a local .env file, if
present) into one place so every other module just does:

    from app.config import settings
"""
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root before we read anything. In production
# (Railway/Render) real env vars are already set and this is a no-op.
load_dotenv()

logger = logging.getLogger("ticketing.config")

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
    dashboard_token: str
    # -- bill tracking --
    google_service_account_json: str  # full JSON key content, not a file path
    bills_sheet_id: str
    line_items_sheet_id: str
    review_token: str
    public_base_url: str  # e.g. https://ticketing-production-xxxx.up.railway.app (no trailing slash)


def _load() -> Settings:
    return Settings(
        line_channel_secret=os.environ.get("LINE_CHANNEL_SECRET", ""),
        line_channel_access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
        ohm_line_user_id=os.environ.get("OHM_LINE_USER_ID", ""),
        mom_line_user_id=os.environ.get("MOM_LINE_USER_ID", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        db_path=os.environ.get("DB_PATH", "data/tickets.db"),
        port=int(os.environ.get("PORT", "8000")),
        dashboard_token=os.environ.get("DASHBOARD_TOKEN", ""),
        google_service_account_json=os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
        bills_sheet_id=os.environ.get("BILLS_SHEET_ID", ""),
        line_items_sheet_id=os.environ.get("LINE_ITEMS_SHEET_ID", ""),
        review_token=os.environ.get("REVIEW_TOKEN", ""),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
    )


settings = _load()

# Make sure the directory for the SQLite file exists (e.g. "data/").
Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

# A relative DB_PATH silently resolves inside the container's own throwaway
# filesystem instead of a mounted volume -- the sqlite file looks fine right
# up until the next deploy wipes it with no error anywhere. Only matters on
# a real deploy (RAILWAY_ENVIRONMENT_NAME is set by Railway itself); a
# relative path is the correct, harmless default for local dev.
if os.environ.get("RAILWAY_ENVIRONMENT_NAME") and not Path(settings.db_path).is_absolute():
    logger.warning(
        "DB_PATH=%r is a relative path on Railway -- it will NOT persist across redeploys "
        "unless it's the absolute path your volume is mounted at (e.g. /data/tickets.db). "
        "See README.md's Railway deploy section.",
        settings.db_path,
    )

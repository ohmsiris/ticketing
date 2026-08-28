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
    # -- vehicle roster auto-refresh (see app/roster_sync.py) --
    drivers_sheet_id: str  # the "Drivers" Google Sheet's id, from its URL
    drivers_roster_worksheet: str  # tab name holding the roster tables; blank = first tab
    # -- payment-slip tracking (see app/slip_extraction.py, app/slips_routes.py) --
    accounting_sheet_id: str  # the "Accounting" Google Sheet's id -- verified slips sync to its Transaction Log tab
    transaction_log_worksheet: str  # tab name; defaults to "Transaction Log"
    # -- preventive-maintenance completion log (see app/maintenance.py, app/sheets_client.py) --
    maintenance_sheet_id: str  # a separate Google Sheet's id -- every completion appends a row, no review step needed
    maintenance_sheet_worksheet: str  # tab name; blank = first tab
    # -- supply/parts purchase tracking (see app/supplies.py, app/supplies_routes.py) --
    supplies_sheet_id: str
    supply_line_items_sheet_id: str
    part_prices_sheet_id: str  # flat cross-supplier price-comparison view; optional, see app/sheets_client.py
    # -- parts-catalog seed auto-refresh (see app/parts_catalog_sync.py) --
    parts_catalog_sheet_id: str  # the user's own "Saraburi Maintenence Sheet" id -- feeds app/known_parts_seed.txt


def _env(key: str, default: str = "") -> str:
    """
    os.environ.get(), stripped. Reported live: MOM_LINE_USER_ID in Railway
    had an invisible trailing character (whitespace/newline from however
    it got pasted in) that made it byte-for-byte different from the real
    LINE userId arriving on every webhook call -- resolve_reporter()'s
    exact `==` compare silently failed every single time, so every message
    Mom sent got logged as "unknown" and dropped with no reply at all, no
    error anywhere. A LINE userId (or any of these other ids/tokens) never
    legitimately contains leading/trailing whitespace, so stripping here
    is always safe and closes off this whole class of bug regardless of
    how the stray character got in.
    """
    return os.environ.get(key, default).strip()


def _load() -> Settings:
    return Settings(
        line_channel_secret=_env("LINE_CHANNEL_SECRET"),
        line_channel_access_token=_env("LINE_CHANNEL_ACCESS_TOKEN"),
        ohm_line_user_id=_env("OHM_LINE_USER_ID"),
        mom_line_user_id=_env("MOM_LINE_USER_ID"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        db_path=_env("DB_PATH", "data/tickets.db"),
        port=int(_env("PORT", "8000")),
        dashboard_token=_env("DASHBOARD_TOKEN"),
        google_service_account_json=_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
        bills_sheet_id=_env("BILLS_SHEET_ID"),
        line_items_sheet_id=_env("LINE_ITEMS_SHEET_ID"),
        review_token=_env("REVIEW_TOKEN"),
        public_base_url=_env("PUBLIC_BASE_URL").rstrip("/"),
        drivers_sheet_id=_env("DRIVERS_SHEET_ID"),
        drivers_roster_worksheet=_env("DRIVERS_ROSTER_WORKSHEET"),
        accounting_sheet_id=_env("ACCOUNTING_SHEET_ID", "1ygXYrtNzLAZ4eUVzD7ydlsACCH0QKHXdmf5LncX6ANM"),
        transaction_log_worksheet=_env("TRANSACTION_LOG_WORKSHEET", "Transaction Log"),
        maintenance_sheet_id=_env("MAINTENANCE_SHEET_ID"),
        maintenance_sheet_worksheet=_env("MAINTENANCE_SHEET_WORKSHEET"),
        supplies_sheet_id=_env("SUPPLIES_SHEET_ID"),
        supply_line_items_sheet_id=_env("SUPPLY_LINE_ITEMS_SHEET_ID"),
        part_prices_sheet_id=_env("PART_PRICES_SHEET_ID"),
        parts_catalog_sheet_id=_env("PARTS_CATALOG_SHEET_ID"),
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

"""
Daily auto-refresh of app/known_parts_seed.txt from the "Known Parts" tab
of the user's real "Saraburi Maintenence Sheet" -- so an edit there shows
up in canonical_part matching context (see app/supplies.get_known_
canonical_parts) within a day, without anyone having to ask a developer
to manually re-pull it. Same role and same safety-net shape as
app/roster_sync.py for the Drivers sheet.

The actual parsing/cleaning (flattening อะไหล่ Main / อะไหล่ / Motor
Index Main into one list, dropping status words that leak into spec
columns, normalizing the sheet's own inconsistent "Overload"/"Overload
Relay" labeling, etc.) deliberately does NOT live here -- it's a Google
Apps Script bound to that spreadsheet (apps_script/sync_known_parts.gs in
this repo, for reference/version control; the live copy the user actually
edits is in that Sheet's own Extensions > Apps Script editor) that writes
its result into a "Known Parts" tab once a day. The user asked for this
split explicitly ("most of my syncs are on there its easier to
visualise") -- they can see and tweak the matching/cleanup logic directly
in Sheets rather than needing a code change + redeploy here every time.
This module's only job is reading that already-clean tab and mirroring it
into a local file, same as roster_sync.py mirrors the Drivers sheet into
vehicle_roster.csv.

Run the Apps Script's trigger a bit BEFORE this module's own scheduled
pull (see app/jobs.py) so a same-day edit is picked up on the very next
run instead of waiting an extra day for the two schedules to line up.
"""
import logging
import os
from pathlib import Path
from typing import Optional

from app import sheets_client
from app.config import settings

logger = logging.getLogger("ticketing.parts_catalog_sync")

SEED_PATH = Path(__file__).resolve().parent / "known_parts_seed.txt"
KNOWN_PARTS_TAB = "Known Parts"


def _open_worksheet():
    client = sheets_client.get_client()
    spreadsheet = client.open_by_key(settings.parts_catalog_sheet_id)
    return spreadsheet.worksheet(KNOWN_PARTS_TAB)


def fetch_parts_seed() -> list[str]:
    """Reads the "Known Parts" tab (one canonical_part-shaped string per
    row, header in row 1 -- see apps_script/sync_known_parts.gs). Raises
    on a hard failure (network error, tab not found, sheet not shared
    with the service account, etc.) -- callers decide what to do with
    that."""
    worksheet = _open_worksheet()
    values = worksheet.col_values(1)[1:]  # skip the header row
    return sorted({v.strip() for v in values if v.strip()})


def _load_current() -> list[str]:
    if not SEED_PATH.exists():
        return []
    with open(SEED_PATH, encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def _looks_sane(new_list: list[str], current_list: list[str]) -> Optional[str]:
    """Returns None if new_list is safe to write, else a reason string."""
    if len(new_list) == 0:
        return "parsed zero entries"
    if current_list and len(new_list) < len(current_list) * 0.5:
        return f"parsed only {len(new_list)} entries, less than half of the current {len(current_list)} -- looks like a partial read"
    return None


def refresh_parts_seed() -> bool:
    """The scheduled job entry point. Returns True if known_parts_seed.txt
    was updated, False if left untouched (nothing changed, or the new
    data failed the sanity check). Never raises -- a failure here should
    never take down the scheduler or block a supply-purchase extraction
    from running with whatever seed file is already on disk."""
    if not settings.parts_catalog_sheet_id:
        logger.debug("PARTS_CATALOG_SHEET_ID not set -- skipping parts-catalog auto-refresh")
        return False

    current = _load_current()
    try:
        new_list = fetch_parts_seed()
    except Exception:
        logger.exception(
            "parts-catalog auto-refresh failed to read the '%s' tab -- keeping existing known_parts_seed.txt",
            KNOWN_PARTS_TAB,
        )
        return False

    reason = _looks_sane(new_list, current)
    if reason is not None:
        logger.warning("parts-catalog auto-refresh got suspicious data, NOT overwriting known_parts_seed.txt: %s", reason)
        return False

    if set(new_list) == set(current):
        logger.info("parts-catalog auto-refresh: no changes (%d entries)", len(new_list))
        return False

    # Write atomically -- a crash mid-write must never leave a half-written
    # file in place of a good one.
    tmp_path = SEED_PATH.with_suffix(".txt.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for entry in new_list:
            f.write(entry + "\n")
    os.replace(tmp_path, SEED_PATH)
    logger.info(
        "parts-catalog auto-refresh: updated known_parts_seed.txt (%d entries, was %d)",
        len(new_list), len(current),
    )
    return True

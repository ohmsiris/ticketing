"""
Daily auto-refresh of app/vehicle_roster.csv from the real "Drivers"
Google Sheet, so a manager's edit there (fixing a plate typo, adding a
new truck) shows up in bill matching within a day, without anyone having
to ask a developer to manually re-pull it.

Layout of that sheet (as of 2026-08): a driver list, then a "รถสระบุรี"
vehicle table, then a "รถแก่งคอย" vehicle table, all stacked in ONE
worksheet with blank rows between them -- not separate tabs. Rather than
hardcode which rows/columns each table lives at (which breaks the moment
someone inserts a row above it), this scans every row for a HEADER row
that has both a "ลำดับ" and a "ทะเบียนรถ" column -- the driver-only table
lacks "ทะเบียนรถ" so it's skipped automatically -- and reads column
positions from that header rather than assuming fixed offsets. Each
vehicle table's own "สาขา" column states its branch directly, so this
doesn't need to track section titles either.

Safety net: this NEVER overwrites the existing vehicle_roster.csv unless
the freshly parsed data looks sane (see _looks_sane). If the sheet's
layout changes in some way this parser doesn't handle, or a transient
read hiccups, the last-known-good roster is left alone and a warning is
logged -- silently shipping a half-empty roster would be worse than
staying one day stale.
"""
import csv
import logging
import os
from pathlib import Path
from typing import Optional

from app import sheets_client
from app.config import settings

logger = logging.getLogger("ticketing.roster_sync")

ROSTER_PATH = Path(__file__).resolve().parent / "vehicle_roster.csv"

# Column names as they appear in the sheet's own header row -> the CSV
# column name we store them under. Matched by header text, not position.
COLUMN_MAP = {
    "สาขา": "branch",
    "รถเบอร์": "truck_number",
    "ทะเบียนรถ": "plate",
    "จังหวัด": "province",
    "ยี่ห้อ": "make",
    "รุ่นรถ": "model",
    "ประเภทรถ": "vehicle_type",
}
CSV_HEADER = ["branch", "truck_number", "plate", "province", "make", "model", "vehicle_type"]

# A parsed row missing ALL of these is almost certainly a stray/blank
# row, not a real vehicle -- skip it rather than writing an empty entry.
MEANINGFUL_FIELDS = ("truck_number", "plate", "make", "model")

PLACEHOLDER_VALUES = {"-", "\\-", ""}


def _open_worksheet():
    client = sheets_client.get_client()
    spreadsheet = client.open_by_key(settings.drivers_sheet_id)
    if settings.drivers_roster_worksheet:
        return spreadsheet.worksheet(settings.drivers_roster_worksheet)
    return spreadsheet.sheet1


def _find_header_rows(all_values: list[list[str]]) -> list[int]:
    """Row indices (0-based) of every header row that looks like a
    vehicle table (has both ลำดับ and ทะเบียนรถ columns) -- there's
    normally one per branch, but this doesn't assume a fixed count."""
    header_rows = []
    for i, row in enumerate(all_values):
        cells = {c.strip() for c in row}
        if "ลำดับ" in cells and "ทะเบียนรถ" in cells:
            header_rows.append(i)
    return header_rows


def _parse_table(all_values: list[list[str]], header_row_idx: int) -> list[dict]:
    header = [c.strip() for c in all_values[header_row_idx]]
    col_index = {}
    for sheet_name, csv_name in COLUMN_MAP.items():
        if sheet_name in header:
            col_index[csv_name] = header.index(sheet_name)
    # "plate" and "branch" are load-bearing for matching -- without them
    # this isn't a table we know how to use, even though it matched the
    # ลำดับ/ทะเบียนรถ header scan (belt and suspenders).
    if "plate" not in col_index or "branch" not in col_index:
        logger.warning("header row %d matched but is missing a plate or branch column -- skipping", header_row_idx)
        return []

    def cell(row: list[str], csv_name: str) -> str:
        idx = col_index.get(csv_name)
        if idx is None or idx >= len(row):
            return ""
        value = row[idx].strip()
        return "" if value in PLACEHOLDER_VALUES else value

    rows = []
    for row in all_values[header_row_idx + 1:]:
        if not any(c.strip() for c in row):
            break  # blank row -- end of this table
        parsed = {csv_name: cell(row, csv_name) for csv_name in CSV_HEADER}
        if not any(parsed.get(f) for f in MEANINGFUL_FIELDS):
            continue  # e.g. a driver-only row with no vehicle assigned
        rows.append(parsed)
    return rows


def fetch_roster_rows() -> list[dict]:
    """Reads and parses every vehicle table found in the Drivers sheet.
    Raises on a hard failure (network error, sheet not shared with the
    service account, etc.) -- callers decide what to do with that."""
    worksheet = _open_worksheet()
    all_values = worksheet.get_all_values()
    header_rows = _find_header_rows(all_values)
    if not header_rows:
        raise ValueError("no vehicle table header row (ลำดับ + ทะเบียนรถ) found anywhere in the sheet")

    rows = []
    for idx in header_rows:
        rows.extend(_parse_table(all_values, idx))
    return rows


def _load_current_rows() -> list[dict]:
    if not ROSTER_PATH.exists():
        return []
    with open(ROSTER_PATH, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _looks_sane(new_rows: list[dict], current_rows: list[dict]) -> Optional[str]:
    """Returns None if new_rows is safe to write, else a reason string
    explaining why it was rejected."""
    if len(new_rows) == 0:
        return "parsed zero rows"
    branches = {r.get("branch") for r in new_rows if r.get("branch")}
    if len(branches) < 2:
        return f"only found branch(es) {branches!r} -- expected at least Saraburi and Kaeng Khoi"
    if current_rows and len(new_rows) < len(current_rows) * 0.5:
        return f"parsed only {len(new_rows)} rows, less than half of the current {len(current_rows)} -- looks like a partial read"
    missing_plate = sum(1 for r in new_rows if not r.get("plate"))
    if missing_plate > len(new_rows) * 0.7:
        return f"{missing_plate}/{len(new_rows)} rows have no plate at all -- looks like columns shifted"
    return None


def refresh_roster() -> bool:
    """The scheduled job entry point. Returns True if the roster file was
    updated, False if it was left untouched (nothing changed, or the new
    data failed the sanity check). Never raises -- a failure here should
    never take down the scheduler or block a bill from being read with
    whatever roster is already on disk."""
    if not settings.drivers_sheet_id:
        logger.debug("DRIVERS_SHEET_ID not set -- skipping roster auto-refresh")
        return False

    current_rows = _load_current_rows()
    try:
        new_rows = fetch_roster_rows()
    except Exception:
        logger.exception("roster auto-refresh failed to read the Drivers sheet -- keeping existing vehicle_roster.csv")
        return False

    reason = _looks_sane(new_rows, current_rows)
    if reason is not None:
        logger.warning("roster auto-refresh got suspicious data, NOT overwriting vehicle_roster.csv: %s", reason)
        return False

    new_key = {tuple(r.get(c, "") for c in CSV_HEADER) for r in new_rows}
    old_key = {tuple(r.get(c, "") for c in CSV_HEADER) for r in current_rows}
    if new_key == old_key:
        logger.info("roster auto-refresh: no changes (%d rows)", len(new_rows))
        return False

    # Write atomically -- a crash mid-write must never leave a half-written
    # CSV in place of a good one.
    tmp_path = ROSTER_PATH.with_suffix(".csv.tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(new_rows)
    os.replace(tmp_path, ROSTER_PATH)
    logger.info(
        "roster auto-refresh: updated vehicle_roster.csv (%d rows, was %d)",
        len(new_rows), len(current_rows),
    )
    return True

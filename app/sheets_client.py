"""
Writes CONFIRMED bills to the real Bills / LineItems Google Sheets, using
a service account (see README's setup section) rather than any
developer's personal Google login -- this needs to work unattended, on
Railway, indefinitely.

Deliberately only ever called AFTER a manager confirms a bill on the
review webpage (see app/bills_routes.py) -- the Sheets should only ever
hold verified data, never a raw AI guess.

Upsert, not append-only: a bill can be re-confirmed (e.g. the manager
opens an already-verified bill and fixes something), and that must
UPDATE the existing Sheet row(s) for that bill_id rather than adding a
duplicate. bill_id is unique per bill, so it's the natural lookup key.
"""
import json
import logging
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from app.config import settings

logger = logging.getLogger("ticketing.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

BILLS_HEADER = [
    "bill_id", "status", "source_type", "shop_name", "date", "branch",
    "vehicle_license", "vehicle_license_province", "vehicle_number",
    "mileage", "next_service_mileage", "total_cost", "source_photos",
    "created_at", "verified_at", "verified_by",
]
LINE_ITEMS_HEADER = [
    "bill_id", "line_item_number", "description", "category",
    "quantity", "unit", "unit_price", "cost",
]

_client: Optional[gspread.Client] = None


def _client_lazy() -> gspread.Client:
    global _client
    if _client is None:
        info = json.loads(settings.google_service_account_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


def _ensure_header(sheet, expected_header: list[str]) -> None:
    """The Sheets already have a header row from setup -- this is just a
    cheap safety net in case it's ever cleared by accident."""
    if not sheet.row_values(1):
        sheet.append_row(expected_header)


def _bills_sheet():
    sheet = _client_lazy().open_by_key(settings.bills_sheet_id).sheet1
    _ensure_header(sheet, BILLS_HEADER)
    return sheet


def _line_items_sheet():
    sheet = _client_lazy().open_by_key(settings.line_items_sheet_id).sheet1
    _ensure_header(sheet, LINE_ITEMS_HEADER)
    return sheet


def upsert_bill(bill: dict) -> None:
    """Updates the existing row for this bill_id if one exists (column A
    is always bill_id), otherwise appends a new row."""
    sheet = _bills_sheet()
    values = [bill.get(col, "") for col in BILLS_HEADER]
    existing = sheet.find(bill["bill_id"], in_column=1)
    if existing is not None:
        sheet.update(
            f"A{existing.row}:{gspread.utils.rowcol_to_a1(existing.row, len(BILLS_HEADER))}",
            [values],
            value_input_option="USER_ENTERED",
        )
    else:
        sheet.append_row(values, value_input_option="USER_ENTERED")


def replace_line_items(bill_id: str, line_items: list[dict]) -> None:
    """Deletes every existing row for this bill_id, then appends the
    current set -- simpler and safer than trying to diff/patch individual
    rows when items can be added or removed between edits."""
    sheet = _line_items_sheet()
    existing_cells = sheet.findall(bill_id, in_column=1)
    if existing_cells:
        # Delete from the bottom up so earlier row numbers stay valid as
        # later ones are removed.
        for cell in sorted(existing_cells, key=lambda c: c.row, reverse=True):
            sheet.delete_rows(cell.row)
    if not line_items:
        return
    rows = [[bill_id] + [item.get(col, "") for col in LINE_ITEMS_HEADER[1:]] for item in line_items]
    sheet.append_rows(rows, value_input_option="USER_ENTERED")


def sync_verified_bill(bill: dict) -> bool:
    """Writes one verified bill + its line items to the real Sheets,
    updating in place if this bill_id was already synced before (see
    upsert_bill / replace_line_items). Returns True on success.
    Deliberately doesn't raise on failure -- the SQLite row is already
    the source of truth and stays 'verified' either way; a failed Sheets
    sync is logged loudly so it's visible, but shouldn't block the
    manager's flow or lose their edits."""
    try:
        upsert_bill(bill)
        replace_line_items(bill["bill_id"], bill.get("line_items") or [])
        logger.info("synced bill %s to Google Sheets OK", bill.get("bill_id"))
        return True
    except Exception:
        logger.exception("failed to sync bill %s to Google Sheets", bill.get("bill_id"))
        return False

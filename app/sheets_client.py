"""
Writes CONFIRMED bills to the real Bills / LineItems Google Sheets, using
a service account (see README's setup section) rather than any
developer's personal Google login -- this needs to work unattended, on
Railway, indefinitely.

Deliberately only ever called AFTER a manager confirms a bill on the
review webpage (see app/bills_routes.py) -- the Sheets should only ever
hold verified data, never a raw AI guess.
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
    "vehicle_license", "vehicle_number", "mileage", "next_service_mileage",
    "total_cost", "source_photos", "created_at", "verified_at", "verified_by",
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


def append_bill(bill: dict) -> None:
    sheet = _client_lazy().open_by_key(settings.bills_sheet_id).sheet1
    _ensure_header(sheet, BILLS_HEADER)
    sheet.append_row(
        [bill.get(col, "") for col in BILLS_HEADER],
        value_input_option="USER_ENTERED",
    )


def append_line_items(bill_id: str, line_items: list[dict]) -> None:
    if not line_items:
        return
    sheet = _client_lazy().open_by_key(settings.line_items_sheet_id).sheet1
    _ensure_header(sheet, LINE_ITEMS_HEADER)
    rows = [[bill_id] + [item.get(col, "") for col in LINE_ITEMS_HEADER[1:]] for item in line_items]
    sheet.append_rows(rows, value_input_option="USER_ENTERED")


def sync_verified_bill(bill: dict) -> bool:
    """Writes one verified bill + its line items to the real Sheets.
    Returns True on success. Deliberately doesn't raise on failure --
    the SQLite row is already the source of truth and stays 'verified'
    either way; a failed Sheets sync is logged loudly so it's visible,
    but shouldn't block the manager's flow or lose their edits. (No
    retry queue yet -- a known v1 gap; re-running the review page's
    submit would just append a duplicate row, so a failed sync currently
    needs a manual copy into the Sheet rather than a resubmit.)"""
    try:
        append_bill(bill)
        append_line_items(bill["bill_id"], bill.get("line_items") or [])
        return True
    except Exception:
        logger.exception("failed to sync bill %s to Google Sheets", bill.get("bill_id"))
        return False

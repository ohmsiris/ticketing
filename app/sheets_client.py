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
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

from app.config import TIMEZONE, settings

logger = logging.getLogger("ticketing.sheets")

BANGKOK = ZoneInfo(TIMEZONE)

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


def get_client() -> gspread.Client:
    """The shared authorized gspread client (lazy singleton). Public so
    other modules that need to read a *different* sheet with the same
    service account -- e.g. app/roster_sync.py reading the Drivers sheet
    -- don't each load and authorize their own copy of the credentials."""
    global _client
    if _client is None:
        info = json.loads(settings.google_service_account_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _client = gspread.authorize(creds)
    return _client


# Old private name, kept as an alias in case anything else in this file
# still calls it below.
_client_lazy = get_client


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


# --- Payment slips -> the "Accounting" Sheet's "Transaction Log" tab ---
#
# That tab (built directly in the Sheet, not by this repo) has LIVE
# FORMULAS in column A (Entry #) and column O (Month), filled down through
# row 500 ahead of any data arriving -- see the Accounting project's
# handoff. Writing to those columns would clobber the formulas, so
# everything below deliberately touches ONLY columns B:N.
TRANSACTION_LOG_FIRST_DATA_ROW = 2
TRANSACTION_LOG_LAST_COL = "N"  # B through N = 13 columns

# slip dict key (or a literal) -> Transaction Log column, in B..N order.
# branch is translated from the roster's SRB/KK short code to the exact
# strings the sheet's own Branch dropdown offers (see _branch_label).
_BRANCH_LABELS = {"SRB": "Saraburi", "KK": "Kaeng Khoi"}


def _branch_label(branch_code: str) -> str:
    return _BRANCH_LABELS.get((branch_code or "").strip(), "")


def _slip_row_values(slip: dict) -> list:
    return [
        slip.get("transaction_date", ""),           # B: Date
        "โอนเงิน",                                    # C: Type (this flow is transfers only, never cheques)
        _branch_label(slip.get("branch", "")),        # D: Branch
        slip.get("pl_category", ""),                   # E: P&L Category
        slip.get("to_display_name", ""),                 # F: Payee
        slip.get("amount", ""),                            # G: Amount
        slip.get("account_used_label", ""),                  # H: Account Used
        slip.get("purpose_note", ""),                          # I: Purpose / Note
        "",                                                      # J: Vehicle (not on a transfer slip)
        "",                                                        # K: Cheque No. (N/A)
        "",                                                          # L: Due/Clear Date (N/A)
        "",                                                            # M: Cheque Status (N/A)
        slip.get("source_photo", ""),                                    # N: Photo Link (LINE message id)
    ]


def _transaction_log_sheet():
    return _client_lazy().open_by_key(settings.accounting_sheet_id).worksheet(settings.transaction_log_worksheet)


def _find_next_empty_row(sheet) -> int:
    """First row (>= TRANSACTION_LOG_FIRST_DATA_ROW) with an empty Date
    column -- column B has no formula in it (unlike A/O), so this is a
    reliable "first truly unused row" check even though A/O look
    non-blank that far down from the pre-filled formulas."""
    date_column = sheet.col_values(2)  # column B
    return max(len(date_column) + 1, TRANSACTION_LOG_FIRST_DATA_ROW)


def sync_verified_slip(slip: dict) -> Optional[int]:
    """Writes one verified slip into the Transaction Log tab, columns B:N
    only. Upserts by sheet_row if this slip was already synced before
    (re-confirming an edited slip updates that row in place); otherwise
    appends to the first empty Date row. Returns the row number written to
    -- the caller (app/slips_routes.py) persists it via
    app.slips.set_sheet_row so the next re-confirm updates in place.
    Deliberately doesn't raise on failure, mirroring sync_verified_bill:
    the SQLite row is already the source of truth and stays 'verified'
    either way, but a failed sync is logged loudly and returns None."""
    try:
        sheet = _transaction_log_sheet()
        values = _slip_row_values(slip)
        row = slip.get("sheet_row")
        if not row:
            row = _find_next_empty_row(sheet)
        sheet.update(f"B{row}:{TRANSACTION_LOG_LAST_COL}{row}", [values], value_input_option="USER_ENTERED")
        logger.info("synced slip %s to Transaction Log row %s", slip.get("slip_id"), row)
        return row
    except Exception:
        logger.exception("failed to sync slip %s to the Transaction Log", slip.get("slip_id"))
        return None


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


# --- Preventive maintenance -- a separate Sheet, one row per completion ---
#
# Unlike bills/slips there's no manager review step here: a completion
# report goes straight from LINE -> classify() -> maintenance_log -> this
# Sheet. Append-only by design (no upsert-by-key) -- SQLite's
# maintenance_log is already an immutable event log, and this just
# mirrors it, so there's no "existing row to find and update" concept the
# way a re-confirmed bill has.
MAINTENANCE_HEADER = ["completed_date", "category", "task", "reporter", "note", "logged_at"]


def _maintenance_sheet():
    book = _client_lazy().open_by_key(settings.maintenance_sheet_id)
    sheet = book.worksheet(settings.maintenance_sheet_worksheet) if settings.maintenance_sheet_worksheet else book.sheet1
    _ensure_header(sheet, MAINTENANCE_HEADER)
    return sheet


def log_maintenance_completion(completed_date: str, category: str, task_name: str, reporter: str, note: str) -> bool:
    """
    Appends one row for a completion just logged via LINE (see
    app.maintenance.log_completion, called right before this in
    app/webhook_handler.py). No-ops (returns False, logged) if
    MAINTENANCE_SHEET_ID isn't configured -- same "feature is just off"
    convention as app/roster_sync.py's DRIVERS_SHEET_ID check, so this
    isn't required for the rest of the app to work. Deliberately doesn't
    raise on failure, same reasoning as sync_verified_bill/slip: the
    SQLite row already has the real data either way.
    """
    if not settings.maintenance_sheet_id:
        logger.info("MAINTENANCE_SHEET_ID not set -- skipping Sheets sync for this completion")
        return False
    try:
        sheet = _maintenance_sheet()
        logged_at = datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M")
        sheet.append_row([completed_date, category, task_name, reporter, note, logged_at], value_input_option="USER_ENTERED")
        logger.info("synced maintenance completion (%s) to Google Sheets OK", task_name)
        return True
    except Exception:
        logger.exception("failed to sync maintenance completion (%s) to Google Sheets", task_name)
        return False


# --- Supply/parts purchases -- same upsert-by-id shape as bills above ---

SUPPLIES_HEADER = ["purchase_id", "status", "supplier_name", "date", "branch", "total_cost", "source_photos", "created_at", "verified_at", "verified_by"]
SUPPLY_LINE_ITEMS_HEADER = ["purchase_id", "line_item_number", "description", "category", "quantity", "unit", "unit_price", "cost"]


def _supplies_sheet():
    sheet = _client_lazy().open_by_key(settings.supplies_sheet_id).sheet1
    _ensure_header(sheet, SUPPLIES_HEADER)
    return sheet


def _supply_line_items_sheet():
    sheet = _client_lazy().open_by_key(settings.supply_line_items_sheet_id).sheet1
    _ensure_header(sheet, SUPPLY_LINE_ITEMS_HEADER)
    return sheet


def upsert_supply_purchase(purchase: dict) -> None:
    sheet = _supplies_sheet()
    values = [purchase.get(col, "") for col in SUPPLIES_HEADER]
    existing = sheet.find(purchase["purchase_id"], in_column=1)
    if existing is not None:
        sheet.update(
            f"A{existing.row}:{gspread.utils.rowcol_to_a1(existing.row, len(SUPPLIES_HEADER))}",
            [values],
            value_input_option="USER_ENTERED",
        )
    else:
        sheet.append_row(values, value_input_option="USER_ENTERED")


def replace_supply_line_items(purchase_id: str, line_items: list[dict]) -> None:
    sheet = _supply_line_items_sheet()
    existing_cells = sheet.findall(purchase_id, in_column=1)
    if existing_cells:
        for cell in sorted(existing_cells, key=lambda c: c.row, reverse=True):
            sheet.delete_rows(cell.row)
    if not line_items:
        return
    rows = [[purchase_id] + [item.get(col, "") for col in SUPPLY_LINE_ITEMS_HEADER[1:]] for item in line_items]
    sheet.append_rows(rows, value_input_option="USER_ENTERED")


def sync_verified_supply_purchase(purchase: dict) -> bool:
    """Writes one verified supply purchase + its line items to the real
    Sheets. Same reasoning as sync_verified_bill: doesn't raise on
    failure, SQLite stays the source of truth either way."""
    try:
        upsert_supply_purchase(purchase)
        replace_supply_line_items(purchase["purchase_id"], purchase.get("line_items") or [])
        logger.info("synced supply purchase %s to Google Sheets OK", purchase.get("purchase_id"))
        return True
    except Exception:
        logger.exception("failed to sync supply purchase %s to Google Sheets", purchase.get("purchase_id"))
        return False

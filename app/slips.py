"""
Slip business logic -- mirrors bills.py's shape (short-lived connections
per call, plain dicts, no ORM), but simpler: a slip is one transaction, not
an itemized document, so there's no line-items table and no multi-page
chaining.
"""
import random
import string
from datetime import datetime, timezone
from typing import Optional

from app.db import get_conn

SLIP_FIELDS = (
    "transaction_date", "transaction_time",
    "from_display_name", "from_account_digits", "from_bank",
    "to_display_name", "to_account_digits", "to_bank",
    "amount", "purpose_note", "reference_number",
    "branch", "account_used_label", "account_match_warning", "cross_branch_note",
    "pl_category",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_slip_id() -> str:
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"SL-{date_part}-{suffix}"


def create_slip(reporter: str, extracted: dict, source_photo: str) -> str:
    """Creates a new slip row from one extract_slip() result. Returns the
    new slip_id. pl_category starts out as the extractor's suggestion
    (slip_extraction.DEFAULT_CATEGORY if nothing matched) -- the reviewer
    can always change it before confirming."""
    from app.slip_extraction import DEFAULT_CATEGORY

    slip_id = _new_slip_id()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO slips (
                slip_id, status, reporter, transaction_date, transaction_time,
                from_display_name, from_account_digits, from_bank,
                to_display_name, to_account_digits, to_bank,
                amount, purpose_note, reference_number,
                branch, account_used_label, account_match_warning, cross_branch_note,
                pl_category, source_photo, created_at
            ) VALUES (?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                slip_id, reporter,
                extracted.get("transaction_date", ""), extracted.get("transaction_time", ""),
                extracted.get("from_display_name", ""), extracted.get("from_account_digits", ""),
                extracted.get("from_bank", ""),
                extracted.get("to_display_name", ""), extracted.get("to_account_digits", ""),
                extracted.get("to_bank", ""),
                extracted.get("amount", 0), extracted.get("purpose_note", ""),
                extracted.get("reference_number", ""),
                extracted.get("branch", ""), extracted.get("account_used_label", ""),
                extracted.get("account_match_warning", ""), extracted.get("cross_branch_note", ""),
                extracted.get("pl_category_suggestion") or DEFAULT_CATEGORY,
                source_photo, _utc_now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return slip_id


def get_slip(slip_id: str) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM slips WHERE slip_id = ?", (slip_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def get_all_slips() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM slips ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def cancel_slip(slip_id: str) -> bool:
    """Permanently deletes a slip -- same reasoning and same
    pending_review-only restriction as bills.cancel_bill (no line items
    table for slips -- always a single transaction, so nothing else to
    cascade)."""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM slips WHERE slip_id = ? AND status = 'pending_review'", (slip_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_reviewed_slip(slip_id: str, fields: dict, verified_by: str) -> None:
    """Applies a manager's edits from the review webpage and marks the
    slip verified. Writing the confirmed version to the real Transaction
    Log sheet is a separate step (app/sheets_client.py) -- see
    app/slips_routes.py."""
    conn = get_conn()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE slips SET {set_clause}, status = 'verified', verified_at = ?, verified_by = ? "
            f"WHERE slip_id = ?",
            (*fields.values(), _utc_now_iso(), verified_by, slip_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_sheet_row(slip_id: str, sheet_row: int) -> None:
    """Records which Transaction Log row this slip was written to, so a
    future re-confirm (after editing an already-verified slip) updates
    that same row instead of appending a duplicate. See
    sheets_client.sync_verified_slip."""
    conn = get_conn()
    try:
        conn.execute("UPDATE slips SET sheet_row = ? WHERE slip_id = ?", (sheet_row, slip_id))
        conn.commit()
    finally:
        conn.close()

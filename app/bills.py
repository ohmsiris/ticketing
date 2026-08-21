"""
Bill + bill_line_items business logic -- mirrors tickets.py's shape
(short-lived connections per call, plain dicts, no ORM).

Handles the STREAMING version of multi-page bill detection. The OCR
testing project's batch linker (bill_linker.py) saw every photo for a
batch upfront; here, photos arrive one at a time via LINE, so an
incomplete bill waits in the DB as an "open chain" (continues_next_page
= 1) until either its next page arrives or the chain goes stale.
"""
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db import get_conn

# An open chain older than this is treated as abandoned rather than
# continued -- if it never gets a next page, it just sits reviewable
# as-is in the /bills list; a manager can correct it by hand there.
OPEN_CHAIN_TIMEOUT_MINUTES = 30

# Same tolerance as the OCR project's bill_linker.py: the combined
# line-item costs adding up to the final page's total is a much stronger
# same-bill signal than date or plate, which get misread more often.
TOTAL_TOLERANCE_FLOOR = 50  # baht
TOTAL_TOLERANCE_FRACTION = 0.10  # plus up to 10% of the final page's total

BILL_FIELDS = (
    "shop_name", "date", "branch", "vehicle_license", "vehicle_license_province",
    "vehicle_number", "mileage", "next_service_mileage",
)


def totals_are_close_enough(combined_sum: float, final_total: float) -> bool:
    tolerance = max(TOTAL_TOLERANCE_FLOOR, TOTAL_TOLERANCE_FRACTION * final_total)
    return abs(combined_sum - final_total) <= tolerance


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_bill_id() -> str:
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"B-{date_part}-{suffix}"


def _unit_price(item: dict) -> str:
    try:
        quantity = float(item.get("quantity") or 0)
        cost = float(item.get("cost") or 0)
    except (TypeError, ValueError):
        return ""
    if not quantity:
        return ""
    return str(round(cost / quantity, 2))


def _insert_line_items(conn, bill_id: str, items: list[dict], start: int = 1) -> None:
    for i, item in enumerate(items, start=start):
        conn.execute(
            "INSERT INTO bill_line_items "
            "(bill_id, line_item_number, description, category, quantity, unit, unit_price, cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                bill_id, i, item.get("description", ""), item.get("category", ""),
                item.get("quantity", ""), item.get("unit", ""),
                item.get("unit_price") if "unit_price" in item else _unit_price(item),
                item.get("cost", ""),
            ),
        )


def find_open_chain(reporter: str) -> Optional[dict]:
    """Most recent still-open (awaiting next page) bill for this
    reporter, if one exists and isn't too stale to plausibly be
    continued by a photo arriving right now."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM bills WHERE reporter = ? AND continues_next_page = 1 "
            "AND status = 'pending_review' ORDER BY created_at DESC LIMIT 1",
            (reporter,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    bill = dict(row)
    created = datetime.fromisoformat(bill["created_at"])
    if datetime.now(timezone.utc) - created > timedelta(minutes=OPEN_CHAIN_TIMEOUT_MINUTES):
        return None
    return bill


def create_bill(reporter: str, source_type: str, extracted: dict, source_photo_id: str) -> str:
    """Creates a new bill row + its line items from one extract_bill()
    result. Returns the new bill_id."""
    bill_id = _new_bill_id()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO bills (
                bill_id, status, source_type, reporter, shop_name, date, branch,
                vehicle_license, vehicle_license_province, vehicle_number,
                vehicle_match_warning, mileage, next_service_mileage,
                total_cost, source_photos, continues_next_page, created_at
            ) VALUES (?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bill_id, source_type, reporter,
                extracted.get("shop_name", ""), extracted.get("date", ""), extracted.get("branch", ""),
                extracted.get("vehicle_license", ""), extracted.get("vehicle_license_province", ""),
                extracted.get("vehicle_number", ""), extracted.get("vehicle_match_warning", ""),
                extracted.get("mileage", ""), extracted.get("next_service_mileage", ""),
                extracted.get("total_cost", ""), source_photo_id,
                1 if extracted.get("continues_next_page") else 0,
                _utc_now_iso(),
            ),
        )
        _insert_line_items(conn, bill_id, extracted.get("line_items") or [])
        conn.commit()
    finally:
        conn.close()
    return bill_id


def append_page_to_chain(bill_id: str, extracted: dict, source_photo_id: str) -> dict:
    """Merges a newly-arrived page into an existing open-chain bill:
    appends its line items, fills in any bill-level field the chain
    didn't have yet, and updates the total/continuation flags.

    Returns {"bill": <updated row>, "totals_reconciled": bool}.
    totals_reconciled is informational, not a gate -- same "still merge,
    but flag it" spirit as the OCR project's batch linker. Only checked
    once a total actually shows up (the final page).
    """
    conn = get_conn()
    try:
        bill = dict(conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,)).fetchone())
        existing_items = conn.execute(
            "SELECT * FROM bill_line_items WHERE bill_id = ? ORDER BY line_item_number", (bill_id,)
        ).fetchall()

        updates = {}
        for field in BILL_FIELDS:
            if not (bill.get(field) or "").strip() and (extracted.get(field) or "").strip():
                updates[field] = extracted[field]

        combined_sum = sum(float(item["cost"] or 0) for item in existing_items) + sum(
            float(item.get("cost") or 0) for item in (extracted.get("line_items") or [])
        )
        final_total = None
        totals_reconciled = True
        if extracted.get("has_total_this_page"):
            final_total = extracted.get("total_cost") or 0
            totals_reconciled = totals_are_close_enough(combined_sum, final_total)
            updates["total_cost"] = final_total

        updates["continues_next_page"] = 1 if extracted.get("continues_next_page") else 0
        updates["source_photos"] = f"{bill.get('source_photos') or ''},{source_photo_id}".lstrip(",")

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE bills SET {set_clause} WHERE bill_id = ?", (*updates.values(), bill_id))

        next_number = len(existing_items) + 1
        _insert_line_items(conn, bill_id, extracted.get("line_items") or [], start=next_number)
        conn.commit()

        updated = dict(conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,)).fetchone())
    finally:
        conn.close()
    return {
        "bill": updated,
        "totals_reconciled": totals_reconciled,
        "combined_sum": combined_sum,
        "final_total": final_total,
    }


def get_bill(bill_id: str) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,)).fetchone()
        if row is None:
            return None
        bill = dict(row)
        items = conn.execute(
            "SELECT * FROM bill_line_items WHERE bill_id = ? ORDER BY line_item_number", (bill_id,)
        ).fetchall()
        bill["line_items"] = [dict(i) for i in items]
        return bill
    finally:
        conn.close()


def get_all_bills() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM bills ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_reviewed_bill(bill_id: str, fields: dict, line_items: list[dict], verified_by: str) -> None:
    """Applies a manager's edits from the review webpage and marks the
    bill verified. Writing the confirmed version to the real Google
    Sheet is a separate step (app/sheets_client.py) -- see app/bills_routes.py."""
    conn = get_conn()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE bills SET {set_clause}, status = 'verified', verified_at = ?, verified_by = ? "
            f"WHERE bill_id = ?",
            (*fields.values(), _utc_now_iso(), verified_by, bill_id),
        )
        conn.execute("DELETE FROM bill_line_items WHERE bill_id = ?", (bill_id,))
        _insert_line_items(conn, bill_id, line_items)
        conn.commit()
    finally:
        conn.close()

"""
Supply-purchase + supply_purchase_items business logic -- mirrors
bills.py's shape (short-lived connections per call, plain dicts, no ORM,
same streaming multi-page-chain handling), for a different document type:
parts/supplies bought from a supplier, not a vehicle repair bill.
"""
import random
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.db import get_conn

# Plain data file, not a Python module -- app/parts_catalog_sync.py's
# scheduled job overwrites this daily from the user's real parts-catalog
# Google Sheet (see that module's docstring), same convention as
# app/vehicle_roster.csv for app/roster_sync.py. One canonical_part-shaped
# string per line. Missing/empty file just means an empty seed -- not an
# error, since PARTS_CATALOG_SHEET_ID (and therefore this file) is optional.
KNOWN_PARTS_SEED_PATH = Path(__file__).resolve().parent / "known_parts_seed.txt"


def _load_seed_known_parts() -> list[str]:
    if not KNOWN_PARTS_SEED_PATH.exists():
        return []
    with open(KNOWN_PARTS_SEED_PATH, encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]

# Same reasoning and same value as bills.py's OPEN_CHAIN_TIMEOUT_MINUTES.
OPEN_CHAIN_TIMEOUT_MINUTES = 30

# Same tolerance as bills.py -- combined line-item costs matching the
# final page's total is a much stronger same-purchase signal than date.
TOTAL_TOLERANCE_FLOOR = 50  # baht
TOTAL_TOLERANCE_FRACTION = 0.10  # plus up to 10% of the final page's total

PURCHASE_FIELDS = ("supplier_name", "date", "branch")

# Cap on how many known canonical_part names get fed back into the next
# extraction call as matching context (see get_known_canonical_parts and
# app/supply_extraction.py) -- keeps the prompt bounded even after years of
# purchases. A small business buying belts/bearings/electrical parts isn't
# going to blow past a few hundred distinct parts; if it ever does, the
# oldest-used names just stop being offered as reuse candidates, which only
# makes matching slightly less complete, never wrong.
MAX_KNOWN_PARTS_IN_PROMPT = 400


def totals_are_close_enough(combined_sum: float, final_total: float) -> bool:
    tolerance = max(TOTAL_TOLERANCE_FLOOR, TOTAL_TOLERANCE_FRACTION * final_total)
    return abs(combined_sum - final_total) <= tolerance


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_purchase_id() -> str:
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"P-{date_part}-{suffix}"


def _unit_price(item: dict) -> str:
    try:
        quantity = float(item.get("quantity") or 0)
        cost = float(item.get("cost") or 0)
    except (TypeError, ValueError):
        return ""
    if not quantity:
        return ""
    return str(round(cost / quantity, 2))


def _insert_line_items(conn, purchase_id: str, items: list[dict], start: int = 1) -> None:
    for i, item in enumerate(items, start=start):
        conn.execute(
            "INSERT INTO supply_purchase_items "
            "(purchase_id, line_item_number, description, category, quantity, unit, unit_price, cost, canonical_part) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                purchase_id, i, item.get("description", ""), item.get("category", ""),
                item.get("quantity", ""), item.get("unit", ""),
                item.get("unit_price") if "unit_price" in item else _unit_price(item),
                item.get("cost", ""),
                # Falls back to description when a caller (e.g. a manually
                # added review-page row with no canonical_part input) doesn't
                # supply one, rather than leaving it blank -- an ungrouped
                # part still shows up under SOME name in the PartPrices sheet.
                item.get("canonical_part") or item.get("description", ""),
            ),
        )


def get_known_canonical_parts() -> list[str]:
    """
    Every distinct canonical_part already in use (real purchases, most-
    recently-used first) UNIONED with the seed list in
    app/known_parts_seed.txt -- extracted from the user's own existing
    "Saraburi Maintenence Sheet" machine-parts catalog, and kept current
    by app/parts_catalog_sync.py's daily auto-refresh job (mirrors
    app/roster_sync.py's role for the vehicle roster) -- capped at
    MAX_KNOWN_PARTS_IN_PROMPT, then alphabetized for a stable/readable
    prompt. Fed into extract_supply_purchase() as matching context -- the
    whole point of canonical_part is grouping the SAME real part across
    different bills for price comparison, and different shops write the
    same part in different languages (one bill in Thai, another in
    English) or different phrasing. Without seeing what's already in use,
    the model has nothing to match against and just invents a fresh name
    every time, silently fragmenting one real part into two rows that
    never get compared. See app/supply_extraction.py's docstring.

    Real purchases take priority over the seed list when both exist and
    the cap would otherwise cut something -- they reflect what's actually
    been bought through the bot, the seed list is just a head start for
    parts that haven't come through yet.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT spi.canonical_part AS canonical_part, MAX(sp.created_at) AS last_used
            FROM supply_purchase_items spi
            JOIN supply_purchases sp ON sp.purchase_id = spi.purchase_id
            WHERE spi.canonical_part IS NOT NULL AND spi.canonical_part != ''
            GROUP BY spi.canonical_part
            ORDER BY last_used DESC
            """
        ).fetchall()
    finally:
        conn.close()
    from_db = [row["canonical_part"] for row in rows]
    seen = set(from_db)
    combined = from_db + [p for p in _load_seed_known_parts() if p not in seen]
    return sorted(combined[:MAX_KNOWN_PARTS_IN_PROMPT])


def find_open_chain(reporter: str) -> Optional[dict]:
    """Most recent still-open (awaiting next page) purchase for this
    reporter, if one exists and isn't too stale to plausibly be
    continued by a photo arriving right now."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM supply_purchases WHERE reporter = ? AND continues_next_page = 1 "
            "AND status = 'pending_review' ORDER BY created_at DESC LIMIT 1",
            (reporter,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    purchase = dict(row)
    created = datetime.fromisoformat(purchase["created_at"])
    if datetime.now(timezone.utc) - created > timedelta(minutes=OPEN_CHAIN_TIMEOUT_MINUTES):
        return None
    return purchase


def create_purchase(reporter: str, extracted: dict, source_photo_id: str) -> str:
    """Creates a new supply_purchases row + its line items from one
    extract_supply_purchase() result. Returns the new purchase_id."""
    purchase_id = _new_purchase_id()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO supply_purchases (
                purchase_id, status, reporter, supplier_name, date, branch,
                total_cost, source_photos, continues_next_page, created_at
            ) VALUES (?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                purchase_id, reporter,
                extracted.get("supplier_name", ""), extracted.get("date", ""), extracted.get("branch", ""),
                extracted.get("total_cost", ""), source_photo_id,
                1 if extracted.get("continues_next_page") else 0,
                _utc_now_iso(),
            ),
        )
        _insert_line_items(conn, purchase_id, extracted.get("line_items") or [])
        conn.commit()
    finally:
        conn.close()
    return purchase_id


def append_page_to_chain(purchase_id: str, extracted: dict, source_photo_id: str) -> dict:
    """Merges a newly-arrived page into an existing open-chain purchase --
    same shape as bills.append_page_to_chain, see that docstring."""
    conn = get_conn()
    try:
        purchase = dict(conn.execute("SELECT * FROM supply_purchases WHERE purchase_id = ?", (purchase_id,)).fetchone())
        existing_items = conn.execute(
            "SELECT * FROM supply_purchase_items WHERE purchase_id = ? ORDER BY line_item_number", (purchase_id,)
        ).fetchall()

        updates = {}
        for field in PURCHASE_FIELDS:
            if not (purchase.get(field) or "").strip() and (extracted.get(field) or "").strip():
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
        updates["source_photos"] = f"{purchase.get('source_photos') or ''},{source_photo_id}".lstrip(",")

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE supply_purchases SET {set_clause} WHERE purchase_id = ?", (*updates.values(), purchase_id))

        next_number = len(existing_items) + 1
        _insert_line_items(conn, purchase_id, extracted.get("line_items") or [], start=next_number)
        conn.commit()

        updated = dict(conn.execute("SELECT * FROM supply_purchases WHERE purchase_id = ?", (purchase_id,)).fetchone())
    finally:
        conn.close()
    return {
        "purchase": updated,
        "totals_reconciled": totals_reconciled,
        "combined_sum": combined_sum,
        "final_total": final_total,
    }


def finalize_chain(purchase_id: str, reporter: str) -> Optional[dict]:
    """Force-closes a still-open chain (continues_next_page=1) without
    waiting for another page to arrive -- for when the sender explicitly
    says there isn't one (see FINALIZE_CHAIN_COMMAND_PREFIX in
    app/webhook_handler.py). Same scoping/safety reasoning as
    bills.finalize_chain -- scoped to purchases this reporter actually
    owns AND still genuinely open, so a stale/duplicate button tap can't
    reopen or re-finalize something already resolved. Returns the
    updated purchase, or None if there was nothing matching left to
    finalize."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT purchase_id FROM supply_purchases WHERE purchase_id = ? AND reporter = ? "
            "AND continues_next_page = 1 AND status = 'pending_review'",
            (purchase_id, reporter),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE supply_purchases SET continues_next_page = 0 WHERE purchase_id = ?", (purchase_id,))
        conn.commit()
    finally:
        conn.close()
    return get_purchase(purchase_id)


def get_purchase(purchase_id: str) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM supply_purchases WHERE purchase_id = ?", (purchase_id,)).fetchone()
        if row is None:
            return None
        purchase = dict(row)
        items = conn.execute(
            "SELECT * FROM supply_purchase_items WHERE purchase_id = ? ORDER BY line_item_number", (purchase_id,)
        ).fetchall()
        purchase["line_items"] = [dict(i) for i in items]
        return purchase
    finally:
        conn.close()


def get_all_purchases() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM supply_purchases ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_reviewed_purchase(purchase_id: str, fields: dict, line_items: list[dict], verified_by: str) -> None:
    """Applies a manager's edits from the review webpage and marks the
    purchase verified. Writing the confirmed version to the real Google
    Sheet is a separate step (app/sheets_client.py) -- see app/supplies_routes.py."""
    conn = get_conn()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE supply_purchases SET {set_clause}, status = 'verified', verified_at = ?, verified_by = ? "
            f"WHERE purchase_id = ?",
            (*fields.values(), _utc_now_iso(), verified_by, purchase_id),
        )
        conn.execute("DELETE FROM supply_purchase_items WHERE purchase_id = ?", (purchase_id,))
        _insert_line_items(conn, purchase_id, line_items)
        conn.commit()
    finally:
        conn.close()

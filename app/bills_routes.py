"""
The manager-only bill review webpage: GET /bills (pending/verified list)
and GET+POST /bills/{bill_id} (the review/edit form, Confirm & Save).

Uses a shared-token gate (?token=...), same style and same caveat as
app/main.py's dashboard routes: not real login, fine for a 2-person
pilot, worth a proper login before a wider rollout.

Templating: uses Jinja2 (a new dependency for this repo -- dashboard.py
deliberately avoided one for its single hand-rolled page, but the bill
review form is a genuinely more complex, already-built-and-tested UI
from the standalone OCR project; porting it as Jinja2 templates was far
less risk than reimplementing that as f-strings).
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import bills, roster_sync, sheets_client
from app.bill_extraction import CATEGORIES, get_roster_rows
from app.config import settings

logger = logging.getLogger("ticketing.bills_routes")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

BILL_FORM_FIELDS = (
    "shop_name", "date", "branch", "vehicle_license", "vehicle_license_province",
    "vehicle_number", "mileage", "next_service_mileage", "total_cost",
)


def _token_ok(token: str) -> bool:
    if not settings.review_token:
        logger.warning("REVIEW_TOKEN not set -- bill review pages are publicly viewable with no token required")
        return True
    return token == settings.review_token


def _forbidden() -> HTMLResponse:
    return HTMLResponse("Forbidden -- missing or incorrect ?token=", status_code=403)


@router.get("/bills", response_class=HTMLResponse)
def bills_list(request: Request, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    return templates.TemplateResponse(
        request, "index.html", {"bills": bills.get_all_bills(), "token": token}
    )


@router.get("/bills/{bill_id}", response_class=HTMLResponse)
def bill_review(request: Request, bill_id: str, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    bill = bills.get_bill(bill_id)
    if bill is None:
        return HTMLResponse("ไม่พบบิลนี้", status_code=404)
    return templates.TemplateResponse(
        request,
        "review.html",
        {"bill": bill, "categories": CATEGORIES, "token": token, "roster": get_roster_rows()},
    )


@router.post("/bills/{bill_id}")
async def bill_review_submit(request: Request, bill_id: str, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    if bills.get_bill(bill_id) is None:
        return HTMLResponse("ไม่พบบิลนี้", status_code=404)

    form = await request.form()

    fields = {field: form.get(field, "") for field in BILL_FORM_FIELDS}

    row_ids = [r for r in form.get("row_ids", "").split(",") if r]
    line_items = [
        {
            "description": form.get(f"description_{row_id}", ""),
            "category": form.get(f"category_{row_id}", ""),
            "quantity": form.get(f"quantity_{row_id}", ""),
            "unit": form.get(f"unit_{row_id}", ""),
            "unit_price": form.get(f"unit_price_{row_id}", ""),
            "cost": form.get(f"cost_{row_id}", ""),
        }
        for row_id in row_ids
    ]

    # TODO once more than one manager exists: derive this from whoever's
    # actually logged in, rather than hardcoding the one pilot manager.
    bills.save_reviewed_bill(bill_id, fields, line_items, verified_by="ohm")

    verified_bill = bills.get_bill(bill_id)
    if not sheets_client.sync_verified_bill(verified_bill):
        logger.error("bill %s saved locally but Google Sheets sync failed -- needs manual attention", bill_id)

    return RedirectResponse(url=f"/bills?token={token}", status_code=303)


@router.post("/bills/{bill_id}/cancel")
def bill_cancel(bill_id: str, token: str = ""):
    """Permanently deletes a mistaken/duplicate pending bill -- see
    bills.cancel_bill. The review page's own confirm() dialog is the
    only guard against a misclick; this route itself doesn't ask twice.
    A no-op (redirects the same either way) if the bill is already
    verified or doesn't exist."""
    if not _token_ok(token):
        return _forbidden()
    bills.cancel_bill(bill_id)
    return RedirectResponse(url=f"/bills?token={token}", status_code=303)


@router.get("/admin/roster-refresh", response_class=HTMLResponse)
def roster_refresh_now(token: str = ""):
    """Manually triggers the same roster pull the 03:00 scheduled job runs
    (app/jobs.py's roster_refresh), so DRIVERS_SHEET_ID + the Drivers-sheet
    share can be tested right after setting them up, instead of waiting
    for the next scheduled run. Same token gate as the rest of this
    router -- this reads/writes a local file, not tickets or bills, but
    it's still an internal tool, not something to leave open."""
    if not _token_ok(token):
        return _forbidden()
    if not settings.drivers_sheet_id:
        return HTMLResponse("DRIVERS_SHEET_ID isn't set -- nothing to refresh from. See README.", status_code=200)
    try:
        updated = roster_sync.refresh_roster()
    except Exception as exc:  # refresh_roster() itself shouldn't raise, but don't 500 if something unexpected slips through
        logger.exception("manual roster refresh failed unexpectedly")
        return HTMLResponse(f"Failed: {exc}", status_code=500)

    rows = roster_sync._load_current_rows()
    branches = sorted({r.get("branch", "") for r in rows if r.get("branch")})
    if updated:
        msg = f"Roster updated: now {len(rows)} rows across branches {branches}."
    else:
        msg = (
            f"No update written (either the sheet's data hasn't changed, or the pull looked "
            f"suspicious and was rejected -- check Railway logs for 'roster_sync'). "
            f"Current file still has {len(rows)} rows across branches {branches}."
        )
    return HTMLResponse(msg)

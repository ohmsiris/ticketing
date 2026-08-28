"""
The manager-only slip review webpage: GET /slips (pending/verified list)
and GET+POST /slips/{slip_id} (the review/edit form, Confirm & Save).
Mirrors app/bills_routes.py exactly -- same shared-token gate, same
Jinja2 templating, same "review page is the only path that ever writes
to the real Sheet" rule.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import sheets_client, slips
from app.config import settings
from app.slip_extraction import CATEGORIES, get_roster_rows

logger = logging.getLogger("ticketing.slips_routes")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

SLIP_FORM_FIELDS = (
    "transaction_date", "transaction_time",
    "from_display_name", "from_account_digits", "from_bank",
    "to_display_name", "to_account_digits", "to_bank",
    "amount", "purpose_note", "reference_number",
    "branch", "account_used_label", "pl_category",
)


def _token_ok(token: str) -> bool:
    if not settings.review_token:
        logger.warning("REVIEW_TOKEN not set -- slip review pages are publicly viewable with no token required")
        return True
    return token == settings.review_token


def _forbidden() -> HTMLResponse:
    return HTMLResponse("Forbidden -- missing or incorrect ?token=", status_code=403)


@router.get("/slips", response_class=HTMLResponse)
def slips_list(request: Request, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    return templates.TemplateResponse(
        request, "slips_index.html", {"slips": slips.get_all_slips(), "token": token}
    )


@router.get("/slips/{slip_id}", response_class=HTMLResponse)
def slip_review(request: Request, slip_id: str, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    slip = slips.get_slip(slip_id)
    if slip is None:
        return HTMLResponse("ไม่พบสลิปนี้", status_code=404)
    return templates.TemplateResponse(
        request,
        "slips_review.html",
        {"slip": slip, "categories": CATEGORIES, "token": token, "accounts": get_roster_rows()},
    )


@router.post("/slips/{slip_id}")
async def slip_review_submit(request: Request, slip_id: str, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    existing = slips.get_slip(slip_id)
    if existing is None:
        return HTMLResponse("ไม่พบสลิปนี้", status_code=404)

    form = await request.form()
    fields = {field: form.get(field, "") for field in SLIP_FORM_FIELDS}

    # TODO once more than one manager exists: derive this from whoever's
    # actually logged in, rather than hardcoding the one pilot manager --
    # same TODO app/bills_routes.py already carries for the same reason.
    slips.save_reviewed_slip(slip_id, fields, verified_by="ohm")

    verified_slip = slips.get_slip(slip_id)
    sheet_row = sheets_client.sync_verified_slip(verified_slip)
    if sheet_row is not None:
        slips.set_sheet_row(slip_id, sheet_row)
    else:
        logger.error("slip %s saved locally but Transaction Log sync failed -- needs manual attention", slip_id)

    return RedirectResponse(url=f"/slips?token={token}", status_code=303)


@router.post("/slips/{slip_id}/cancel")
def slip_cancel(slip_id: str, token: str = ""):
    """Permanently deletes a mistaken/duplicate pending slip -- see
    slips.cancel_slip. Same reasoning as bills_routes.bill_cancel: the
    review page's own confirm() dialog is the only guard, this route
    doesn't ask twice, and it's a no-op if already verified or missing."""
    if not _token_ok(token):
        return _forbidden()
    slips.cancel_slip(slip_id)
    return RedirectResponse(url=f"/slips?token={token}", status_code=303)

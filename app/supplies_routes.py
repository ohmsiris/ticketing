"""
The manager-only supply-purchase review webpage: GET /supplies
(pending/verified list) and GET+POST /supplies/{purchase_id} (the
review/edit form, Confirm & Save). Mirrors app/bills_routes.py's shape --
same token gate, same caveat -- minus the vehicle-roster matching that
doesn't apply here (a parts purchase isn't tied to one vehicle).
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import sheets_client, supplies
from app.config import settings
from app.supply_extraction import CATEGORIES

logger = logging.getLogger("ticketing.supplies_routes")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

PURCHASE_FORM_FIELDS = ("supplier_name", "date", "branch", "total_cost")


def _token_ok(token: str) -> bool:
    if not settings.review_token:
        logger.warning("REVIEW_TOKEN not set -- supply purchase review pages are publicly viewable with no token required")
        return True
    return token == settings.review_token


def _forbidden() -> HTMLResponse:
    return HTMLResponse("Forbidden -- missing or incorrect ?token=", status_code=403)


@router.get("/supplies", response_class=HTMLResponse)
def supplies_list(request: Request, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    return templates.TemplateResponse(
        request, "supplies_index.html", {"purchases": supplies.get_all_purchases(), "token": token}
    )


@router.get("/supplies/{purchase_id}", response_class=HTMLResponse)
def supply_purchase_review(request: Request, purchase_id: str, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    purchase = supplies.get_purchase(purchase_id)
    if purchase is None:
        return HTMLResponse("ไม่พบรายการนี้", status_code=404)
    return templates.TemplateResponse(
        request, "supplies_review.html", {"purchase": purchase, "categories": CATEGORIES, "token": token}
    )


@router.post("/supplies/{purchase_id}")
async def supply_purchase_review_submit(request: Request, purchase_id: str, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    if supplies.get_purchase(purchase_id) is None:
        return HTMLResponse("ไม่พบรายการนี้", status_code=404)

    form = await request.form()

    fields = {field: form.get(field, "") for field in PURCHASE_FORM_FIELDS}

    row_ids = [r for r in form.get("row_ids", "").split(",") if r]
    line_items = [
        {
            "description": form.get(f"description_{row_id}", ""),
            "canonical_part": form.get(f"canonical_part_{row_id}", ""),
            "category": form.get(f"category_{row_id}", ""),
            "quantity": form.get(f"quantity_{row_id}", ""),
            "unit": form.get(f"unit_{row_id}", ""),
            "unit_price": form.get(f"unit_price_{row_id}", ""),
            "cost": form.get(f"cost_{row_id}", ""),
        }
        for row_id in row_ids
    ]

    # TODO once more than one manager exists: derive this from whoever's
    # actually logged in, rather than hardcoding the one pilot manager --
    # same TODO as bills_routes.py's identical line.
    supplies.save_reviewed_purchase(purchase_id, fields, line_items, verified_by="ohm")

    verified_purchase = supplies.get_purchase(purchase_id)
    if not sheets_client.sync_verified_supply_purchase(verified_purchase):
        logger.error("supply purchase %s saved locally but Google Sheets sync failed -- needs manual attention", purchase_id)

    return RedirectResponse(url=f"/supplies?token={token}", status_code=303)


@router.post("/supplies/{purchase_id}/cancel")
def supply_purchase_cancel(purchase_id: str, token: str = ""):
    """Permanently deletes a mistaken/duplicate pending purchase -- see
    supplies.cancel_purchase. Same reasoning as bills_routes.bill_cancel:
    the review page's own confirm() dialog is the only guard, this route
    doesn't ask twice, and it's a no-op if already verified or missing."""
    if not _token_ok(token):
        return _forbidden()
    supplies.cancel_purchase(purchase_id)
    return RedirectResponse(url=f"/supplies?token={token}", status_code=303)

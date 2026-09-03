"""
The manager-only ticket management webpage: GET /tickets (every ticket,
open + closed, with edit links), GET /tickets.csv (same data as CSV --
the "Ticket Sync" Google Apps Script the user runs pulls this), and
GET+POST /tickets/{ticket_id} (an edit form: summary/department/due
date/reminder lead-time, plus open<->closed as a status toggle) and
POST /tickets/{ticket_id}/cancel (void the ticket -- a separate action
from closing, see tickets.admin_cancel_ticket).

Same shared-token gate as the rest of this app's internal pages, but
reuses DASHBOARD_TOKEN specifically (not REVIEW_TOKEN) -- this route
already existed under that token before this file did; changing it here
would silently break whatever link the user already has saved/shared.

Replaces the old hand-rolled read-only page in app/dashboard.py (kept
only for render_tickets_csv, still used by the CSV route below) -- once
edits are possible this needed the same Jinja2 review-form treatment as
bills/slips/supplies, not a plain read-only table.

Editing here is deliberately NOT reporter/department-scoped the way the
LINE-text close/cancel flow is (see tickets.py's admin_* functions) --
the page itself is already gated to the one manager, who should be able
to touch anything from it. Every save that changes something pushes a
LINE notice to the ticket's ORIGINAL REPORTER (skipped when the manager
IS the reporter -- no point notifying yourself of your own edit), so
"the line notice updates from this list too" holds regardless of which
field changed.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import tickets
from app.classifier import KNOWN_DEPARTMENTS
from app.config import settings
from app.dashboard import render_tickets_csv
from app.line_client import push_message
from app import strings

logger = logging.getLogger("ticketing.tickets_routes")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Stable display order for the department dropdown -- KNOWN_DEPARTMENTS
# itself is a set, which has no defined iteration order.
DEPARTMENTS = ["รถ", "เครื่องจักร", "พนักงาน", "อื่นๆ"]
assert set(DEPARTMENTS) == KNOWN_DEPARTMENTS, "DEPARTMENTS list drifted from classifier.KNOWN_DEPARTMENTS"


def _token_ok(token: str) -> bool:
    if not settings.dashboard_token:
        logger.warning("DASHBOARD_TOKEN not set -- ticket pages are publicly viewable with no token required")
        return True
    return token == settings.dashboard_token


def _forbidden() -> HTMLResponse:
    return HTMLResponse("Forbidden -- missing or incorrect ?token=", status_code=403)


def _line_user_id_for(reporter: str) -> str:
    return settings.ohm_line_user_id if reporter == "ohm" else settings.mom_line_user_id


def _notify_reporter(ticket: dict, text: str) -> None:
    """Pushes a LINE notice to the ticket's original reporter -- skipped
    when that's the manager themselves (they're the one making this edit,
    on the page, right now)."""
    reporter = ticket["reporter"]
    if reporter == "ohm":
        return
    push_message(_line_user_id_for(reporter), [text])


@router.get("/tickets", response_class=HTMLResponse)
def tickets_list(request: Request, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    return templates.TemplateResponse(request, "tickets_index.html", {"tickets": tickets.get_all_tickets(), "token": token})


@router.get("/tickets.csv")
def tickets_csv(token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    # UTF-8 BOM up front so Excel/Google Sheets' IMPORTDATA (and the
    # user's own "Ticket Sync" Apps Script, which fetches this same URL)
    # correctly detect the encoding instead of mangling the Thai text.
    body = "﻿" + render_tickets_csv(tickets.get_all_tickets())
    return Response(content=body, media_type="text/csv; charset=utf-8")


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_edit(request: Request, ticket_id: int, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    ticket = tickets.get_ticket(ticket_id)
    if ticket is None:
        return HTMLResponse("ไม่พบทิกเก็ตนี้", status_code=404)
    return templates.TemplateResponse(
        request, "tickets_edit.html", {"ticket": ticket, "departments": DEPARTMENTS, "token": token}
    )


@router.post("/tickets/{ticket_id}")
async def ticket_edit_submit(request: Request, ticket_id: int, token: str = ""):
    if not _token_ok(token):
        return _forbidden()
    ticket = tickets.get_ticket(ticket_id)
    if ticket is None:
        return HTMLResponse("ไม่พบทิกเก็ตนี้", status_code=404)

    form = await request.form()
    summary = (form.get("summary") or "").strip()
    department = (form.get("department") or "").strip()
    due_date = (form.get("due_date") or "").strip()
    remind_raw = (form.get("remind_days_before") or "").strip()
    new_status = (form.get("status") or "open").strip()

    fields = {
        "summary": summary or None,
        "department": department if department in KNOWN_DEPARTMENTS else ticket["department"],
        "due_date": due_date or None,
        "remind_days_before": int(remind_raw) if remind_raw.isdigit() else None,
    }
    tickets.admin_update_ticket(ticket_id, fields)

    # Status is a separate explicit action from the field edits above, so
    # the notification below can describe what actually happened (closed/
    # reopened/just-edited) instead of guessing from a field diff.
    display_text = summary or ticket["summary"] or ticket["message"]
    if ticket["status"] == "open" and new_status == "closed":
        tickets.admin_close_ticket(ticket_id)
        _notify_reporter(ticket, strings.ticket_closed_by_admin(ticket_id, display_text))
    elif ticket["status"] == "closed" and new_status == "open":
        tickets.admin_reopen_ticket(ticket_id)
        _notify_reporter(ticket, strings.ticket_reopened_by_admin(ticket_id, display_text))
    else:
        _notify_reporter(ticket, strings.ticket_updated_by_admin(ticket_id, display_text))

    return RedirectResponse(url=f"/tickets?token={token}", status_code=303)


@router.post("/tickets/{ticket_id}/cancel")
def ticket_cancel(ticket_id: int, token: str = ""):
    """Voids the ticket -- see tickets.admin_cancel_ticket's docstring for
    why this is a soft cancel, not a delete. The page's own confirm()
    dialog is the only guard against a misclick; this route doesn't ask
    twice. A no-op (still redirects) if the ticket's already closed/
    cancelled or doesn't exist."""
    if not _token_ok(token):
        return _forbidden()
    ticket = tickets.get_ticket(ticket_id)
    if ticket is not None:
        cancelled = tickets.admin_cancel_ticket(ticket_id)
        if cancelled is not None:
            display_text = ticket["summary"] or ticket["message"]
            _notify_reporter(ticket, strings.ticket_cancelled_by_admin(ticket_id, display_text))
    return RedirectResponse(url=f"/tickets?token={token}", status_code=303)

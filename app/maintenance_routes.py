"""
GET /maintenance.csv -- every logged PM completion, ever, as CSV. One-time
backfill tool: MAINTENANCE_SHEET_ID (see app/sheets_client.py) went live
without ever being configured, so nothing synced to a Sheet before now --
this route exists to pull the full history out once so it can be pasted/
imported into whatever Sheet ends up as MAINTENANCE_SHEET_ID going forward.
Not polled by anything the way tickets.csv is (that one feeds the user's
own Apps Script + Sheets' =IMPORTDATA()); this is a manual pull.

Same shared-token gate as the rest of this app's internal pages, reusing
DASHBOARD_TOKEN (not a new token) -- same read-only "internal admin data"
trust level as /tickets.csv.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from app import maintenance
from app.config import settings
from app.dashboard import render_maintenance_csv

logger = logging.getLogger("ticketing.maintenance_routes")

router = APIRouter()


def _token_ok(token: str) -> bool:
    if not settings.dashboard_token:
        logger.warning("DASHBOARD_TOKEN not set -- maintenance.csv is publicly viewable with no token required")
        return True
    return token == settings.dashboard_token


@router.get("/maintenance.csv")
def maintenance_csv(token: str = ""):
    if not _token_ok(token):
        return HTMLResponse("Forbidden -- missing or incorrect ?token=", status_code=403)
    # UTF-8 BOM up front, same reasoning as tickets_routes.tickets_csv --
    # Excel/Sheets need it to read the Thai text correctly.
    body = "﻿" + render_maintenance_csv(maintenance.get_all_completions())
    return Response(content=body, media_type="text/csv; charset=utf-8")

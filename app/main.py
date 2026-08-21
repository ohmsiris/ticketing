"""
FastAPI entry point: the LINE webhook route, a health check, startup wiring
(DB init + scheduler).

Run locally with:
    uvicorn app.main:app --reload --port 8000

Run in production (Railway/Render set $PORT for you):
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import tickets
from app.bills_routes import router as bills_router
from app.config import settings
from app.dashboard import render_tickets_csv, render_tickets_page
from app.db import init_db
from app.jobs import start_scheduler
from app.line_client import verify_signature
from app.webhook_handler import handle_follow_event, handle_message_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ticketing.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Ticketing LINE Bot", lifespan=lifespan)

_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(bills_router)


@app.get("/health")
def health():
    return {"status": "ok"}


def _check_dashboard_token(token: str, route: str) -> Optional[Response]:
    """
    Simple shared-token gate, not real auth -- this is a 2-person pilot with
    no login system. Set DASHBOARD_TOKEN and pass it as ?token=xxxx. If it's
    not set, the route is left open (with a loud log warning) so this
    doesn't block first deploy -- set it before sharing either link around.
    Returns a 403 Response to short-circuit with, or None to proceed.
    """
    if settings.dashboard_token:
        if token != settings.dashboard_token:
            return HTMLResponse("Forbidden -- missing or incorrect ?token=", status_code=403)
        return None
    logger.warning("DASHBOARD_TOKEN not set -- %s is publicly viewable with no token required", route)
    return None


@app.get("/tickets", response_class=HTMLResponse)
def tickets_dashboard(token: str = ""):
    denied = _check_dashboard_token(token, "/tickets")
    if denied is not None:
        return denied
    return render_tickets_page(tickets.get_all_tickets())


@app.get("/tickets.csv")
def tickets_csv(token: str = ""):
    denied = _check_dashboard_token(token, "/tickets.csv")
    if denied is not None:
        return denied
    # UTF-8 BOM up front so Excel/Google Sheets' IMPORTDATA correctly detect
    # the encoding instead of mangling the Thai text.
    body = "\ufeff" + render_tickets_csv(tickets.get_all_tickets())
    return Response(content=body, media_type="text/csv; charset=utf-8")


@app.post("/webhook")
async def webhook(request: Request, x_line_signature: str = Header(default="", alias="X-Line-Signature")):
    body = await request.body()

    if not verify_signature(body, x_line_signature):
        logger.warning("invalid LINE signature -- rejecting webhook request")
        return JSONResponse(status_code=400, content={"error": "invalid signature"})

    payload = await request.json()
    events = payload.get("events", [])

    for event in events:
        try:
            event_type = event.get("type")
            if event_type == "message":
                handle_message_event(event)
            elif event_type == "follow":
                handle_follow_event(event)
            # Other event types (unfollow, join, postback, ...) are out of
            # scope for v0 and are silently ignored.
        except Exception:
            # One bad event shouldn't take down the rest of the batch or
            # cause LINE to keep retrying the whole webhook delivery.
            logger.exception("failed to handle event: %s", event)

    # LINE just needs a 200 back; content is ignored.
    return {"status": "ok"}

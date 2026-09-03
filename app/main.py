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

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import maintenance
from app.bills_routes import router as bills_router
from app.db import init_db
from app.jobs import start_scheduler
from app.line_client import verify_signature
from app.slips_routes import router as slips_router
from app.supplies_routes import router as supplies_router
from app.tickets_routes import router as tickets_router
from app.webhook_handler import handle_follow_event, handle_message_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ticketing.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    maintenance.seed_default_tasks()
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Ticketing LINE Bot", lifespan=lifespan)

_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(bills_router)
app.include_router(slips_router)
app.include_router(supplies_router)
app.include_router(tickets_router)


@app.get("/health")
def health():
    return {"status": "ok"}


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

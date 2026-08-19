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

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.db import init_db
from app.jobs import start_scheduler
from app.line_client import verify_signature
from app.webhook_handler import handle_message_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ticketing.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Ticketing LINE Bot", lifespan=lifespan)


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
            if event.get("type") == "message":
                handle_message_event(event)
            # Other event types (follow, unfollow, join, postback, ...) are
            # out of scope for v0 and are silently ignored.
        except Exception:
            # One bad event shouldn't take down the rest of the batch or
            # cause LINE to keep retrying the whole webhook delivery.
            logger.exception("failed to handle event: %s", event)

    # LINE just needs a 200 back; content is ignored.
    return {"status": "ok"}

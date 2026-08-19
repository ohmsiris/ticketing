"""
Sends each incoming text message to Claude to figure out what the sender
means, instead of trying to pattern-match Thai phrasing ourselves.
"""
import json
import logging
import re
from datetime import datetime
from typing import Optional, TypedDict
from zoneinfo import ZoneInfo

from anthropic import Anthropic

from app.config import TIMEZONE, settings

logger = logging.getLogger("ticketing.classifier")

_client: Optional[Anthropic] = None


def _client_lazy() -> Anthropic:
    # Created lazily so importing this module never requires the API key
    # (useful for tests / running without full env config).
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


MODEL = "claude-sonnet-5"

KNOWN_INTENTS = {"new_ticket", "due_date_reply", "close_ticket", "other"}

SYSTEM_PROMPT = """\
You are a message classifier for a small internal ticket system used by two \
people. Messages will primarily be in Thai, sometimes mixed with English \
technical terms -- classify accordingly.

Read the user's latest message and decide what they mean. Respond with ONLY \
a single JSON object, no markdown fences, no explanation, matching exactly \
this shape:

{
  "intent": "new_ticket" | "due_date_reply" | "close_ticket" | "other",
  "due_date_days": <integer or null>,
  "due_date_calendar": "<YYYY-MM-DD or null>",
  "close_ticket_id": <integer or null>
}

Rules:
- "new_ticket": the message describes a new problem, issue, or task to track.
- "due_date_reply": the message answers a question about a deadline/due \
date, e.g. "อีก 3 วัน", "พรุ่งนี้", "วันศุกร์หน้า", or an explicit date. If \
they gave a relative offset in days, fill due_date_days with that integer \
and leave due_date_calendar null. If they gave (or implied) a specific \
calendar date, resolve it to YYYY-MM-DD using today's date below and fill \
due_date_calendar, leaving due_date_days null.
- "close_ticket": the message says an issue is fixed/done/resolved/closed, \
e.g. "ปิดงานนี้", "เสร็จแล้ว", "closed", "done with #14". If a specific \
ticket number is mentioned, put it (as an integer) in close_ticket_id, \
otherwise leave it null (meaning "the last thing I reported").
- "other": anything that doesn't clearly fit the above (small talk, \
unclear, questions unrelated to tickets).

Today's date is {today} ({tz}). Use it to resolve any relative dates.
"""


class Classification(TypedDict):
    intent: str
    due_date_days: Optional[int]
    due_date_calendar: Optional[str]
    close_ticket_id: Optional[int]


def _default_classification() -> Classification:
    return {
        "intent": "new_ticket",
        "due_date_days": None,
        "due_date_calendar": None,
        "close_ticket_id": None,
    }


def _extract_json(text: str) -> dict:
    # Claude is asked to return raw JSON, but strip code fences defensively
    # in case it wraps the answer in ```json ... ```.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model output")
    return json.loads(match.group(0))


def classify(message: str) -> Classification:
    """
    Classifies a single incoming text message. Never raises -- on any error,
    or if the model returns an intent we don't recognize, this falls back to
    "new_ticket" (better to log something as a ticket than silently drop it).
    """
    today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    system = SYSTEM_PROMPT.format(today=today, tz=TIMEZONE)

    result = _default_classification()
    raw_text = None
    try:
        response = _client_lazy().messages.create(
            model=MODEL,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        raw_text = response.content[0].text
        parsed = _extract_json(raw_text)

        intent = parsed.get("intent")
        if intent not in KNOWN_INTENTS:
            intent = "new_ticket"  # unsure -> log as a ticket, don't drop it

        result = {
            "intent": intent,
            "due_date_days": parsed.get("due_date_days"),
            "due_date_calendar": parsed.get("due_date_calendar"),
            "close_ticket_id": parsed.get("close_ticket_id"),
        }
    except Exception:
        logger.exception("classification failed, defaulting to new_ticket")

    # "other" is not something the rest of the app routes on -- fall back to
    # new_ticket per spec ("default to treating it as new_ticket").
    routed_intent = result["intent"] if result["intent"] != "other" else "new_ticket"
    routed = {**result, "intent": routed_intent}

    # Log the raw message alongside the classification so misclassifications
    # are easy to spot in the platform logs later.
    logger.info(
        json.dumps(
            {
                "raw_message": message,
                "model_output": raw_text,
                "classification": routed,
            },
            ensure_ascii=False,
        )
    )
    return routed

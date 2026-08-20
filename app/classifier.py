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
KNOWN_DEPARTMENTS = {"รถ", "เครื่องจักร", "พนักงาน", "อื่นๆ"}
DEFAULT_DEPARTMENT = "อื่นๆ"

SYSTEM_PROMPT = """\
You are a message classifier for a small internal ticket system used by two \
people. Messages will primarily be in Thai, sometimes mixed with English \
technical terms -- classify accordingly.

Read the user's latest message and decide what they mean. Respond with ONLY \
a single JSON object, no markdown fences, no explanation, matching exactly \
this shape:

{{
  "intent": "new_ticket" | "due_date_reply" | "close_ticket" | "other",
  "department": "รถ" | "เครื่องจักร" | "พนักงาน" | "อื่นๆ",
  "summary": "<cleaned-up short version of the issue, or null>",
  "due_date_days": <integer or null>,
  "due_date_calendar": "<YYYY-MM-DD or null>",
  "close_ticket_id": <integer or null>,
  "close_specific_no_match": <true or false>
}}

Rules:
- "new_ticket": the message describes a new problem, issue, or task to \
track. If it ALSO states a due date/deadline in the same message (e.g. \
"เปลี่ยนน้ำมันเครื่อง 27/9/69"), extract that date too -- fill \
due_date_days or due_date_calendar exactly like you would for a \
due_date_reply, using the date resolution rules below. If no date is \
mentioned, leave both null.
- "due_date_reply": the message answers a question about a deadline/due \
date, e.g. "อีก 3 วัน", "อีก2วัน" (same thing, no spaces), "พรุ่งนี้", \
"วันศุกร์หน้า", or an explicit date. If they gave a relative offset in \
days, fill due_date_days with that integer and leave due_date_calendar \
null. If they gave (or implied) a specific calendar date, resolve it to \
YYYY-MM-DD and fill due_date_calendar, leaving due_date_days null.

Date resolution rules (for a due date mentioned in either a new_ticket or \
a due_date_reply message):
  - Recognize Thai month names in any common form -- full, short, or \
abbreviated with a period, all equivalent: มกราคม/มกรา/ม.ค., \
กุมภาพันธ์/กุมภา/ก.พ., มีนาคม/มีนา/มี.ค., เมษายน/เมษา/เม.ย., \
พฤษภาคม/พฤษภา/พ.ค., มิถุนายน/มิถุนา/มิ.ย., กรกฎาคม/กรกฎา/ก.ค., \
สิงหาคม/สิงหา/ส.ค., กันยายน/กันยา/ก.ย., ตุลาคม/ตุลา/ต.ค., \
พฤศจิกายน/พฤศจิกา/พ.ย., ธันวาคม/ธันวา/ธ.ค. -- as well as plain numeric \
dates like "19/4" or "5/1" (day/month order, Thai convention).
  - If a year is given as 2 digits, or as a 4-digit number 2400 or \
higher, treat it as Buddhist Era (BE) and convert to Gregorian by \
subtracting 543 (e.g. "70" or "2570" -> 2027; "69" or "2569" -> 2026). A \
4-digit year below 2400 is already Gregorian -- use it as-is.
  - If NO year is given at all, pick whichever occurrence is soonest but \
not already past: use the current year if that day/month hasn't happened \
yet this year relative to today (given below); if it has already passed \
this year, use next year instead. Example: if today is 2026-12-21 and \
they say "5/1", that resolves to 2027-01-05 (next year), not 2026-01-05 \
(already passed).
- "close_ticket": the message says an issue is fixed/done/resolved/closed, \
e.g. "ปิดงานนี้", "เสร็จแล้ว", "closed", "done with #14", "ปิด #3", or a \
past-tense completion report like "ถ่ายน้ำมันเครื่องเบอร์ 1 แล้ว" / \
"เปลี่ยนลูกปืนเสร็จเรียบร้อยแล้ว" (no explicit "close" word needed -- \
"แล้ว"/"เสร็จ(เรียบร้อย)แล้ว" on a task description is itself a completion \
report). If a specific ticket number is mentioned, put it (as an integer) \
in close_ticket_id. If no number is given but the message clearly \
describes the content of exactly one ticket from this person's \
open-tickets list (given in context below, if any), put THAT ticket's id \
in close_ticket_id instead -- match by meaning, not exact wording. \
Otherwise leave close_ticket_id null, and set close_specific_no_match: \
  - true if the message described a specific task/item (not just a bare \
"ปิดงาน"/"เสร็จแล้ว") but it does NOT match anything in the open-tickets \
list -- e.g. they said a motor was fixed but nothing about a motor is \
open. This matters even when only one ticket happens to be open: don't \
guess that unrelated ticket is the one they mean.
  - false if the message was generic with nothing to match against at all \
(plain "ปิดงาน"/"เสร็จแล้ว") -- in that case, if only one ticket is open, \
it's fine to assume that's the one.
- "other": anything that doesn't clearly fit the above (small talk, \
unclear, questions unrelated to tickets).
- "department": which category the underlying issue belongs to -- only \
meaningful when intent is "new_ticket" (for other intents, just give your \
best guess or "อื่นๆ", it won't be used):
  - "รถ": vehicles -- cars, motorcycles, trucks: repairs, maintenance, \
oil/fuel, registration, accidents, etc.
  - "เครื่องจักร": machinery/equipment -- factory or work equipment, tools, \
appliances (not vehicles).
  - "พนักงาน": staff/personnel -- anything about an employee or a person's \
behavior/HR-type issue.
  - "อื่นๆ": anything that doesn't clearly fit the three above.
- "summary": only meaningful when intent is "new_ticket" (for other \
intents, leave it null). A clean, short version of the actual issue/task, \
with conversational framing stripped out -- drop lead-ins like "เตือนให้" \
(remind [me] to), "ช่วย...ด้วย" (please help with...), "กรุณา"/"รบกวน" \
(please), "แจ้งว่า" (reporting that) -- and drop the due date itself, \
since that's already shown separately. Keep every concrete detail (what, \
which item/number, location, etc.) exactly as given, just without the \
filler. Example: "เตือนให้เปลี่ยนน้ำมันเครื่องเบอร์ 19 สระบุรี 17-10-69" \
-> "เปลี่ยนน้ำมันเครื่องเบอร์ 19 สระบุรี". If there's nothing to strip, \
just repeat the message as-is.

Today's date is {today} ({tz}). Use it to resolve any relative dates.
"""

# Appended to the system prompt only when the sender has an open ticket
# that's still missing a due date -- i.e. the bot's last message to them was
# almost certainly "when's this due?". Without this, short replies like
# "อีก2วัน" get read cold, with no idea a due-date question was just asked,
# and can get misread as a new ticket instead of an answer.
CONTEXT_AWAITING_DUE_DATE = """

Context: this person has an open ticket that's still missing a due date, \
and the bot's last message to them was asking for one. If this message \
could plausibly be answering that -- a timeframe, a date, "ไม่มี", \
"ไม่แน่ใจ", even a bare number -- classify it as "due_date_reply" rather \
than "new_ticket", even if the phrasing is terse or has no spaces (e.g. \
"อีก2วัน" means "อีก 2 วัน"). Only treat it as a new_ticket if it clearly \
describes a distinct new problem instead.
"""


def _open_tickets_context(open_tickets: Optional[list]) -> str:
    """
    Builds the context block listing a person's currently open tickets, so
    close_ticket can be matched by content ("ปิดงานเปลี่ยนน้ำมัน") instead of
    requiring an exact id every time -- see the "close_ticket" rule above.
    """
    if not open_tickets:
        return "\n\nThis person has no open tickets right now."
    lines = ["\n\nThis person's open tickets (id: description):"]
    for t in open_tickets:
        lines.append(f"#{t['id']}: {t.get('summary') or t['message']}")
    return "\n".join(lines)


class Classification(TypedDict):
    intent: str
    department: str
    summary: Optional[str]
    due_date_days: Optional[int]
    due_date_calendar: Optional[str]
    close_ticket_id: Optional[int]
    close_specific_no_match: bool


def _default_classification() -> Classification:
    return {
        "intent": "new_ticket",
        "department": DEFAULT_DEPARTMENT,
        "summary": None,
        "due_date_days": None,
        "due_date_calendar": None,
        "close_ticket_id": None,
        "close_specific_no_match": False,
    }


def _extract_json(text: str) -> dict:
    # Claude is asked to return raw JSON, but strip code fences defensively
    # in case it wraps the answer in ```json ... ```.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model output")
    return json.loads(match.group(0))


def classify(
    message: str,
    awaiting_due_date: bool = False,
    open_tickets: Optional[list] = None,
) -> Classification:
    """
    Classifies a single incoming text message. Never raises -- on any error,
    or if the model returns an intent we don't recognize, this falls back to
    "new_ticket" (better to log something as a ticket than silently drop it).

    awaiting_due_date: pass True when the sender has an open ticket still
    missing a due date, so the classifier knows a short reply is more likely
    answering that than describing something new (see webhook_handler.py).

    open_tickets: this sender's currently open tickets (list of dicts with
    at least id/message/summary), so a close_ticket message can be matched
    to one by content instead of requiring an exact ticket number.
    """
    today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    system = SYSTEM_PROMPT.format(today=today, tz=TIMEZONE)
    if awaiting_due_date:
        system += CONTEXT_AWAITING_DUE_DATE
    system += _open_tickets_context(open_tickets)

    result = _default_classification()
    raw_text = None
    try:
        response = _client_lazy().messages.create(
            model=MODEL,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": message}],
        )
        # claude-sonnet-5 can return a leading "thinking" content block
        # before the actual answer, so find the text block by type rather
        # than assuming content[0] is it (that assumption silently broke
        # every single classification -- always caught by the except below
        # and defaulted -- until this was caught via live testing).
        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block is None:
            raise ValueError("no text block in model response")
        raw_text = text_block.text
        parsed = _extract_json(raw_text)

        intent = parsed.get("intent")
        if intent not in KNOWN_INTENTS:
            intent = "new_ticket"  # unsure -> log as a ticket, don't drop it

        department = parsed.get("department")
        if department not in KNOWN_DEPARTMENTS:
            department = DEFAULT_DEPARTMENT

        summary = parsed.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = None  # caller falls back to the raw message -- see webhook_handler.py

        result = {
            "intent": intent,
            "department": department,
            "summary": summary,
            "due_date_days": parsed.get("due_date_days"),
            "due_date_calendar": parsed.get("due_date_calendar"),
            "close_ticket_id": parsed.get("close_ticket_id"),
            "close_specific_no_match": bool(parsed.get("close_specific_no_match")),
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
                "awaiting_due_date": awaiting_due_date,
                "open_ticket_ids": [t["id"] for t in (open_tickets or [])],
                "model_output": raw_text,
                "classification": routed,
            },
            ensure_ascii=False,
        )
    )
    return routed

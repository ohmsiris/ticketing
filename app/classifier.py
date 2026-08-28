"""
Sends each incoming text message to Claude to figure out what the sender
means, instead of trying to pattern-match Thai phrasing ourselves.
"""
import json
import logging
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

KNOWN_INTENTS = {"new_ticket", "due_date_reply", "close_ticket", "cancel_ticket", "maintenance_done", "other"}
KNOWN_DEPARTMENTS = {"รถ", "เครื่องจักร", "พนักงาน", "อื่นๆ"}
DEFAULT_DEPARTMENT = "อื่นๆ"

SYSTEM_PROMPT = """\
You are a message classifier for a small internal ticket system used by two \
people. Messages will primarily be in Thai, sometimes mixed with English \
technical terms -- classify accordingly.

You may be given the recent conversation between this person and the bot \
(their messages and the bot's replies, oldest first), followed by their \
newest message to classify now. Use that history the way a person would: \
if the bot's last message asked a question (e.g. "when's this due?"), a \
short or terse-looking reply -- even a bare number, a date with no spaces, \
or just "ไม่มี"/"ไม่แน่ใจ" -- is almost certainly answering THAT, not \
describing something new. If they're correcting, clarifying, or backing \
out of something from a moment ago ("ไม่ใช่", "ยกเลิก", "เอาใหม่"), read it \
against what was actually just said, not in isolation. If there's no \
history, or the newest message clearly stands on its own, just read it \
normally -- don't force a connection that isn't there.

Read the user's latest message and decide what they mean. Respond with ONLY \
a single JSON object, no markdown fences, no explanation, matching exactly \
this shape:

{{
  "intent": "new_ticket" | "due_date_reply" | "close_ticket" | "cancel_ticket" | "maintenance_done" | "other",
  "department": "รถ" | "เครื่องจักร" | "พนักงาน" | "อื่นๆ",
  "summary": "<cleaned-up short version of the issue, or null>",
  "due_date_days": <integer or null>,
  "due_date_calendar": "<YYYY-MM-DD or null>",
  "remind_days_before": <integer or null>,
  "close_ticket_id": <integer or null>,
  "close_specific_no_match": <true or false>,
  "maintenance_task_id": <integer or null>,
  "maintenance_completed_days_ago": <integer or null>,
  "maintenance_completed_calendar": "<YYYY-MM-DD or null>",
  "cancel_ticket_id": <integer or null>,
  "banter_reply": "<short Thai reply, or null>"
}}

Rules:
- "maintenance_done": ONLY when a maintenance task catalog is given in \
context below -- if none is given, never use this intent no matter what \
the message says. The message reports completing one of the ROUTINE, \
SCHEDULED tasks in that catalog (a regular cleaning/oil-change/check that \
recurs on its own cadence) -- e.g. "เพิ่งล้างฟรีซหลอด 2 เสร็จ", "เป่า \
คอนเดนเซอร์ตู้แพ็คให้แล้วครับ". No special trigger word is needed -- match \
by meaning, same as close_ticket. Put the matched task's id in \
maintenance_task_id. If you're not confident which catalog task it means, \
or it doesn't match any of them, do NOT use maintenance_done -- classify \
it as new_ticket or another intent instead, exactly as if no catalog \
existed; there's no picker/disambiguation flow for this one; unlike \
close_ticket, guessing wrong here silently marks a routine task done \
early, which is worse than just not matching. The key distinction from \
new_ticket: a maintenance_done message reports a routine task from the \
catalog being done; new_ticket describes a NEW or unusual problem (broken, \
unexpected, not part of the regular schedule) -- when genuinely unsure \
which, prefer new_ticket (a report never silently vanishes that way). If \
they mention WHEN they actually did it and it wasn't today/just now, use \
EXACTLY ONE of these two fields -- never compute one from the other \
yourself, never do date arithmetic in your head:
  - maintenance_completed_days_ago: for a VAGUE relative time with no \
explicit date -- "เมื่อวาน" (yesterday -> 1), "เมื่อวานซืน" (day before \
yesterday -> 2), "3 วันที่แล้ว" (3 days ago -> 3), "อาทิตย์ที่แล้ว" (about a \
week ago -> 7), "2 เดือนที่แล้ว" (about 2 months ago -> 60). Approximate is \
fine here, that's inherent to how vague the phrasing is.
  - maintenance_completed_calendar: whenever an EXPLICIT calendar date is \
given (a day+month, with or without a year, or a named day like "วันจันทร์\
ที่แล้ว" that has a specific date) -- resolve it to YYYY-MM-DD using the \
exact same date resolution rules given below for due dates (month name \
forms, Buddhist-Era year conversion, no-year-passed-so-use-last-\
occurrence-not-next). Put the result here, NOT in \
maintenance_completed_days_ago -- do not convert it to a day-count \
yourself, the app does that conversion in plain arithmetic afterward. \
Getting this field right only requires reading a date, never counting.
Leave both null if they don't mention timing, or say something meaning \
today/just now ("เมื่อกี้", "เมื่อสักครู่", or no time reference at all).
- "new_ticket": the message describes a new problem, issue, or task to \
track. If it ALSO states a due date/deadline in the same message (e.g. \
"เปลี่ยนน้ำมันเครื่อง 27/9/69"), extract that date too -- fill \
due_date_days or due_date_calendar exactly like you would for a \
due_date_reply, using the date resolution rules below. If no date is \
mentioned, leave both null. It can also mention a heads-up lead time in \
the same breath -- see remind_days_before below.
- "due_date_reply": the message answers a question about a deadline/due \
date, e.g. "อีก 3 วัน", "อีก2วัน" (same thing, no spaces), "พรุ่งนี้", \
"วันศุกร์หน้า", or an explicit date. If they gave a relative offset in \
days, fill due_date_days with that integer and leave due_date_calendar \
null. If they gave (or implied) a specific calendar date, resolve it to \
YYYY-MM-DD and fill due_date_calendar, leaving due_date_days null. A \
message that ONLY sets a reminder lead time (see remind_days_before \
below), with no due date in it at all, is ALSO a due_date_reply -- leave \
due_date_days/due_date_calendar both null in that case, it just means \
"adjust the reminder on my existing ticket, not the due date itself".
- "remind_days_before": an extra heads-up reminder N days before the due \
date, on top of the normal due-date reminder -- e.g. "เตือนก่อน 3 วัน", \
"แจ้งเตือนล่วงหน้า 2 วัน", "เตือนล่วงหน้า5วัน". Put the integer N here. \
This can appear alongside a due date (new_ticket or due_date_reply) or by \
itself (due_date_reply, adjusting an already-set ticket). Leave null if \
nothing about a heads-up/advance reminder is mentioned -- most messages \
won't have this.

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
report). This means the work actually got done -- if instead they're \
withdrawing/undoing the ticket itself, that's cancel_ticket below, not \
this. If a specific ticket number is mentioned, put it (as an integer) \
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
- "cancel_ticket": the message says to cancel, undo, scrap, or void a \
ticket -- NOT the same as close_ticket. close_ticket means the described \
work actually happened; cancel_ticket means the ticket itself shouldn't \
be tracked at all -- it was a mistake, a duplicate, plans changed, or \
they're backing out of something they just reported. Trigger words/ \
phrases: "ยกเลิก", "ไม่เอาแล้ว", "ไม่ใช่", "พิมพ์ผิด", "งดไว้ก่อน", or \
similar -- with no completion language ("เสร็จแล้ว" etc). If a specific \
ticket number is mentioned, put it (as an integer) in cancel_ticket_id, \
otherwise leave it null -- the app resolves which ticket that refers to \
from context (see close_ticket's number/no-number handling above, same \
idea). A bare cancel word with no number NEVER means "cancel everything" \
-- there is no concept of cancelling more than one ticket at once in this \
system, so don't try to enumerate multiple ids even if the message says \
something like "ยกเลิกทั้งหมด" (cancel everything) -- just leave \
cancel_ticket_id null as usual and let the app figure out the one ticket \
that's actually relevant.
- "other": anything that doesn't clearly fit the above (small talk, \
unclear, questions unrelated to tickets).
- "banter_reply": ONLY when intent is "other" (null for every other \
intent). A short reply in Thai actually responding to what they said -- a \
greeting gets a greeting back, "ขอบคุณ" gets a warm "ยินดีครับ", a random \
comment gets a light, natural acknowledgment. Tone: warm and a little \
lighthearted, like a friendly coworker, NOT a jokey/sarcastic comedy bot -- \
this may be read by someone unfamiliar with chatbots, including an older \
family member. One short sentence, occasionally two. After responding to \
what they said, end with a brief, natural nudge back to what you're for, \
e.g. "มีอะไรให้ช่วยแจ้งได้เลยนะครับ" -- vary the phrasing, don't repeat the \
exact same nudge every time. Never sarcastic, never a joke at their \
expense, never more than ~2 short sentences total.
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

def _open_tickets_context(open_tickets: Optional[list]) -> str:
    """
    Builds the context block listing tickets this sender can act on, so
    close_ticket/cancel_ticket can be matched by content
    ("ปิดงานเปลี่ยนน้ำมัน") instead of requiring an exact id every time --
    see those rules above. This is their own open tickets PLUS the shared
    pool -- every department except เครื่องจักร/machinery (see
    get_actionable_tickets_for in tickets.py) -- i.e. not strictly "their
    own", so a shared-department ticket the other person filed can
    legitimately appear and get matched here too.
    """
    if not open_tickets:
        return "\n\nThis person has no open tickets right now."
    lines = ["\n\nTickets this person can act on (id: description):"]
    for t in open_tickets:
        lines.append(f"#{t['id']}: {t.get('summary') or t['message']}")
    return "\n".join(lines)


def _maintenance_context(maintenance_tasks: Optional[list]) -> str:
    """
    Builds the context block listing the recurring-maintenance catalog, so
    maintenance_done can be matched by content -- see that rule above. When
    not provided at all (None, not just empty), explicitly tells the model
    never to use maintenance_done -- this is how the intent stays gated to
    Ohm only for now (see app/webhook_handler.py, which only passes this in
    for him) without needing a separate prompt variant.
    """
    if maintenance_tasks is None:
        return "\n\nNo maintenance task catalog is available for this person -- never use maintenance_done."
    if not maintenance_tasks:
        return "\n\nThe maintenance task catalog is currently empty -- never use maintenance_done."
    lines = ["\n\nMaintenance task catalog (id: name [category]):"]
    for t in maintenance_tasks:
        lines.append(f"#{t['id']}: {t['name']} [{t['category']}]")
    return "\n".join(lines)


class Classification(TypedDict):
    intent: str
    department: str
    summary: Optional[str]
    due_date_days: Optional[int]
    due_date_calendar: Optional[str]
    remind_days_before: Optional[int]
    close_ticket_id: Optional[int]
    close_specific_no_match: bool
    maintenance_task_id: Optional[int]
    maintenance_completed_days_ago: Optional[int]
    maintenance_completed_calendar: Optional[str]
    cancel_ticket_id: Optional[int]
    banter_reply: Optional[str]


def _default_classification(awaiting_due_date: bool = False) -> Classification:
    """
    Used both as classify()'s pre-call starting value and its exception
    fallback -- if the API call fails, or returns something unparseable or
    an intent we don't recognize, this is exactly what the caller gets.

    Reported live: a transient API hiccup while someone had a pending
    "when's this due?" question turned a plain date reply ("24/8/69") into
    a phantom new ticket literally titled "24/8/69" -- because this always
    defaulted to new_ticket regardless of context. Defaulting to
    due_date_reply instead when awaiting_due_date is true (both date
    fields left null, which just asks them to repeat the date -- see
    due_date_unclear() in webhook_handler.py) avoids that without giving
    up the "never silently drop a message" principle: a reply to a
    just-asked question was never a candidate to be a fresh report in the
    first place. Outside that context, new_ticket remains the right
    default.
    """
    return {
        "intent": "due_date_reply" if awaiting_due_date else "new_ticket",
        "department": DEFAULT_DEPARTMENT,
        "summary": None,
        "due_date_days": None,
        "due_date_calendar": None,
        "remind_days_before": None,
        "close_ticket_id": None,
        "close_specific_no_match": False,
        "maintenance_task_id": None,
        "maintenance_completed_days_ago": None,
        "maintenance_completed_calendar": None,
        "cancel_ticket_id": None,
        "banter_reply": None,
    }


def _extract_json(text: str) -> dict:
    # Claude is asked to return raw JSON, but strip code fences defensively
    # in case it wraps the answer in ```json ... ```. Parse with
    # raw_decode() from the first '{' rather than a greedy \{.*\} regex --
    # the regex grabs everything up to the LAST '}' in the whole text, so
    # any trailing content after the real JSON (a closing ``` fence, a
    # stray sentence) that itself contains a brace anywhere later on
    # breaks json.loads with "Extra data". raw_decode() parses just the
    # first complete JSON value and ignores whatever comes after it.
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model output")
    obj, _ = json.JSONDecoder().raw_decode(text, start)
    return obj


def classify(
    message: str,
    awaiting_due_date: bool = False,
    open_tickets: Optional[list] = None,
    conversation_history: Optional[list] = None,
    maintenance_tasks: Optional[list] = None,
) -> Classification:
    """
    Classifies a single incoming text message. Never raises -- on any error,
    or if the model returns an intent we don't recognize, this falls back to
    "new_ticket" (better to log something as a ticket than silently drop
    it) -- except when awaiting_due_date is true, where it falls back to
    "due_date_reply" instead, so a failure while someone's mid-reply to
    "when's this due?" asks them to repeat it rather than fabricating a
    phantom ticket out of what was never a candidate to be a fresh report.

    awaiting_due_date: still used ONLY for that fallback default above (a
    cheap, deterministic DB check -- see get_open_tickets_for_reporter in
    tickets.py) -- it no longer shapes the prompt itself. That job now
    belongs to conversation_history below, which the model reads directly
    instead of us hand-summarizing "the bot just asked about a due date"
    into prose every time a new scenario like that comes up.

    open_tickets: this sender's currently open tickets (list of dicts with
    at least id/message/summary), so a close_ticket message can be matched
    to one by content instead of requiring an exact ticket number.

    conversation_history: recent turns for this sender (list of
    {"role": "user"|"assistant", "text": ...}, oldest first, NOT including
    `message` itself) -- see app/conversation.py. Passed to Claude as real
    prior turns, not summarized into the system prompt, so it can use
    context the way it naturally would in any other conversation instead of
    relying on us to have anticipated and hand-flagged the scenario.

    maintenance_tasks: the recurring-maintenance catalog (list of dicts
    with at least id/name/category), so a completion report can be matched
    by content -- see app/maintenance.py. Pass None (not just an empty
    list) to disable maintenance_done entirely for this call -- that's how
    the feature stays scoped to Ohm only for now (see app/webhook_handler.py).
    """
    today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    system = SYSTEM_PROMPT.format(today=today, tz=TIMEZONE)
    system += _open_tickets_context(open_tickets)
    system += _maintenance_context(maintenance_tasks)

    messages = [{"role": turn["role"], "content": turn["text"]} for turn in (conversation_history or [])]
    messages.append({"role": "user", "content": message})

    result = _default_classification(awaiting_due_date)
    raw_text = None
    try:
        response = _client_lazy().messages.create(
            model=MODEL,
            max_tokens=1024,
            # claude-sonnet-5 defaults to spending part of its budget on an
            # internal "thinking" block before the actual JSON answer, and
            # that spend is wildly variable -- measured anywhere from ~290
            # to over 1000 tokens on the SAME ordinary message across
            # repeated calls. Bumping max_tokens alone (300 -> 1024) still
            # left a ~37% truncation rate (stop_reason: "max_tokens", cut
            # off mid-JSON, _extract_json fails, caught below and silently
            # defaulted -- indistinguishable from a genuine API failure on
            # messages that had nothing wrong with them). Explicitly
            # disabling thinking removes the variability at its source:
            # confirmed reliable across repeated trials, and there's no
            # accuracy cost for a task this structured -- classification
            # doesn't need chain-of-thought, just correct extraction. 1024
            # is now pure headroom for the answer itself (summary/
            # banter_reply can both run a bit long), never for thinking.
            thinking={"type": "disabled"},
            system=system,
            messages=messages,
        )
        # Find the text block by type rather than assuming content[0] is it
        # -- thinking is disabled above so this should always be content[0]
        # now, but that exact assumption once silently broke every single
        # classification when a thinking block WAS present (always caught
        # by the except below and defaulted, until caught via live
        # testing), so keep the defensive lookup rather than re-earn that.
        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block is None:
            raise ValueError("no text block in model response")
        raw_text = text_block.text
        parsed = _extract_json(raw_text)

        intent = parsed.get("intent")
        if intent not in KNOWN_INTENTS:
            # unrecognized/hallucinated intent value -- same reasoning as
            # _default_classification()'s fallback: don't manufacture a
            # phantom ticket out of what's likely a pending due-date answer
            intent = "due_date_reply" if awaiting_due_date else "new_ticket"

        maintenance_task_id = parsed.get("maintenance_task_id")
        if not isinstance(maintenance_task_id, int) or isinstance(maintenance_task_id, bool):
            maintenance_task_id = None
        if intent == "maintenance_done" and maintenance_task_id is None:
            # Contract violation -- the prompt says never return this intent
            # without a matched id. Don't silently mark some task done out
            # of a guess; fall back exactly like an unrecognized intent.
            intent = "due_date_reply" if awaiting_due_date else "new_ticket"

        maintenance_completed_days_ago = parsed.get("maintenance_completed_days_ago")
        if not isinstance(maintenance_completed_days_ago, int) or isinstance(maintenance_completed_days_ago, bool):
            maintenance_completed_days_ago = None
        elif maintenance_completed_days_ago <= 0 or maintenance_completed_days_ago > 365:
            maintenance_completed_days_ago = None  # 0/negative/absurdly-large isn't meaningful as "days ago"

        maintenance_completed_calendar = parsed.get("maintenance_completed_calendar")
        if not isinstance(maintenance_completed_calendar, str) or not maintenance_completed_calendar.strip():
            maintenance_completed_calendar = None
        elif maintenance_completed_days_ago is not None:
            # Contract says use exactly one -- if the model gave both
            # anyway, the calendar date is the deterministic one (no model
            # arithmetic involved getting there), prefer it.
            maintenance_completed_days_ago = None

        department = parsed.get("department")
        if department not in KNOWN_DEPARTMENTS:
            department = DEFAULT_DEPARTMENT

        summary = parsed.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = None  # caller falls back to the raw message -- see webhook_handler.py

        remind_days_before = parsed.get("remind_days_before")
        if not isinstance(remind_days_before, int) or isinstance(remind_days_before, bool) or remind_days_before <= 0:
            remind_days_before = None  # 0/negative/non-numeric doesn't mean anything as "days before"

        banter_reply = parsed.get("banter_reply")
        if intent != "other" or not isinstance(banter_reply, str) or not banter_reply.strip():
            # only meaningful for "other" -- webhook_handler.py falls back to
            # a plain static reply if this is missing/empty
            banter_reply = None

        result = {
            "intent": intent,
            "department": department,
            "summary": summary,
            "due_date_days": parsed.get("due_date_days"),
            "due_date_calendar": parsed.get("due_date_calendar"),
            "remind_days_before": remind_days_before,
            "close_ticket_id": parsed.get("close_ticket_id"),
            "close_specific_no_match": bool(parsed.get("close_specific_no_match")),
            "maintenance_task_id": maintenance_task_id,
            "maintenance_completed_days_ago": maintenance_completed_days_ago,
            "maintenance_completed_calendar": maintenance_completed_calendar,
            "cancel_ticket_id": parsed.get("cancel_ticket_id"),
            "banter_reply": banter_reply,
        }
    except Exception:
        logger.exception(
            "classification failed, defaulting to %s",
            "due_date_reply (awaiting_due_date)" if awaiting_due_date else "new_ticket",
        )

    # NOTE: "other" used to be force-converted to "new_ticket" here, on the
    # theory that a low-confidence read should still make a ticket rather
    # than silently drop a possible report. In practice that meant every
    # confidently-recognized greeting/small-talk message ("hello", "สวัสดี")
    # also turned into a spurious ticket, since the classifier correctly
    # returns "other" for those. The actual "don't silently drop something
    # that might be a report" safety net is the `except` block above and the
    # KNOWN_INTENTS check (both already default to new_ticket on their own)
    # -- a clean, successful "other" classification is a real, confident
    # answer and gets routed as such (see webhook_handler.py: replies
    # conversationally, no ticket created).
    routed = result

    # Log the raw message alongside the classification so misclassifications
    # are easy to spot in the platform logs later.
    logger.info(
        json.dumps(
            {
                "raw_message": message,
                "awaiting_due_date": awaiting_due_date,
                "open_ticket_ids": [t["id"] for t in (open_tickets or [])],
                "history_turns": len(conversation_history or []),
                "model_output": raw_text,
                "classification": routed,
            },
            ensure_ascii=False,
        )
    )
    return routed

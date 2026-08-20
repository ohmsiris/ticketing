"""
Turns one LINE webhook "message" event into ticket actions + a reply.
This is the piece that ties classifier.py + tickets.py + strings.py + line_client.py
together; kept separate from main.py so the routing logic is easy to read
on its own.
"""
import logging

from app import strings, tickets
from app.classifier import classify
from app.config import settings
from app.line_client import QuickReplyOption, reply_message

logger = logging.getLogger("ticketing.webhook")

ONBOARDING_TICKET_THRESHOLD = 3  # mom gets onboarding copy for tickets 1-3


def resolve_reporter(line_user_id: str) -> str | None:
    if line_user_id == settings.ohm_line_user_id:
        return "ohm"
    if line_user_id == settings.mom_line_user_id:
        return "mom"
    return None


def handle_follow_event(event: dict) -> None:
    """
    Fires once when someone adds the bot as a friend (LINE's "follow"
    event) -- the natural moment to send a welcome/how-to-use message,
    instead of relying on someone pasting it manually. Unknown senders are
    just logged (same userId-discovery flow as handle_message_event) and
    not sent anything, so a stranger who finds the bot doesn't get a peek
    at what it's for.
    """
    source = event.get("source", {})
    line_user_id = source.get("userId")
    reply_token = event.get("replyToken")

    reporter = resolve_reporter(line_user_id)
    if reporter is None:
        logger.info("follow event from unknown LINE userId=%s -- ignoring", line_user_id)
        return

    reply_message(reply_token, [strings.welcome_message()])


def handle_message_event(event: dict) -> None:
    source = event.get("source", {})
    line_user_id = source.get("userId")
    reply_token = event.get("replyToken")
    message = event.get("message", {})
    message_type = message.get("type")

    reporter = resolve_reporter(line_user_id)
    if reporter is None:
        # Not one of our two known users. Log the id so it's easy to find
        # and paste into OHM_LINE_USER_ID / MOM_LINE_USER_ID on first contact.
        logger.info("message from unknown LINE userId=%s -- ignoring. %s", line_user_id, message)
        return

    if message_type == "audio":
        reply_message(reply_token, [strings.voice_not_supported()])
        return

    if message_type != "text":
        logger.info("ignoring non-text, non-audio message type=%s from %s", message_type, reporter)
        return

    text = message.get("text", "")

    # On-demand digest command, Ohm only. Checked before we bother calling
    # Claude, since it's an exact command, not natural-language content.
    if reporter == "ohm" and text.strip().lower() == "open tickets":
        open_tickets = tickets.get_open_tickets(min_age_hours=0)
        reply_message(reply_token, [strings.open_tickets_digest(open_tickets)])
        return

    # Fetched once and reused for two things: (a) tells the classifier when
    # this sender has a ticket still waiting on a due date, so a short reply
    # like "อีก2วัน" reads as an answer instead of a new, unrelated ticket,
    # and (b) gives it content to match a close_ticket message against, and
    # backs the tap-to-close picker if that match is ambiguous.
    open_tickets_for_reporter = tickets.get_open_tickets_for_reporter(reporter)
    awaiting_due_date = any(t["due_date"] is None for t in open_tickets_for_reporter)
    result = classify(text, awaiting_due_date=awaiting_due_date, open_tickets=open_tickets_for_reporter)
    intent = result["intent"]

    if intent == "new_ticket":
        _handle_new_ticket(reporter, text, result, reply_token)
    elif intent == "due_date_reply":
        _handle_due_date_reply(reporter, result, reply_token)
    elif intent == "close_ticket":
        _handle_close_ticket(reporter, result, open_tickets_for_reporter, reply_token)
    elif intent == "other":
        # Confidently not a report -- greeting, small talk, an unrelated
        # question. Reply conversationally instead of logging it as a
        # ticket (see classifier.py for why this is no longer forced into
        # new_ticket). classify() still defaults to new_ticket on anything
        # it can't confidently read as "other" either, so a real report
        # never gets silently dropped here. banter_reply is the
        # classifier's own short, warm reply to what they actually said;
        # falls back to a static line if it's missing for any reason.
        reply_message(reply_token, [result.get("banter_reply") or strings.unclear_message()])
    else:
        # Should be unreachable -- classify() only ever returns one of the
        # four branches above. Kept as a safety net in case that contract
        # ever changes.
        _handle_new_ticket(reporter, text, result, reply_token)


def _handle_new_ticket(reporter: str, text: str, result: dict, reply_token: str) -> None:
    department = result["department"]
    # summary is the classifier's cleaned-up version (framing like "เตือนให้"
    # and the due date itself stripped out) -- falls back to the raw text if
    # the classifier didn't give one. Used for both storage (so digests get
    # the benefit too) and this confirmation reply.
    summary = result.get("summary") or text
    ticket_id = tickets.create_ticket(reporter, text, department, summary)
    ticket_count = tickets.increment_ticket_count(reporter)

    # If the message already stated its own due date (e.g. "เปลี่ยนน้ำมัน
    # เครื่อง 27/9/69"), apply it right away instead of asking again -- same
    # resolution + past-date guard as an explicit due_date_reply. An
    # invalid/past embedded date is just ignored, falling back to asking.
    due_date = tickets.resolve_due_date(result.get("due_date_days"), result.get("due_date_calendar"))
    if due_date is not None and tickets.is_past_date(due_date):
        due_date = None
    if due_date is not None:
        tickets.set_due_date(reporter, due_date)  # the ticket just created is the most recent open one, still due-date-less

    remind_days_before = result.get("remind_days_before")
    if remind_days_before is not None:
        tickets.set_remind_days_before(ticket_id, remind_days_before)

    if due_date is not None:
        reply_texts = [
            strings.new_ticket_confirmation_with_due_date(ticket_id, summary, department, due_date, remind_days_before)
        ]
    else:
        reply_texts = [strings.new_ticket_confirmation(ticket_id, department), strings.ask_due_date()]

    if reporter == "mom" and ticket_count <= ONBOARDING_TICKET_THRESHOLD:
        tip = strings.onboarding_first_ticket() if ticket_count == 1 else strings.onboarding_reminder_tip()
        reply_texts[0] = f"{reply_texts[0]}\n{tip}"

    reply_message(reply_token, reply_texts)


def _handle_due_date_reply(reporter: str, result: dict, reply_token: str) -> None:
    due_date = tickets.resolve_due_date(result["due_date_days"], result["due_date_calendar"])
    remind_days_before = result.get("remind_days_before")

    if due_date is None:
        # No due date in this message. If it was a standalone reminder-
        # lead-time adjustment ("เตือนก่อน 3 วัน" on its own), apply that to
        # the most recent open ticket rather than treating it as unclear.
        if remind_days_before is not None:
            ticket = tickets.most_recent_open_ticket(reporter)
            if ticket is None:
                reply_message(reply_token, [strings.no_ticket_for_reminder()])
                return
            tickets.set_remind_days_before(ticket["id"], remind_days_before)
            display_text = ticket["summary"] or ticket["message"]
            reply_message(reply_token, [strings.remind_days_before_set(ticket["id"], display_text, remind_days_before)])
            return

        reply_message(reply_token, [strings.due_date_unclear()])
        return

    if tickets.is_past_date(due_date):
        reply_message(reply_token, [strings.due_date_in_past(due_date)])
        return

    ticket = tickets.set_due_date(reporter, due_date)
    if ticket is None:
        reply_message(reply_token, [strings.no_ticket_needs_due_date()])
        return

    if remind_days_before is not None:
        tickets.set_remind_days_before(ticket["id"], remind_days_before)

    display_text = ticket["summary"] or ticket["message"]
    reply_message(
        reply_token, [strings.due_date_set(ticket["id"], display_text, due_date, remind_days_before)]
    )


def _handle_close_ticket(reporter: str, result: dict, open_tickets_for_reporter: list[dict], reply_token: str) -> None:
    # An id here can come from an explicit number in the message, or from
    # the classifier content-matching it against open_tickets_for_reporter
    # (e.g. "ปิดงานเปลี่ยนน้ำมัน") -- either way, close_ticket_by_id is
    # reporter-scoped so this can't ever close the other person's ticket.
    ticket_id = result.get("close_ticket_id")
    if ticket_id is not None:
        ticket = tickets.close_ticket_by_id(ticket_id, reporter)
        if ticket is None:
            reply_message(reply_token, [strings.ticket_not_found_or_closed(ticket_id)])
            return
        reply_message(reply_token, [strings.ticket_closed(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    if not open_tickets_for_reporter:
        reply_message(reply_token, [strings.no_open_ticket_to_close()])
        return

    quick_reply = [
        QuickReplyOption(label=_close_picker_label(t), text=f"ปิด #{t['id']}") for t in open_tickets_for_reporter
    ]

    # They described something specific ("ซ่อมมอเตอร์เสร็จแล้ว") but it
    # didn't match any open ticket -- even if only one happens to be open,
    # don't assume that unrelated one is the one they mean. Say so and let
    # them pick if it's actually one of these after all.
    if result.get("close_specific_no_match"):
        reply_message(reply_token, [strings.close_ticket_no_match_prompt()], quick_reply=quick_reply)
        return

    if len(open_tickets_for_reporter) == 1:
        # Generic close phrase, nothing to match against, and only one
        # candidate -- safe to assume that's the one.
        ticket = tickets.close_ticket_by_id(open_tickets_for_reporter[0]["id"], reporter)
        reply_message(reply_token, [strings.ticket_closed(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    # More than one open ticket and nothing to tell them apart by -- ask via
    # tappable buttons instead of guessing which one they meant.
    reply_message(reply_token, [strings.close_ticket_picker_prompt()], quick_reply=quick_reply)


def _close_picker_label(ticket: dict) -> str:
    text = ticket.get("summary") or ticket["message"]
    label = f"#{ticket['id']} {text}"
    return label if len(label) <= 20 else label[:19] + "…"

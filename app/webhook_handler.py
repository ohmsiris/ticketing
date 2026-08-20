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
from app.line_client import reply_message

logger = logging.getLogger("ticketing.webhook")

ONBOARDING_TICKET_THRESHOLD = 3  # mom gets onboarding copy for tickets 1-3


def resolve_reporter(line_user_id: str) -> str | None:
    if line_user_id == settings.ohm_line_user_id:
        return "ohm"
    if line_user_id == settings.mom_line_user_id:
        return "mom"
    return None


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

    # Hint the classifier when this sender has a ticket still waiting on a
    # due date, so a short reply like "อีก2วัน" gets read as an answer to
    # that instead of guessed cold as a new, unrelated ticket.
    awaiting_due_date = tickets.has_pending_due_date_ticket(reporter)
    result = classify(text, awaiting_due_date=awaiting_due_date)
    intent = result["intent"]

    if intent == "new_ticket":
        _handle_new_ticket(reporter, text, result["department"], reply_token)
    elif intent == "due_date_reply":
        _handle_due_date_reply(reporter, result, reply_token)
    elif intent == "close_ticket":
        _handle_close_ticket(reporter, result, reply_token)
    else:
        # classify() already normalizes unknown/"other" intents to
        # new_ticket, so this branch should be unreachable -- kept as a
        # safety net in case that contract ever changes.
        _handle_new_ticket(reporter, text, result["department"], reply_token)


def _handle_new_ticket(reporter: str, text: str, department: str, reply_token: str) -> None:
    ticket_id = tickets.create_ticket(reporter, text, department)
    ticket_count = tickets.increment_ticket_count(reporter)

    confirmation = strings.new_ticket_confirmation(ticket_id, department)
    if reporter == "mom" and ticket_count <= ONBOARDING_TICKET_THRESHOLD:
        tip = strings.onboarding_first_ticket() if ticket_count == 1 else strings.onboarding_reminder_tip()
        confirmation = f"{confirmation}\n{tip}"

    reply_message(reply_token, [confirmation, strings.ask_due_date()])


def _handle_due_date_reply(reporter: str, result: dict, reply_token: str) -> None:
    due_date = tickets.resolve_due_date(result["due_date_days"], result["due_date_calendar"])
    if due_date is None:
        reply_message(reply_token, [strings.due_date_unclear()])
        return

    if tickets.is_past_date(due_date):
        reply_message(reply_token, [strings.due_date_in_past(due_date)])
        return

    ticket = tickets.set_due_date(reporter, due_date)
    if ticket is None:
        reply_message(reply_token, [strings.no_ticket_needs_due_date()])
        return

    reply_message(reply_token, [strings.due_date_set(ticket["id"], ticket["message"], due_date)])


def _handle_close_ticket(reporter: str, result: dict, reply_token: str) -> None:
    ticket_id = result.get("close_ticket_id")
    if ticket_id is not None:
        ticket = tickets.close_ticket_by_id(ticket_id)
        if ticket is None:
            reply_message(reply_token, [strings.ticket_not_found_or_closed(ticket_id)])
            return
        reply_message(reply_token, [strings.ticket_closed(ticket["id"])])
        return

    ticket = tickets.close_most_recent_open(reporter)
    if ticket is None:
        reply_message(reply_token, [strings.no_open_ticket_to_close()])
        return
    reply_message(reply_token, [strings.ticket_closed(ticket["id"])])

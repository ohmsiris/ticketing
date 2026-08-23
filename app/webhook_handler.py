"""
Turns one LINE webhook "message" event into ticket actions + a reply.
This is the piece that ties classifier.py + tickets.py + strings.py + line_client.py
together; kept separate from main.py so the routing logic is easy to read
on its own.
"""
import logging

from app import bills, strings, tickets
from app.bill_extraction import extract_bill
from app.classifier import classify
from app.config import settings
from app.line_client import QuickReplyOption, download_message_content, push_message, reply_message

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
    # "group" (LINE group chat) or "room" (multi-person room) vs "user"
    # (a private 1-on-1 chat with the bot) -- this is what lets bill
    # notifications stay OUT of groups entirely, see _handle_bill_message.
    is_group = source.get("type") in ("group", "room")
    reply_token = event.get("replyToken")
    message = event.get("message", {})
    message_type = message.get("type")

    reporter = resolve_reporter(line_user_id)
    if reporter is None:
        # Not one of our two known users. Log the id so it's easy to find
        # and paste into OHM_LINE_USER_ID / MOM_LINE_USER_ID on first contact.
        logger.info("message from unknown LINE userId=%s -- ignoring. %s", line_user_id, message)
        return

    if message_type in ("image", "file"):
        _handle_bill_message(reporter, message, is_group, reply_token)
        return

    if message_type == "audio":
        reply_message(reply_token, [strings.voice_not_supported()])
        return

    if message_type != "text":
        logger.info("ignoring non-text, non-audio/image/file message type=%s from %s", message_type, reporter)
        return

    text = message.get("text", "")

    # On-demand digest commands, Ohm only. Checked before we bother calling
    # Claude, since these are exact commands, not natural-language content.
    # "open tickets": how long each has been sitting open. "งานทั้งหมด":
    # everything open right now sorted by due date -- different angle on
    # the same underlying open tickets, not a duplicate of the other.
    stripped = text.strip()
    if reporter == "ohm" and stripped.lower() == "open tickets":
        open_tickets = tickets.get_open_tickets(min_age_hours=0)
        reply_message(reply_token, [strings.open_tickets_digest(open_tickets)])
        return
    if reporter == "ohm" and stripped == "งานทั้งหมด":
        reply_message(reply_token, [strings.all_open_tickets_digest(tickets.get_all_open_tickets_by_due_date())])
        return

    # awaiting_due_date: is THIS sender's own ticket-creation flow mid a
    # "when's this due?" question -- a personal, sequential thread, so it
    # stays scoped to their own tickets only (see get_open_tickets_for_reporter).
    # actionable_tickets: everything close_ticket/cancel_ticket can act on --
    # their own tickets, PLUS the shared pool (any open ticket outside
    # เครื่องจักร/machinery, regardless of who filed it) -- fed to the
    # classifier for content-matching and to the close/cancel handlers for
    # the single-target/picker logic. Two different lists on purpose:
    # someone else's open shared-department ticket shouldn't make a short
    # reply read as answering a due-date question that was never asked of
    # THIS sender.
    open_tickets_for_reporter = tickets.get_open_tickets_for_reporter(reporter)
    awaiting_due_date = any(t["due_date"] is None for t in open_tickets_for_reporter)
    actionable_tickets = tickets.get_actionable_tickets_for(reporter)
    result = classify(text, awaiting_due_date=awaiting_due_date, open_tickets=actionable_tickets)
    intent = result["intent"]

    if intent == "new_ticket":
        _handle_new_ticket(reporter, text, result, reply_token)
    elif intent == "due_date_reply":
        _handle_due_date_reply(reporter, result, reply_token)
    elif intent == "close_ticket":
        _handle_close_ticket(reporter, result, actionable_tickets, reply_token)
    elif intent == "cancel_ticket":
        _handle_cancel_ticket(reporter, result, actionable_tickets, awaiting_due_date, reply_token)
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
        # branches above. Kept as a safety net in case that contract ever
        # changes.
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


def _handle_close_ticket(reporter: str, result: dict, actionable_tickets: list[dict], reply_token: str) -> None:
    # An id here can come from an explicit number in the message, or from
    # the classifier content-matching it against actionable_tickets (e.g.
    # "ปิดงานเปลี่ยนน้ำมัน") -- either way, close_ticket_by_id enforces the
    # real rule at the DB level: this can close the sender's own tickets,
    # or any รถ-department ticket (shared pool, any reporter), never
    # anything else.
    ticket_id = result.get("close_ticket_id")
    if ticket_id is not None:
        ticket = tickets.close_ticket_by_id(ticket_id, reporter)
        if ticket is None:
            reply_message(reply_token, [strings.ticket_not_found_or_closed(ticket_id)])
            return
        reply_message(reply_token, [strings.ticket_closed(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    if not actionable_tickets:
        reply_message(reply_token, [strings.no_open_ticket_to_close()])
        return

    quick_reply = [
        QuickReplyOption(label=_ticket_picker_label(t), text=f"ปิด #{t['id']}") for t in actionable_tickets
    ]

    # They described something specific ("ซ่อมมอเตอร์เสร็จแล้ว") but it
    # didn't match any open ticket -- even if only one happens to be open,
    # don't assume that unrelated one is the one they mean. Say so and let
    # them pick if it's actually one of these after all.
    if result.get("close_specific_no_match"):
        reply_message(reply_token, [strings.close_ticket_no_match_prompt()], quick_reply=quick_reply)
        return

    if len(actionable_tickets) == 1:
        # Generic close phrase, nothing to match against, and only one
        # candidate -- safe to assume that's the one.
        ticket = tickets.close_ticket_by_id(actionable_tickets[0]["id"], reporter)
        reply_message(reply_token, [strings.ticket_closed(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    # More than one open ticket and nothing to tell them apart by -- ask via
    # tappable buttons instead of guessing which one they meant.
    reply_message(reply_token, [strings.close_ticket_picker_prompt()], quick_reply=quick_reply)


def _handle_cancel_ticket(
    reporter: str,
    result: dict,
    actionable_tickets: list[dict],
    awaiting_due_date: bool,
    reply_token: str,
) -> None:
    """
    Voids at most one ticket -- there is no bulk-cancel path anywhere in
    this app, by design (see the cancel_ticket rule in classifier.py: a
    bare cancel word with no number is never "cancel everything", even if
    phrased that way). Shape mirrors _handle_close_ticket: explicit id
    first, then the awaiting-due-date shortcut (always the sender's own
    ticket, never a shared car one -- see get_open_tickets_for_reporter),
    then an unambiguous single-candidate case, then a tap-to-cancel picker
    when there's more than one and nothing to disambiguate by.
    """
    ticket_id = result.get("cancel_ticket_id")
    if ticket_id is not None:
        ticket = tickets.cancel_ticket_by_id(ticket_id, reporter)
        if ticket is None:
            reply_message(reply_token, [strings.ticket_not_found_or_closed(ticket_id)])
            return
        reply_message(reply_token, [strings.ticket_cancelled(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    # No explicit number, but this arrived right after the bot asked for a
    # due date -- unambiguously means "cancel the ticket you just asked me
    # about", regardless of how many other tickets happen to be open, so no
    # picker is needed here even if there's more than one open ticket.
    if awaiting_due_date:
        ticket = tickets.cancel_most_recent_missing_due_date_ticket(reporter)
        if ticket is not None:
            reply_message(
                reply_token, [strings.ticket_cancelled(ticket["id"], ticket["summary"] or ticket["message"])]
            )
            return
        # Nothing was actually missing a due date after all -- fall through
        # to the generic path below rather than silently doing nothing.

    if not actionable_tickets:
        reply_message(reply_token, [strings.no_open_ticket_to_cancel()])
        return

    if len(actionable_tickets) == 1:
        # Generic cancel phrase, nothing to disambiguate, only one
        # candidate -- safe to assume that's the one.
        ticket = tickets.cancel_ticket_by_id(actionable_tickets[0]["id"], reporter)
        reply_message(reply_token, [strings.ticket_cancelled(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    # More than one open ticket and nothing to tell them apart by -- ask via
    # tappable buttons instead of guessing which one they meant. Still only
    # ever cancels the single one they tap.
    quick_reply = [
        QuickReplyOption(label=_ticket_picker_label(t), text=f"ยกเลิก #{t['id']}") for t in actionable_tickets
    ]
    reply_message(reply_token, [strings.cancel_ticket_picker_prompt()], quick_reply=quick_reply)


def _handle_bill_message(reporter: str, message: dict, is_group: bool, reply_token: str) -> None:
    """
    Handles a photographed bill or PDF. Deliberately NEVER replies into a
    group/room (is_group) -- whatever happens, the confirmation always
    goes as a PRIVATE push to the manager (OHM_LINE_USER_ID), regardless
    of who sent the photo or where. In a private chat, a quick "reading
    it now" ack is sent too since extraction takes a few seconds.

    Multi-page bills: LINE delivers a multi-select gallery send as
    consecutive events from the same sender, so bills.find_open_chain
    picks up right where the last photo left off. See bills.py for the
    actual chain/merge logic.
    """
    message_id = message.get("id")
    message_type = message.get("type")
    logger.info(
        "bill message received: type=%s from=%s group=%s message_id=%s",
        message_type, reporter, is_group, message_id,
    )

    if message_type == "file":
        file_name = (message.get("fileName") or "").lower()
        if not file_name.endswith(".pdf"):
            if not is_group:
                reply_message(reply_token, [strings.bill_unsupported_file()])
            else:
                logger.info("ignoring non-PDF file %r from %s in a group", file_name, reporter)
            return
        media_type = "application/pdf"
    else:
        media_type = "image/jpeg"  # LINE always serves downloaded images as JPEG regardless of original format

    if not is_group:
        reply_message(reply_token, [strings.bill_processing_ack()])

    try:
        file_bytes = download_message_content(message_id)
        extracted = extract_bill(file_bytes, media_type)
    except Exception:
        logger.exception("bill extraction failed for message_id=%s from %s", message_id, reporter)
        if not is_group:
            reply_message(reply_token, [strings.bill_extraction_failed()])
        # Always tell the manager privately too, even from a group where we
        # otherwise stay silent -- a failure should never be invisible to
        # everyone, only successes are meant to route exclusively through
        # the manager's private chat.
        push_message(
            settings.ohm_line_user_id,
            [f"⚠️ อ่านบิลไม่สำเร็จ (จาก {reporter}, message_id={message_id}) เช็ค log ดูนะครับ"],
        )
        return

    logger.info(
        "extracted OK: shop=%r total=%r items=%d continues_next_page=%s",
        extracted.get("shop_name"), extracted.get("total_cost"),
        len(extracted.get("line_items") or []), extracted.get("continues_next_page"),
    )

    note = None
    open_chain = bills.find_open_chain(reporter)
    if open_chain is not None:
        logger.info("merging into open chain %s", open_chain["bill_id"])
        result = bills.append_page_to_chain(open_chain["bill_id"], extracted, message_id)
        bill = result["bill"]
        if not result["totals_reconciled"]:
            note = strings.bill_totals_mismatch_note(result["combined_sum"], result["final_total"])
            logger.warning(
                "bill %s totals mismatch: combined=%s final=%s",
                bill["bill_id"], result["combined_sum"], result["final_total"],
            )
    else:
        bill_id = bills.create_bill(reporter, "external_bill", extracted, message_id)
        bill = bills.get_bill(bill_id)
        logger.info("created new bill %s from %s", bill_id, reporter)

    if bill.get("continues_next_page"):
        # Still an open chain awaiting its next page -- don't notify yet,
        # the next photo (or the 30-minute staleness window) decides
        # what happens next. See bills.find_open_chain.
        logger.info("bill %s still awaiting next page from %s", bill["bill_id"], reporter)
        return

    # Fold in the deterministic roster-match warning (e.g. "vehicle number
    # not found") from bill_extraction.py alongside any totals-mismatch
    # note, so the manager sees everything worth double-checking in one
    # message rather than only in the review page.
    vehicle_warning = (bill.get("vehicle_match_warning") or "").strip()
    if vehicle_warning:
        note = f"{note}; {vehicle_warning}" if note else vehicle_warning

    review_url = f"{settings.public_base_url}/bills/{bill['bill_id']}?token={settings.review_token}"
    logger.info("bill %s ready for review, notifying manager: %s", bill["bill_id"], review_url)
    push_message(settings.ohm_line_user_id, [strings.bill_ready_for_review(bill, review_url, note)])


def _ticket_picker_label(ticket: dict) -> str:
    text = ticket.get("summary") or ticket["message"]
    label = f"#{ticket['id']} {text}"
    return label if len(label) <= 20 else label[:19] + "…"

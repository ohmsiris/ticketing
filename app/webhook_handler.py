"""
Turns one LINE webhook "message" event into ticket actions + a reply.
This is the piece that ties classifier.py + tickets.py + strings.py + line_client.py
together; kept separate from main.py so the routing logic is easy to read
on its own.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app import bills, conversation, maintenance, sheets_client, slips, supplies, strings, tickets
from app.bill_extraction import extract_bill
from app.classifier import classify
from app.config import TIMEZONE, settings
from app.image_classifier import classify_image
from app.line_client import QuickReplyOption, download_message_content, push_message, reply_message
from app.slip_extraction import extract_slip
from app.supply_extraction import extract_supply_purchase

logger = logging.getLogger("ticketing.webhook")

ONBOARDING_TICKET_THRESHOLD = 3  # mom gets onboarding copy for tickets 1-3
BANGKOK = ZoneInfo(TIMEZONE)
DOCUMENT_TYPE_COMMAND_PREFIX = "เอกสาร:"  # see _send_document_type_picker / _handle_document_type_confirmation
FINALIZE_CHAIN_COMMAND_PREFIX = "จบบิล:"  # see _handle_finalize_chain_command


def _line_user_id_for(reporter: str) -> str:
    return settings.ohm_line_user_id if reporter == "ohm" else settings.mom_line_user_id


def _bangkok_date_str(iso_utc: str) -> str:
    return datetime.fromisoformat(iso_utc).astimezone(BANGKOK).date().isoformat()


def resolve_reporter(line_user_id: str) -> str | None:
    if line_user_id == settings.ohm_line_user_id:
        return "ohm"
    if line_user_id == settings.mom_line_user_id:
        return "mom"
    return None


def _reply(reporter: str, reply_token: str, texts: list[str], quick_reply: list[QuickReplyOption] | None = None) -> None:
    """
    Wraps line_client.reply_message to also log the bot's reply into
    conversation history (see app/conversation.py), so the NEXT message
    from this sender gets real context instead of us hand-flagging it.
    Use this instead of calling reply_message directly anywhere in the
    classify()-driven ticket flow (new_ticket/due_date_reply/close_ticket/
    cancel_ticket/other). The separate photo/bill/slip flow and HQ
    real-time notifications deliberately don't use this -- see
    app/conversation.py's module docstring for why.
    """
    reply_message(reply_token, texts, quick_reply=quick_reply)
    conversation.log_turn(reporter, "assistant", "\n".join(texts))


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
        _handle_photo_message(reporter, message, is_group, reply_token)
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

    # A tap on the photo-type disambiguation picker (see
    # _handle_photo_message) arrives as an ordinary text message, same
    # mechanism as the ticket close/cancel pickers -- but unlike those,
    # there's no ticket id in the database to act on here, just a photo
    # that needs re-fetching. Checked before anything else text-related,
    # same reasoning as the on-demand commands above.
    if stripped.startswith(DOCUMENT_TYPE_COMMAND_PREFIX):
        _handle_document_type_confirmation(reporter, stripped, reply_token)
        return

    # A tap on the "no more pages" button attached to an awaiting-next-
    # page notification (see _handle_bill_message / _handle_supply_
    # purchase_message) -- same interception pattern as the two commands
    # above, checked before anything text-classification-related.
    if stripped.startswith(FINALIZE_CHAIN_COMMAND_PREFIX):
        _handle_finalize_chain_command(reporter, stripped, reply_token)
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

    # Maintenance-completion matching is scoped to Ohm only for now (see
    # app/maintenance.py's module docstring) -- passing None rather than an
    # empty list tells the classifier to never use maintenance_done at all
    # for anyone else, not just "nothing matched".
    maintenance_tasks = maintenance.get_active_tasks() if reporter == "ohm" else None

    # Fetched BEFORE logging this message -- otherwise it'd show up twice
    # (once here, once as the new message classify() is asked to read).
    # See app/conversation.py.
    history = conversation.get_recent_history(reporter)
    conversation.log_turn(reporter, "user", text)

    result = classify(
        text,
        awaiting_due_date=awaiting_due_date,
        open_tickets=actionable_tickets,
        conversation_history=history,
        maintenance_tasks=maintenance_tasks,
    )
    intent = result["intent"]

    if intent == "new_ticket":
        _handle_new_ticket(reporter, text, result, reply_token)
    elif intent == "due_date_reply":
        _handle_due_date_reply(reporter, result, reply_token)
    elif intent == "close_ticket":
        _handle_close_ticket(reporter, result, actionable_tickets, reply_token)
    elif intent == "cancel_ticket":
        _handle_cancel_ticket(reporter, result, actionable_tickets, awaiting_due_date, reply_token)
    elif intent == "maintenance_done":
        _handle_maintenance_done(reporter, text, result, reply_token)
    elif intent == "other":
        # Confidently not a report -- greeting, small talk, an unrelated
        # question. Reply conversationally instead of logging it as a
        # ticket (see classifier.py for why this is no longer forced into
        # new_ticket). classify() still defaults to new_ticket on anything
        # it can't confidently read as "other" either, so a real report
        # never gets silently dropped here. banter_reply is the
        # classifier's own short, warm reply to what they actually said;
        # falls back to a static line if it's missing for any reason.
        _reply(reporter, reply_token, [result.get("banter_reply") or strings.unclear_message()])
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

    # Real-time heads-up to Ohm (HQ) whenever someone else reports
    # something -- he sees it the moment it happens, not just later via
    # the scheduled digests (which only cover what's still open, not "this
    # just came in"). Skip when Ohm is the one reporting; he doesn't need
    # to be told about his own message.
    if reporter != "ohm":
        push_message(
            settings.ohm_line_user_id,
            [strings.new_ticket_notify_hq(ticket_id, reporter, department, summary, due_date, remind_days_before)],
        )

    if due_date is not None:
        reply_texts = [
            strings.new_ticket_confirmation_with_due_date(ticket_id, summary, department, due_date, remind_days_before)
        ]
    else:
        reply_texts = [strings.new_ticket_confirmation(ticket_id, department), strings.ask_due_date()]

    if reporter == "mom" and ticket_count <= ONBOARDING_TICKET_THRESHOLD:
        tip = strings.onboarding_first_ticket() if ticket_count == 1 else strings.onboarding_reminder_tip()
        reply_texts[0] = f"{reply_texts[0]}\n{tip}"

    _reply(reporter, reply_token, reply_texts)


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
                _reply(reporter, reply_token, [strings.no_ticket_for_reminder()])
                return
            tickets.set_remind_days_before(ticket["id"], remind_days_before)
            display_text = ticket["summary"] or ticket["message"]
            if reporter != "ohm":
                push_message(
                    settings.ohm_line_user_id,
                    [strings.remind_days_before_set_notify_hq(ticket["id"], reporter, display_text, remind_days_before)],
                )
            _reply(reporter, reply_token, [strings.remind_days_before_set(ticket["id"], display_text, remind_days_before)])
            return

        _reply(reporter, reply_token, [strings.due_date_unclear()])
        return

    if tickets.is_past_date(due_date):
        _reply(reporter, reply_token, [strings.due_date_in_past(due_date)])
        return

    ticket = tickets.set_due_date(reporter, due_date)
    if ticket is None:
        _reply(reporter, reply_token, [strings.no_ticket_needs_due_date()])
        return

    if remind_days_before is not None:
        tickets.set_remind_days_before(ticket["id"], remind_days_before)

    display_text = ticket["summary"] or ticket["message"]
    if reporter != "ohm":
        push_message(
            settings.ohm_line_user_id,
            [strings.due_date_set_notify_hq(ticket["id"], reporter, display_text, due_date, remind_days_before)],
        )
    _reply(reporter, reply_token, [strings.due_date_set(ticket["id"], display_text, due_date, remind_days_before)])


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
            _reply(reporter, reply_token, [strings.ticket_not_found_or_closed(ticket_id)])
            return
        if reporter != "ohm":
            push_message(
                settings.ohm_line_user_id,
                [strings.ticket_closed_notify_hq(ticket["id"], reporter, ticket["summary"] or ticket["message"])],
            )
        _reply(reporter, reply_token, [strings.ticket_closed(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    if not actionable_tickets:
        _reply(reporter, reply_token, [strings.no_open_ticket_to_close()])
        return

    quick_reply = [
        QuickReplyOption(label=_ticket_picker_label(t), text=f"ปิด #{t['id']}") for t in actionable_tickets
    ]

    # They described something specific ("ซ่อมมอเตอร์เสร็จแล้ว") but it
    # didn't match any open ticket -- even if only one happens to be open,
    # don't assume that unrelated one is the one they mean. Say so and let
    # them pick if it's actually one of these after all.
    if result.get("close_specific_no_match"):
        _reply(reporter, reply_token, [strings.close_ticket_no_match_prompt()], quick_reply=quick_reply)
        return

    if len(actionable_tickets) == 1:
        # Generic close phrase, nothing to match against, and only one
        # candidate -- safe to assume that's the one.
        ticket = tickets.close_ticket_by_id(actionable_tickets[0]["id"], reporter)
        if reporter != "ohm":
            push_message(
                settings.ohm_line_user_id,
                [strings.ticket_closed_notify_hq(ticket["id"], reporter, ticket["summary"] or ticket["message"])],
            )
        _reply(reporter, reply_token, [strings.ticket_closed(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    # More than one open ticket and nothing to tell them apart by -- ask via
    # tappable buttons instead of guessing which one they meant.
    _reply(reporter, reply_token, [strings.close_ticket_picker_prompt()], quick_reply=quick_reply)


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
            _reply(reporter, reply_token, [strings.ticket_not_found_or_closed(ticket_id)])
            return
        if reporter != "ohm":
            push_message(
                settings.ohm_line_user_id,
                [strings.ticket_cancelled_notify_hq(ticket["id"], reporter, ticket["summary"] or ticket["message"])],
            )
        _reply(reporter, reply_token, [strings.ticket_cancelled(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    # No explicit number, but this arrived right after the bot asked for a
    # due date -- unambiguously means "cancel the ticket you just asked me
    # about", regardless of how many other tickets happen to be open, so no
    # picker is needed here even if there's more than one open ticket.
    if awaiting_due_date:
        ticket = tickets.cancel_most_recent_missing_due_date_ticket(reporter)
        if ticket is not None:
            if reporter != "ohm":
                push_message(
                    settings.ohm_line_user_id,
                    [strings.ticket_cancelled_notify_hq(ticket["id"], reporter, ticket["summary"] or ticket["message"])],
                )
            _reply(reporter, reply_token, [strings.ticket_cancelled(ticket["id"], ticket["summary"] or ticket["message"])])
            return
        # Nothing was actually missing a due date after all -- fall through
        # to the generic path below rather than silently doing nothing.

    if not actionable_tickets:
        _reply(reporter, reply_token, [strings.no_open_ticket_to_cancel()])
        return

    if len(actionable_tickets) == 1:
        # Generic cancel phrase, nothing to disambiguate, only one
        # candidate -- safe to assume that's the one.
        ticket = tickets.cancel_ticket_by_id(actionable_tickets[0]["id"], reporter)
        if reporter != "ohm":
            push_message(
                settings.ohm_line_user_id,
                [strings.ticket_cancelled_notify_hq(ticket["id"], reporter, ticket["summary"] or ticket["message"])],
            )
        _reply(reporter, reply_token, [strings.ticket_cancelled(ticket["id"], ticket["summary"] or ticket["message"])])
        return

    # More than one open ticket and nothing to tell them apart by -- ask via
    # tappable buttons instead of guessing which one they meant. Still only
    # ever cancels the single one they tap.
    quick_reply = [
        QuickReplyOption(label=_ticket_picker_label(t), text=f"ยกเลิก #{t['id']}") for t in actionable_tickets
    ]
    _reply(reporter, reply_token, [strings.cancel_ticket_picker_prompt()], quick_reply=quick_reply)


def _handle_maintenance_done(reporter: str, text: str, result: dict, reply_token: str) -> None:
    """
    Logs a completed recurring-maintenance task -- scoped to Ohm only for
    now (see maintenance_tasks' gating in handle_message_event above, which
    is why this branch can only ever be reached by him at the moment).
    maintenance_task_id is always non-null here -- classify() only ever
    returns this intent when it found a confident catalog match (see the
    maintenance_done rule in classifier.py).
    """
    task_id = result["maintenance_task_id"]
    task = maintenance.get_task(task_id)
    if task is None:
        # Shouldn't happen -- classify() only ever sees ids from the live
        # active catalog -- but fail safe rather than crash if it somehow
        # changed between that call and now.
        _reply(reporter, reply_token, [strings.maintenance_task_not_found()])
        return

    # "เมื่อวานล้างฟรีซหลอด 2 เสร็จ" / "...เมื่อวันที่ 15 มิถุนายน" (did it
    # earlier, forgot to report at the time) backdates the log entry --
    # otherwise get_due_tasks() would anchor the next-due date on today,
    # later than it actually should be.
    days_ago = result.get("maintenance_completed_days_ago")
    calendar_date = result.get("maintenance_completed_calendar")
    completed_at = maintenance.resolve_completed_at(days_ago, calendar_date)
    maintenance.log_completion(task_id, reporter, text, completed_at=completed_at)

    # Best-effort mirror to a separate Google Sheet -- a no-op (logged, not
    # raised) if MAINTENANCE_SHEET_ID isn't configured, so this never blocks
    # the reply. SQLite is already the real source of truth either way.
    completed_date = tickets.today_bangkok_str() if completed_at is None else _bangkok_date_str(completed_at)
    sheets_client.log_maintenance_completion(completed_date, task["category"], task["name"], reporter, text)

    _reply(reporter, reply_token, [strings.maintenance_done_confirmation(task["name"], days_ago, calendar_date)])


def _handle_photo_message(reporter: str, message: dict, is_group: bool, reply_token: str) -> None:
    """
    Shared entry point for every image/file message -- works out the media
    type, sends one immediate ack, downloads the file once, then asks
    app.image_classifier.classify_image() whether it's a repair bill, a
    payment slip, a supply-purchase bill, or not a document at all (see
    that module's docstring for why that 4th case exists -- mechanics send
    whole batches of photos together, most of which aren't the receipt).
    When it's confidently one of those, route straight there (a confident
    not_a_document is routed to _handle_non_document_photo, which just
    skips it -- no extraction, no reply, no review-queue entry). When it's
    genuinely unsure -- torn between two real types, OR unsure whether
    this is even a document at all -- ask via a tap-to-choose picker
    instead of guessing, see _send_document_type_picker. A classification
    failure (the "unclear" sentinel, not a real document_type -- see
    image_classifier.classify_image) falls back to the bill flow, the
    long-standing safety net for "we don't actually know" that predates
    this classifier entirely.

    Deliberately never replies into a group/room (is_group) beyond the
    unsupported-file case -- whatever happens after that, the outcome goes
    as a PRIVATE push to the manager (OHM_LINE_USER_ID), regardless of who
    sent the photo or where. See _handle_bill_message / _handle_slip_message
    / _handle_supply_purchase_message. The disambiguation picker is
    likewise skipped in groups -- just uses the best guess there (falling
    back to the bill flow on anything not confidently not_a_document,
    same reasoning as a classification failure above).
    """
    message_id = message.get("id")
    message_type = message.get("type")
    logger.info(
        "photo message received: type=%s from=%s group=%s message_id=%s",
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
        reply_message(reply_token, [strings.photo_processing_ack()])

    try:
        file_bytes = download_message_content(message_id)
    except Exception:
        logger.exception("failed to download message_id=%s from %s", message_id, reporter)
        if not is_group:
            reply_message(reply_token, [strings.bill_extraction_failed()])
        push_message(
            settings.ohm_line_user_id,
            [f"⚠️ ดาวน์โหลดรูปไม่สำเร็จ (จาก {reporter}, message_id={message_id}) เช็ค log ดูนะครับ"],
        )
        return

    classification = classify_image(file_bytes, media_type)
    logger.info(
        "classified message_id=%s as %s (confidence=%s)",
        message_id, classification.document_type, classification.confidence,
    )

    # Ask whenever genuinely unsure -- torn between two real types, or
    # unsure whether this is even a document -- never just for a confident
    # not_a_document read (see _send_document_type_picker's 4th option for
    # the case where the model WAS unsure and guessed not_a_document but
    # was wrong). Groups never get asked, same as before this existed.
    if classification.confidence == "low" and not is_group:
        _send_document_type_picker(reporter, message_id, media_type)
        return

    _route_photo_by_type(reporter, classification.document_type, message_id, file_bytes, media_type, is_group, reply_token)


def _route_photo_by_type(
    reporter: str, doc_type: str, message_id: str, file_bytes: bytes, media_type: str, is_group: bool, reply_token: str
) -> None:
    if doc_type == "payment_slip":
        _handle_slip_message(reporter, message_id, file_bytes, is_group, reply_token)
    elif doc_type == "supply_purchase":
        _handle_supply_purchase_message(reporter, message_id, file_bytes, media_type, is_group, reply_token)
    elif doc_type == "not_a_document":
        _handle_non_document_photo(reporter, message_id)
    else:
        # "repair_bill", or the "unclear" classification-failure sentinel --
        # the long-standing safety net: never silently drop a photo we're
        # not sure about, only ones we're confidently sure AREN'T a
        # document (handled above).
        _handle_bill_message(reporter, message_id, file_bytes, media_type, is_group, reply_token)


def _handle_non_document_photo(reporter: str, message_id: str) -> None:
    """
    A confident not_a_document read (or the human explicitly tapping "not
    a bill" on the picker) -- a reference/context photo with nothing to
    extract: a truck itself, someone mid-repair, an odometer, a
    handwritten checklist with no cost figures. Deliberately a total
    no-op beyond logging -- no reply (these routinely arrive in batches of
    several alongside the real receipt; a "got it, not a bill" ping for
    every one would be far noisier than useful), no DB row, no Sheets
    sync, no push to the manager. If this turns out wrong for a specific
    photo, the sender still has the real bill/slip/supply flow available
    any time by sending that photo again and picking the right option
    from the picker (confidence == "low" always asks, see
    _handle_photo_message).
    """
    logger.info("message_id=%s from %s classified as not_a_document -- skipping, no action taken", message_id, reporter)


def _send_document_type_picker(reporter: str, message_id: str, media_type: str) -> None:
    """
    Pushed (not replied -- the one-time reply token is already spent on
    the "processing your photo" ack sent moments ago in
    _handle_photo_message) to whichever of Ohm/Mom sent the photo.
    Tapping a button sends the encoded command text as an ordinary
    message, intercepted in handle_message_event before it ever reaches
    the text classifier -- see _handle_document_type_confirmation, which
    re-downloads the photo via message_id rather than trying to persist
    the bytes anywhere in the meantime.
    """
    options = [
        ("บิลซ่อมรถ", "repair_bill"),
        ("สลิปโอนเงิน", "payment_slip"),
        ("บิลซื้ออะไหล่", "supply_purchase"),
        ("ไม่ใช่บิล/สลิป", "not_a_document"),
    ]
    quick_reply = [
        QuickReplyOption(label=label, text=f"{DOCUMENT_TYPE_COMMAND_PREFIX}{doc_type}:{media_type}:{message_id}")
        for label, doc_type in options
    ]
    push_message(_line_user_id_for(reporter), [strings.document_type_picker_prompt()], quick_reply=quick_reply)


def _handle_document_type_confirmation(reporter: str, command: str, reply_token: str) -> None:
    try:
        _, doc_type, media_type, message_id = command.split(":", 3)
    except ValueError:
        logger.warning("malformed document-type confirmation command: %r", command)
        return

    try:
        file_bytes = download_message_content(message_id)
    except Exception:
        logger.exception("failed to re-download message_id=%s for document-type confirmation", message_id)
        reply_message(reply_token, [strings.bill_extraction_failed()])
        return

    _route_photo_by_type(reporter, doc_type, message_id, file_bytes, media_type, is_group=False, reply_token=reply_token)


def _handle_finalize_chain_command(reporter: str, command: str, reply_token: str) -> None:
    """
    A tap on the "ไม่มีหน้าต่อแล้ว" button attached to an awaiting-next-
    page notification -- force-closes that specific chain with whatever
    pages it already has, then runs the exact same "ready for review"
    notification the normal flow would send once a real final page
    arrives. See bills.finalize_chain / supplies.finalize_chain for the
    scoping/safety rules (only the sender's own still-open chain can be
    closed this way -- a stale tap on an already-resolved chain, e.g. the
    real next page arrived in the meantime, is a no-op with a clear
    reply, not a silent double-notification or a crash).
    """
    try:
        payload = command[len(FINALIZE_CHAIN_COMMAND_PREFIX):]
        doc_type, chain_id = payload.split(":", 1)
    except ValueError:
        logger.warning("malformed finalize-chain command: %r", command)
        return

    if doc_type == "bill":
        bill = bills.finalize_chain(chain_id, reporter)
        if bill is None:
            reply_message(reply_token, [strings.finalize_chain_not_found()])
            return
        reply_message(reply_token, [strings.finalize_chain_confirmed()])
        _notify_bill_ready_for_review(bill, strings.chain_force_closed_note())
    elif doc_type == "supply":
        purchase = supplies.finalize_chain(chain_id, reporter)
        if purchase is None:
            reply_message(reply_token, [strings.finalize_chain_not_found()])
            return
        reply_message(reply_token, [strings.finalize_chain_confirmed()])
        _notify_supply_purchase_ready_for_review(purchase, strings.chain_force_closed_note())
    else:
        logger.warning("unknown finalize-chain doc_type in command: %r", command)


def _handle_bill_message(
    reporter: str, message_id: str, file_bytes: bytes, media_type: str, is_group: bool, reply_token: str
) -> None:
    """
    Extracts a photographed bill or PDF (bytes already downloaded by
    _handle_photo_message) and files it for review. See that function's
    docstring for the never-reply-into-a-group rule.

    Multi-page bills: LINE delivers a multi-select gallery send as
    consecutive events from the same sender, so bills.find_open_chain
    picks up right where the last photo left off. See bills.py for the
    actual chain/merge logic.
    """
    try:
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
        # Still an open chain awaiting its next page -- the sender gets
        # told this explicitly (used to be a silent return here: nothing
        # sent, nothing logged anywhere they could see, so "still waiting
        # for another page" was indistinguishable from "broke silently").
        # A quick-reply lets them force it closed right now if there
        # genuinely isn't a next page coming -- see
        # _handle_finalize_chain_command. Next photo (or the 30-minute
        # staleness window) is still the normal path; this is just the
        # escape hatch. See bills.find_open_chain.
        logger.info("bill %s still awaiting next page from %s", bill["bill_id"], reporter)
        quick_reply = [QuickReplyOption(
            label=strings.FINALIZE_CHAIN_BUTTON_LABEL,
            text=f"{FINALIZE_CHAIN_COMMAND_PREFIX}bill:{bill['bill_id']}",
        )]
        push_message(_line_user_id_for(reporter), [strings.bill_awaiting_next_page(bill)], quick_reply=quick_reply)
        return

    _notify_bill_ready_for_review(bill, note)


def _notify_bill_ready_for_review(bill: dict, note: str | None) -> None:
    """Shared by the normal extraction flow above and
    _handle_finalize_chain_command (manually force-closing a chain that
    never got its next page)."""
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


def _handle_slip_message(
    reporter: str, message_id: str, file_bytes: bytes, is_group: bool, reply_token: str
) -> None:
    """
    Extracts a photographed bank-transfer slip (bytes already downloaded
    by _handle_photo_message) and files it for review. No multi-page
    chaining here -- a slip is always a single transaction on one photo,
    unlike a bill's line items. media_type is always 'image/jpeg': slips
    only ever arrive as photos in practice (a PDF bank statement isn't a
    single-transaction slip), so this flow doesn't handle
    'application/pdf' -- _handle_photo_message would only route a PDF here
    if the classifier itself decided a PDF was a payment_slip, which isn't
    expected but is handled the same as any other slip if it happens.
    """
    try:
        extracted = extract_slip(file_bytes, "image/jpeg")
    except Exception:
        logger.exception("slip extraction failed for message_id=%s from %s", message_id, reporter)
        if not is_group:
            reply_message(reply_token, [strings.slip_extraction_failed()])
        push_message(
            settings.ohm_line_user_id,
            [f"⚠️ อ่านสลิปไม่สำเร็จ (จาก {reporter}, message_id={message_id}) เช็ค log ดูนะครับ"],
        )
        return

    logger.info(
        "slip extracted OK: to=%r amount=%r branch=%r",
        extracted.get("to_display_name"), extracted.get("amount"), extracted.get("branch"),
    )

    slip_id = slips.create_slip(reporter, extracted, message_id)
    slip = slips.get_slip(slip_id)
    logger.info("created new slip %s from %s", slip_id, reporter)

    review_url = f"{settings.public_base_url}/slips/{slip_id}?token={settings.review_token}"
    logger.info("slip %s ready for review, notifying manager: %s", slip_id, review_url)
    push_message(settings.ohm_line_user_id, [strings.slip_ready_for_review(slip, review_url)])


def _handle_supply_purchase_message(
    reporter: str, message_id: str, file_bytes: bytes, media_type: str, is_group: bool, reply_token: str
) -> None:
    """
    Extracts a photographed supplier bill for parts/supplies (bytes
    already downloaded by _handle_photo_message) and files it for review.
    Same shape as _handle_bill_message (multi-page chaining via
    supplies.find_open_chain, same never-reply-into-a-group rule), no
    vehicle-roster-match warning since this isn't tied to one vehicle.
    """
    try:
        extracted = extract_supply_purchase(
            file_bytes, media_type, known_canonical_parts=supplies.get_known_canonical_parts()
        )
    except Exception:
        logger.exception("supply purchase extraction failed for message_id=%s from %s", message_id, reporter)
        if not is_group:
            reply_message(reply_token, [strings.bill_extraction_failed()])
        push_message(
            settings.ohm_line_user_id,
            [f"⚠️ อ่านบิลซื้ออะไหล่ไม่สำเร็จ (จาก {reporter}, message_id={message_id}) เช็ค log ดูนะครับ"],
        )
        return

    logger.info(
        "extracted OK: supplier=%r total=%r items=%d continues_next_page=%s",
        extracted.get("supplier_name"), extracted.get("total_cost"),
        len(extracted.get("line_items") or []), extracted.get("continues_next_page"),
    )

    note = None
    open_chain = supplies.find_open_chain(reporter)
    if open_chain is not None:
        logger.info("merging into open chain %s", open_chain["purchase_id"])
        result = supplies.append_page_to_chain(open_chain["purchase_id"], extracted, message_id)
        purchase = result["purchase"]
        if not result["totals_reconciled"]:
            note = strings.bill_totals_mismatch_note(result["combined_sum"], result["final_total"])
            logger.warning(
                "supply purchase %s totals mismatch: combined=%s final=%s",
                purchase["purchase_id"], result["combined_sum"], result["final_total"],
            )
    else:
        purchase_id = supplies.create_purchase(reporter, extracted, message_id)
        purchase = supplies.get_purchase(purchase_id)
        logger.info("created new supply purchase %s from %s", purchase_id, reporter)

    if purchase.get("continues_next_page"):
        # See _handle_bill_message's identical branch for why this pushes
        # a visible message + escape-hatch button instead of silently
        # returning.
        logger.info("supply purchase %s still awaiting next page from %s", purchase["purchase_id"], reporter)
        quick_reply = [QuickReplyOption(
            label=strings.FINALIZE_CHAIN_BUTTON_LABEL,
            text=f"{FINALIZE_CHAIN_COMMAND_PREFIX}supply:{purchase['purchase_id']}",
        )]
        push_message(
            _line_user_id_for(reporter), [strings.supply_purchase_awaiting_next_page(purchase)], quick_reply=quick_reply
        )
        return

    _notify_supply_purchase_ready_for_review(purchase, note)


def _notify_supply_purchase_ready_for_review(purchase: dict, note: str | None) -> None:
    """Shared by the normal extraction flow above and
    _handle_finalize_chain_command."""
    review_url = f"{settings.public_base_url}/supplies/{purchase['purchase_id']}?token={settings.review_token}"
    logger.info("supply purchase %s ready for review, notifying manager: %s", purchase["purchase_id"], review_url)
    push_message(settings.ohm_line_user_id, [strings.supply_purchase_ready_for_review(purchase, review_url, note)])


def _ticket_picker_label(ticket: dict) -> str:
    text = ticket.get("summary") or ticket["message"]
    label = f"#{ticket['id']} {text}"
    return label if len(label) <= 20 else label[:19] + "…"

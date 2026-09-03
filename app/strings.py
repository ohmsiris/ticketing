"""
All Thai (and Thai/English) copy the bot sends, in one place.

This is a first draft -- wording and politeness level are worth tuning once
the pilot is actually running. Keeping every string here (instead of spread
across the handler logic) makes that easy to do later without touching any
behavior code.
"""

# --- Welcome (sent once, on LINE's "follow" event -- see handle_follow_event
# in app/webhook_handler.py) ---


def welcome_message() -> str:
    return (
        "บอทตั๋วงานตอนนี้ทำอะไรได้บ้าง สรุปสั้นๆ ครับ:\n\n"
        "1. แจ้งปัญหา — พิมพ์บอกเป็นภาษาปกติได้เลย ระบบจะบันทึกเป็นงานให้อัตโนมัติ "
        "พร้อมบอกว่าเป็นงานหมวดไหน (รถ/เครื่องจักร/พนักงาน/อื่นๆ)\n"
        "2. ตั้งกำหนดวัน — บอกได้เลย เช่น \"อีก 3 วัน\" หรือ \"25 ส.ค.\" "
        "(ไม่ใส่ปีก็ได้ ระบบเดาปีให้เอง)\n"
        "3. ขอให้เตือนล่วงหน้า — ถ้าอยากให้เตือนก่อนถึงกำหนดกี่วัน พิมพ์บอกได้ "
        "เช่น \"เตือนก่อน 3 วัน\"\n"
        "4. ปิดงานที่เสร็จแล้ว — พิมพ์บอกว่าอะไรเสร็จ ไม่ต้องจำเลขงานเลย "
        "เช่น \"เปลี่ยนน้ำมันเสร็จแล้ว\" ระบบจะเดาให้เองว่าเป็นงานไหน "
        "ถ้าไม่ชัดเจนจะมีปุ่มให้กดเลือก ไม่ต้องพิมพ์เพิ่ม\n"
        "5. ยกเลิกงานที่พิมพ์ผิดหรือเปลี่ยนใจ — พิมพ์ \"ยกเลิก\" ได้เลย "
        "(ยกเลิกได้ทีละงานเท่านั้นนะครับ ไม่ยกเลิกทั้งหมดพร้อมกัน)\n"
        "6. ตอนนี้ยังไม่รองรับข้อความเสียง พิมพ์แทนได้เลยนะครับ"
    )


# --- Ticket capture ---


def new_ticket_confirmation(ticket_id: int, department: str) -> str:
    return f"✅ บันทึกแล้ว (#{ticket_id}) แผนก: {department}"


def new_ticket_confirmation_with_due_date(
    ticket_id: int, message: str, department: str, due_date: str, remind_days_before: int | None = None
) -> str:
    """Used when the due date was already stated in the same message that
    created the ticket, so we skip asking for it separately."""
    text = f"✅ บันทึกแล้ว (#{ticket_id}) แผนก: {department}\n{_snippet(message)}\n📅 กำหนด: {due_date}"
    return text + _remind_days_before_line(remind_days_before)


def new_ticket_notify_hq(
    ticket_id: int,
    reporter: str,
    department: str,
    message: str,
    due_date: str | None = None,
    remind_days_before: int | None = None,
) -> str:
    """
    Real-time push to Ohm (HQ) whenever anyone else's message creates a
    ticket -- separate from that person's own confirmation reply, and
    separate from the scheduled digests (which only resurface what's
    still open later, not "this just happened"). See _handle_new_ticket
    in webhook_handler.py, which skips this when Ohm is the reporter --
    no need to tell him about his own message.
    """
    lines = [f"🆕 งานใหม่จาก {_reporter_label(reporter)} (#{ticket_id}) แผนก: {department}", _snippet(message, 120)]
    if due_date:
        lines.append(f"📅 กำหนด: {due_date}")
    return "\n".join(lines) + _remind_days_before_line(remind_days_before)


def ask_due_date() -> str:
    return "มีกำหนดวันไหมครับ? บอกเป็นจำนวนวัน หรือระบุวันที่ก็ได้"


def due_date_set(ticket_id: int, message: str, due_date: str, remind_days_before: int | None = None) -> str:
    text = f"✅ บันทึกแล้ว (#{ticket_id})\n{_snippet(message)}\n📅 กำหนด: {due_date}"
    return text + _remind_days_before_line(remind_days_before)


def due_date_set_notify_hq(
    ticket_id: int, reporter: str, message: str, due_date: str, remind_days_before: int | None = None
) -> str:
    """Real-time push to Ohm whenever anyone else sets/changes a due date
    -- same idea as new_ticket_notify_hq, see _handle_due_date_reply."""
    text = f"🗓️ ตั้งกำหนดวันจาก {_reporter_label(reporter)} (#{ticket_id})\n{_snippet(message, 120)}\n📅 กำหนด: {due_date}"
    return text + _remind_days_before_line(remind_days_before)


def _remind_days_before_line(remind_days_before: int | None) -> str:
    if remind_days_before is None:
        return ""
    return f"\n🔔 จะเตือนล่วงหน้า {remind_days_before} วันก่อนถึงกำหนด"


def remind_days_before_set(ticket_id: int, message: str, remind_days_before: int) -> str:
    """Confirmation for a standalone reminder-lead-time message, not attached
    to setting/changing the due date itself."""
    return f"🔔 ตั้งเตือนล่วงหน้าแล้ว (#{ticket_id})\n{_snippet(message)}\nจะเตือนก่อนถึงกำหนด {remind_days_before} วัน"


def remind_days_before_set_notify_hq(ticket_id: int, reporter: str, message: str, remind_days_before: int) -> str:
    """Real-time push to Ohm for the standalone reminder-lead-time case
    (not attached to a due-date change) -- see _handle_due_date_reply."""
    return (
        f"🔔 ตั้งเตือนล่วงหน้าจาก {_reporter_label(reporter)} (#{ticket_id})\n"
        f"{_snippet(message, 120)}\nเตือนก่อนถึงกำหนด {remind_days_before} วัน"
    )


def no_ticket_for_reminder() -> str:
    return "ไม่พบงานที่เปิดอยู่ให้ตั้งเตือนครับ ถ้าอยากแจ้งปัญหาใหม่ พิมพ์บอกได้เลยครับ"


def no_ticket_needs_due_date() -> str:
    return "ไม่พบงานที่ยังไม่ได้ตั้งกำหนดวันครับ ถ้าอยากแจ้งปัญหาใหม่ พิมพ์บอกได้เลยครับ"


def due_date_unclear() -> str:
    return "ขอวันที่อีกครั้งได้ไหมครับ เช่น 'อีก 3 วัน' หรือ '25 ส.ค.'"


def due_date_in_past(due_date: str) -> str:
    return f"วันที่ {due_date} ผ่านไปแล้วครับ ขอวันที่ในอนาคตได้ไหมครับ"


# --- Closing tickets ---


def ticket_closed(ticket_id: int, message: str) -> str:
    return f"ปิดงาน #{ticket_id} แล้ว\n{_snippet(message)}"


def ticket_closed_notify_hq(ticket_id: int, reporter: str, message: str) -> str:
    """Real-time push to Ohm whenever anyone else closes a ticket -- same
    idea as new_ticket_notify_hq, see _handle_close_ticket. Fires
    regardless of whose ticket it was (the shared รถ/พนักงาน/อื่นๆ pool
    means the actor and the original reporter can differ)."""
    return f"☑️ ปิดงานจาก {_reporter_label(reporter)} (#{ticket_id})\n{_snippet(message, 120)}"


def no_open_ticket_to_close() -> str:
    return "ไม่มีงานที่เปิดอยู่ครับ"


def ticket_not_found_or_closed(ticket_id: int) -> str:
    return f"ไม่พบทิกเก็ต #{ticket_id} หรือถูกปิดไปแล้วครับ"


def close_ticket_picker_prompt() -> str:
    return "จะปิดงานไหนครับ? เลือกจากด้านล่างได้เลย"


def close_ticket_no_match_prompt() -> str:
    return "ไม่พบงานที่ตรงกับที่พิมพ์มาครับ นี่คืองานที่เปิดอยู่ตอนนี้ เลือกได้เลยถ้าใช่งานใดงานหนึ่ง"


# --- Cancelling tickets -- distinct from closing: closing means the work
# got done, cancelling means the ticket itself is being withdrawn (mistake,
# changed their mind, etc). Always exactly one ticket per message -- there
# is no "cancel everything" anywhere in this app, by design. ---


def ticket_cancelled(ticket_id: int, message: str) -> str:
    return f"ยกเลิกงาน #{ticket_id} แล้ว\n{_snippet(message)}"


def ticket_cancelled_notify_hq(ticket_id: int, reporter: str, message: str) -> str:
    """Real-time push to Ohm whenever anyone else cancels a ticket -- same
    idea as new_ticket_notify_hq, see _handle_cancel_ticket. Fires
    regardless of whose ticket it was, same reasoning as
    ticket_closed_notify_hq."""
    return f"🚫 ยกเลิกงานจาก {_reporter_label(reporter)} (#{ticket_id})\n{_snippet(message, 120)}"


# --- Ticket management webpage (app/tickets_routes.py) -- pushed to the
# ORIGINAL REPORTER (not HQ) whenever the manager edits/closes/cancels/
# reopens their ticket from /tickets, skipped entirely when the manager
# IS the reporter (no point notifying yourself of your own web edit). ---


def ticket_closed_by_admin(ticket_id: int, message: str) -> str:
    return f"☑️ งาน #{ticket_id} ถูกปิดแล้วครับ (ปิดผ่านหน้าเว็บจัดการทิกเก็ต)\n{_snippet(message)}"


def ticket_cancelled_by_admin(ticket_id: int, message: str) -> str:
    return f"🚫 งาน #{ticket_id} ถูกยกเลิกแล้วครับ (ยกเลิกผ่านหน้าเว็บจัดการทิกเก็ต)\n{_snippet(message)}"


def ticket_reopened_by_admin(ticket_id: int, message: str) -> str:
    return f"🔓 งาน #{ticket_id} ถูกเปิดใหม่อีกครั้งครับ (ผ่านหน้าเว็บจัดการทิกเก็ต)\n{_snippet(message)}"


def ticket_updated_by_admin(ticket_id: int, message: str) -> str:
    return f"🔄 งาน #{ticket_id} มีการแก้ไขข้อมูลครับ (ผ่านหน้าเว็บจัดการทิกเก็ต)\n{_snippet(message)}"


def no_open_ticket_to_cancel() -> str:
    return "ไม่มีงานที่เปิดอยู่ให้ยกเลิกครับ"


def cancel_ticket_picker_prompt() -> str:
    return "จะยกเลิกงานไหนครับ? เลือกจากด้านล่างได้เลย"


# --- Preventive maintenance (recurring tasks -- see app/maintenance.py) ---


def maintenance_done_confirmation(task_name: str, days_ago: int | None = None, calendar_date: str | None = None) -> str:
    """days_ago / calendar_date: from classify()'s maintenance_completed_*
    fields -- shown so a backdated report ("เมื่อวานล้างฟรีซหลอด 2 เสร็จ",
    or "...เมื่อวันที่ 15 มิถุนายน") gets clear confirmation it was
    actually logged against that date, not today. At most one of the two
    is ever set (see classifier.py)."""
    if calendar_date:
        return f"✅ บันทึกแล้ว: {task_name} ({calendar_date})"
    if not days_ago:
        return f"✅ บันทึกแล้ว: {task_name}"
    when = "เมื่อวาน" if days_ago == 1 else f"{days_ago} วันที่แล้ว"
    return f"✅ บันทึกแล้ว: {task_name} ({when})"


def maintenance_task_not_found() -> str:
    return "ไม่พบรายการนี้ในระบบครับ อาจมีการเปลี่ยนแปลงรายการ ลองพิมพ์ใหม่อีกครั้ง"


def maintenance_due_digest(tasks: list[dict]) -> str:
    """tasks: list of dicts with name, category, days_overdue (from
    maintenance.get_due_tasks) -- grouped by category, most overdue first
    within each group."""
    lines = [f"🔧 งานบำรุงรักษาที่ถึงกำหนด {len(tasks)} รายการ:"]
    by_category: dict[str, list[dict]] = {}
    for t in tasks:
        by_category.setdefault(t["category"], []).append(t)
    for category, group in by_category.items():
        lines.append(f"\n{category}")
        for t in group:
            when = "ถึงกำหนดวันนี้" if t["days_overdue"] == 0 else f"เลยกำหนดมา {t['days_overdue']} วัน"
            lines.append(f"- {t['name']} ({when})")
    return "\n".join(lines)


# --- Unsupported input ---


def voice_not_supported() -> str:
    return "ตอนนี้ระบบยังไม่รองรับข้อความเสียง พิมพ์บอกแทนได้ไหมครับ"


# --- Confidently not a report (greeting, small talk, unrelated question) --
# see classify()'s "other" intent in app/classifier.py ---


def unclear_message() -> str:
    return "สวัสดีครับ 🙂 ถ้ามีปัญหาที่ต้องการแจ้ง พิมพ์บอกได้เลยครับ"


# --- Mom's onboarding (appended to the end of her confirmation message
# while ticket_count is low; see app/webhook_handler.py) ---


def onboarding_first_ticket() -> str:
    return (
        "พิมพ์บอกปัญหาที่เจอเป็นภาษาปกติได้เลยครับ ระบบจะบันทึกให้อัตโนมัติ "
        "ถ้ามีกำหนดวันที่ต้องเสร็จ บอกได้เลย เช่น 'อีก 3 วัน' หรือระบุวันที่ก็ได้"
    )


def onboarding_reminder_tip() -> str:
    return "(ทิป: บอกกำหนดวันเพิ่มได้ถ้ามีนะครับ)"


# --- Digests (sent to Ohm only) ---


def _reporter_label(reporter: str) -> str:
    return "ohm" if reporter == "ohm" else "mom"


def _snippet(message: str, max_len: int = 40) -> str:
    message = message.strip().replace("\n", " ")
    return message if len(message) <= max_len else message[: max_len - 1] + "…"


def _duration_label(hours_open: float) -> str:
    total_minutes = int(hours_open * 60)
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} วัน")
    if hours:
        parts.append(f"{hours} ชม.")
    if not days and not hours:
        parts.append(f"{minutes} นาที")
    return " ".join(parts)


def _display_text(t: dict) -> str:
    """summary (cleaned up by the classifier) if we have one, else the raw message."""
    return t.get("summary") or t["message"]


def open_tickets_digest(tickets: list[dict]) -> str:
    """tickets: list of dicts with id, reporter, department, message, summary, hours_open."""
    if not tickets:
        return "ไม่มีงานค้างอยู่ตอนนี้ครับ"
    lines = [f"📋 มีงานค้างอยู่ {len(tickets)} รายการ:"]
    for t in tickets:
        lines.append(
            f"{t['id']}. [{_reporter_label(t['reporter'])}/{t['department']}] "
            f"{_snippet(_display_text(t))} (เปิดมา {_duration_label(t['hours_open'])})"
        )
    return "\n".join(lines)


def mom_shared_reminder_digest(tickets: list[dict]) -> str:
    """tickets: list of dicts with id, reporter, department, message, summary,
    due_date -- every open ticket outside เครื่องจักร (machinery), soonest due
    date first. Sent to Mom 3x/day, whoever originally reported each one
    (see jobs.py). Department shown per line since this now spans more
    than one (รถ/พนักงาน/อื่นๆ), unlike the old car-only version."""
    lines = [f"📋 งานที่ยังค้างอยู่ {len(tickets)} รายการ:"]
    for t in tickets:
        due = f"กำหนด: {t['due_date']}" if t["due_date"] else "ไม่มีกำหนด"
        lines.append(
            f"#{t['id']} [{_reporter_label(t['reporter'])}/{t['department']}] {_snippet(_display_text(t))} ({due})"
        )
    return "\n".join(lines)


def all_open_tickets_digest(tickets: list[dict]) -> str:
    """tickets: list of dicts with id, reporter, department, message, summary,
    due_date -- every currently open ticket, soonest due date first (see
    get_all_open_tickets_by_due_date). Powers the "งานทั้งหมด" command."""
    if not tickets:
        return "ไม่มีงานค้างอยู่ตอนนี้ครับ"
    lines = [f"📋 งานทั้งหมด {len(tickets)} รายการ:"]
    for t in tickets:
        due = f"กำหนด: {t['due_date']}" if t["due_date"] else "ไม่มีกำหนด"
        lines.append(
            f"#{t['id']} [{_reporter_label(t['reporter'])}/{t['department']}] "
            f"{_snippet(_display_text(t))} ({due})"
        )
    return "\n".join(lines)


def admin_links_message(tickets_url: str, bills_url: str, slips_url: str, supplies_url: str) -> str:
    """The "ลิงก์" on-demand command (Ohm only, see webhook_handler.py) --
    a quick way to get every management-page link without digging through
    Railway/notes. URLs are built by the caller (needs settings.
    public_base_url + the right token per page) and just handed in here,
    same as review_url elsewhere in this file -- keeps this module free
    of any app.config dependency."""
    return (
        "🔗 ลิงก์หน้าจัดการ:\n"
        f"ทิกเก็ต: {tickets_url}\n"
        f"บิลซ่อมรถ: {bills_url}\n"
        f"สลิปโอนเงิน: {slips_url}\n"
        f"บิลซื้ออะไหล่: {supplies_url}"
    )


def due_today_digest(tickets: list[dict]) -> str:
    """tickets: list of dicts with id, reporter, department, message, summary."""
    lines = [f"📅 งานที่ครบกำหนดวันนี้ {len(tickets)} รายการ:"]
    for t in tickets:
        lines.append(f"#{t['id']} [{_reporter_label(t['reporter'])}/{t['department']}] {_snippet(_display_text(t))}")
    return "\n".join(lines)


def due_soon_digest(tickets: list[dict]) -> str:
    """tickets: list of dicts with id, reporter, department, message, summary,
    due_date, remind_days_before -- the requested heads-up lead time."""
    lines = [f"🔔 งานที่ใกล้ครบกำหนด {len(tickets)} รายการ:"]
    for t in tickets:
        lines.append(
            f"#{t['id']} [{_reporter_label(t['reporter'])}/{t['department']}] "
            f"{_snippet(_display_text(t))} (ครบกำหนด {t['due_date']}, อีก {t['remind_days_before']} วัน)"
        )
    return "\n".join(lines)


# --- Bill tracking ---
#
# The immediate "reading it now" ack shared by both bill and slip photos
# is photo_processing_ack(), below the payment-slip section -- it's sent
# before app.image_classifier knows which flow this is, so it isn't
# specific to either one.


def bill_ready_for_review(bill: dict, review_url: str, note: str | None = None) -> str:
    vehicle_bits = [b for b in (bill.get("vehicle_license"), bill.get("vehicle_number")) if b]
    vehicle_label = " / ".join(vehicle_bits) if vehicle_bits else "(ไม่ทราบทะเบียน)"
    lines = [
        "✅ บิลใหม่พร้อมตรวจสอบ",
        f"ร้าน: {bill.get('shop_name') or '(ไม่ทราบ)'}",
        f"รถ: {vehicle_label}",
        f"สาขา: {bill.get('branch') or '(ไม่ทราบ)'}",
        f"ยอดรวม: {bill.get('total_cost') or 0} บาท",
    ]
    if note:
        lines.append(f"⚠️ {note}")
    lines.append(f"ตรวจสอบที่: {review_url}")
    return "\n".join(lines)


def bill_totals_mismatch_note(combined_sum: float, final_total: float) -> str:
    return (
        f"ยอดรวมของหลายหน้าบวกกันได้ {combined_sum:g} แต่หน้าสุดท้ายเขียนว่า {final_total:g} "
        f"— อาจมีตัวเลขอ่านผิดสักรายการ ลองเช็คอีกครั้ง"
    )


def bill_unsupported_file() -> str:
    return "รองรับเฉพาะรูปถ่ายบิล หรือไฟล์ PDF นะครับ ไฟล์นี้ยังไม่รองรับ"


# --- Supply/parts purchase tracking (see app/supplies.py, app/supply_extraction.py) ---


def supply_purchase_ready_for_review(purchase: dict, review_url: str, note: str | None = None) -> str:
    lines = [
        "✅ บิลซื้ออะไหล่ใหม่พร้อมตรวจสอบ",
        f"ร้าน: {purchase.get('supplier_name') or '(ไม่ทราบ)'}",
        f"สาขา: {purchase.get('branch') or '(ไม่ทราบ)'}",
        f"ยอดรวม: {purchase.get('total_cost') or 0} บาท",
    ]
    if note:
        lines.append(f"⚠️ {note}")
    lines.append(f"ตรวจสอบที่: {review_url}")
    return "\n".join(lines)


# --- Multi-page chains: awaiting-next-page visibility + the manual
# "no more pages" escape hatch (see FINALIZE_CHAIN_COMMAND_PREFIX /
# _handle_finalize_chain_command in app/webhook_handler.py). Before this,
# a bill/purchase silently sitting in "awaiting next page" state was
# completely invisible to the sender -- they had no way to tell "still
# processing" apart from "broke, no output at all". ---

FINALIZE_CHAIN_BUTTON_LABEL = "ไม่มีหน้าต่อแล้ว"


def bill_awaiting_next_page(bill: dict) -> str:
    shop = bill.get("shop_name") or "(ไม่ทราบชื่อร้าน)"
    return (
        f"📄 ได้รับรูปบิลจากร้าน {shop} แล้วครับ แต่ยังไม่เห็นยอดรวม ดูเหมือนบิลนี้จะมีต่ออีกหน้า\n"
        "ส่งรูปหน้าถัดไปได้เลย หรือกดปุ่มด้านล่างถ้าไม่มีหน้าต่อแล้ว"
    )


def supply_purchase_awaiting_next_page(purchase: dict) -> str:
    supplier = purchase.get("supplier_name") or "(ไม่ทราบชื่อร้าน)"
    return (
        f"📄 ได้รับรูปบิลซื้ออะไหล่จากร้าน {supplier} แล้วครับ แต่ยังไม่เห็นยอดรวม ดูเหมือนบิลนี้จะมีต่ออีกหน้า\n"
        "ส่งรูปหน้าถัดไปได้เลย หรือกดปุ่มด้านล่างถ้าไม่มีหน้าต่อแล้ว"
    )


def finalize_chain_confirmed() -> str:
    return "รับทราบครับ ปิดบิลตามที่มีอยู่ตอนนี้ และส่งให้ตรวจสอบแล้ว"


def finalize_chain_not_found() -> str:
    return "ไม่พบบิลที่รอหน้าต่ออยู่ตอนนี้ครับ (อาจถูกปิดไปแล้ว หรือมีหน้าต่อเข้ามาแล้ว)"


def chain_force_closed_note() -> str:
    return "ปิดบิลเองโดยไม่รอหน้าต่อ อาจไม่ครบทุกรายการ -- ตรวจสอบให้ดีก่อนยืนยัน"


# --- Photo-type disambiguation (see _send_document_type_picker in app/webhook_handler.py) ---


def document_type_picker_prompt() -> str:
    return "ไม่แน่ใจว่าเป็นเอกสารประเภทไหนครับ เลือกได้เลย"


def bill_extraction_failed() -> str:
    return "อ่านบิลนี้ไม่สำเร็จ ลองส่งใหม่อีกครั้ง หรือถ่ายรูปให้ชัดขึ้นนะครับ"


# --- Payment slip tracking ---


def photo_processing_ack() -> str:
    """Sent for EVERY incoming image/file, before app.image_classifier has
    decided whether it's a bill or a slip -- see webhook_handler.py's
    _handle_photo_message. Deliberately generic wording since the type
    isn't known yet at this point."""
    return "📷 กำลังอ่านรูป รอสักครู่นะครับ"


def slip_ready_for_review(slip: dict, review_url: str) -> str:
    branch_label = {"SRB": "สระบุรี", "KK": "แก่งคอย"}.get(slip.get("branch") or "", "(ไม่ทราบ)")
    lines = [
        "✅ สลิปใหม่พร้อมตรวจสอบ",
        f"ถึง: {slip.get('to_display_name') or '(ไม่ทราบ)'}",
        f"จำนวนเงิน: {slip.get('amount') or 0} บาท",
        f"สาขา (ตามบัญชีที่จ่าย): {branch_label}",
    ]
    warning = (slip.get("account_match_warning") or "").strip()
    if warning:
        lines.append(f"⚠️ {warning}")
    cross_branch = (slip.get("cross_branch_note") or "").strip()
    if cross_branch:
        lines.append(f"ℹ️ {cross_branch}")
    lines.append(f"ตรวจสอบที่: {review_url}")
    return "\n".join(lines)


def slip_extraction_failed() -> str:
    return "อ่านสลิปนี้ไม่สำเร็จ ลองส่งใหม่อีกครั้ง หรือถ่ายรูปให้ชัดขึ้นนะครับ"

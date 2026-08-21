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


def ask_due_date() -> str:
    return "มีกำหนดวันไหมครับ? บอกเป็นจำนวนวัน หรือระบุวันที่ก็ได้"


def due_date_set(ticket_id: int, message: str, due_date: str, remind_days_before: int | None = None) -> str:
    text = f"✅ บันทึกแล้ว (#{ticket_id})\n{_snippet(message)}\n📅 กำหนด: {due_date}"
    return text + _remind_days_before_line(remind_days_before)


def _remind_days_before_line(remind_days_before: int | None) -> str:
    if remind_days_before is None:
        return ""
    return f"\n🔔 จะเตือนล่วงหน้า {remind_days_before} วันก่อนถึงกำหนด"


def remind_days_before_set(ticket_id: int, message: str, remind_days_before: int) -> str:
    """Confirmation for a standalone reminder-lead-time message, not attached
    to setting/changing the due date itself."""
    return f"🔔 ตั้งเตือนล่วงหน้าแล้ว (#{ticket_id})\n{_snippet(message)}\nจะเตือนก่อนถึงกำหนด {remind_days_before} วัน"


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


def no_open_ticket_to_cancel() -> str:
    return "ไม่มีงานที่เปิดอยู่ให้ยกเลิกครับ"


def cancel_ticket_picker_prompt() -> str:
    return "จะยกเลิกงานไหนครับ? เลือกจากด้านล่างได้เลย"


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


def mom_car_reminder_digest(tickets: list[dict]) -> str:
    """tickets: list of dicts with id, reporter, message, summary, due_date --
    every open รถ-department ticket, soonest due date first. Sent to Mom
    daily at 08:00, whoever originally reported each one (see jobs.py)."""
    lines = [f"🚗 งานรถที่ยังค้างอยู่ {len(tickets)} รายการ:"]
    for t in tickets:
        due = f"กำหนด: {t['due_date']}" if t["due_date"] else "ไม่มีกำหนด"
        lines.append(f"#{t['id']} [{_reporter_label(t['reporter'])}] {_snippet(_display_text(t))} ({due})")
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


def bill_processing_ack() -> str:
    """Sent as an immediate reply in a PRIVATE chat only (never into a
    group -- see webhook_handler.py) right when a photo/PDF arrives,
    since extraction takes several seconds and silence would be
    confusing."""
    return "📷 กำลังอ่านบิล รอสักครู่นะครับ"


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


def bill_extraction_failed() -> str:
    return "อ่านบิลนี้ไม่สำเร็จ ลองส่งใหม่อีกครั้ง หรือถ่ายรูปให้ชัดขึ้นนะครับ"

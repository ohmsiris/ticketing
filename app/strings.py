"""
All Thai (and Thai/English) copy the bot sends, in one place.

This is a first draft -- wording and politeness level are worth tuning once
the pilot is actually running. Keeping every string here (instead of spread
across the handler logic) makes that easy to do later without touching any
behavior code.
"""

# --- Ticket capture ---


def new_ticket_confirmation(ticket_id: int, department: str) -> str:
    return f"✅ บันทึกแล้ว (#{ticket_id}) แผนก: {department}"


def ask_due_date() -> str:
    return "มีกำหนดวันไหมครับ? บอกเป็นจำนวนวัน หรือระบุวันที่ก็ได้"


def due_date_set(ticket_id: int, due_date: str) -> str:
    return f"📅 ตั้งกำหนดวันแล้ว: #{ticket_id} → {due_date}"


def no_ticket_needs_due_date() -> str:
    return "ไม่พบงานที่ยังไม่ได้ตั้งกำหนดวันครับ ถ้าอยากแจ้งปัญหาใหม่ พิมพ์บอกได้เลยครับ"


def due_date_unclear() -> str:
    return "ขอวันที่อีกครั้งได้ไหมครับ เช่น 'อีก 3 วัน' หรือ '25 ส.ค.'"


# --- Closing tickets ---


def ticket_closed(ticket_id: int) -> str:
    return f"ปิดงาน #{ticket_id} แล้ว"


def no_open_ticket_to_close() -> str:
    return "ไม่มีงานที่เปิดอยู่ครับ"


def ticket_not_found_or_closed(ticket_id: int) -> str:
    return f"ไม่พบทิกเก็ต #{ticket_id} หรือถูกปิดไปแล้วครับ"


# --- Unsupported input ---


def voice_not_supported() -> str:
    return "ตอนนี้ระบบยังไม่รองรับข้อความเสียง พิมพ์บอกแทนได้ไหมครับ"


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


def open_tickets_digest(tickets: list[dict]) -> str:
    """tickets: list of dicts with id, reporter, department, message, hours_open."""
    if not tickets:
        return "ไม่มีงานค้างอยู่ตอนนี้ครับ"
    lines = [f"📋 มีงานค้างอยู่ {len(tickets)} รายการ:"]
    for t in tickets:
        lines.append(
            f"{t['id']}. [{_reporter_label(t['reporter'])}/{t['department']}] "
            f"{_snippet(t['message'])} (เปิดมา {_duration_label(t['hours_open'])})"
        )
    return "\n".join(lines)


def due_today_digest(tickets: list[dict]) -> str:
    """tickets: list of dicts with id, reporter, department, message."""
    lines = [f"📅 งานที่ครบกำหนดวันนี้ {len(tickets)} รายการ:"]
    for t in tickets:
        lines.append(f"#{t['id']} [{_reporter_label(t['reporter'])}/{t['department']}] {_snippet(t['message'])}")
    return "\n".join(lines)

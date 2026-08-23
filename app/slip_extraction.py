"""
Reads a photographed bank-transfer/payment slip via Claude vision and
returns structured data, enriched with a deterministic lookup against
app/bank_accounts.csv (ported from the Accounting project's
Account_Master.xlsx). Modeled directly on app/bill_extraction.py's shape
-- same base64/content-block plumbing, same "OCR guesses text, a roster
lookup resolves the rest deterministically" split.

The one piece of business logic baked in here, per the owner's explicit
call: branch attribution follows whoever PAID, not what the slip's memo
says the money was for. Saraburi paying a Kaeng Khoi expense is a Saraburi
cost. See _cross_branch_note() -- this is applied unconditionally, but
always surfaced to the reviewer so the cross-branch case is visible, not
silently absorbed.
"""
import base64
import csv
import json
import logging
from pathlib import Path
from typing import Optional

from anthropic import Anthropic

from app.config import settings

logger = logging.getLogger("ticketing.slip_extraction")

_client: Optional[Anthropic] = None


def _client_lazy() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


# Slip photos are clean app screenshots/printed receipts, not messy
# handwriting -- sonnet-5 (same tier as classifier.py) should be plenty
# accurate. Bump to claude-opus-5 (bill_extraction.py's choice) if
# real-world accuracy on amounts/digits ever proves it's needed.
MODEL = "claude-sonnet-5"

ACCOUNTS_ROSTER_PATH = Path(__file__).resolve().parent / "bank_accounts.csv"

# The same 40 P&L categories the Accounting Google Sheet's "Lists" tab
# offers as a dropdown (Lists!A2:A41) -- kept here as a plain constant, same
# pattern as bill_extraction.CATEGORIES. If the Sheet's list ever changes,
# update both places; there's no live sync between them (same tradeoff the
# Sheet's own Lists tab already accepted for the vehicle roster before
# roster_sync.py existed).
CATEGORIES = [
    "ค่าไฟ - มิเตอร์ 7916", "ค่าไฟ - มิเตอร์ 9615", "ค่าไฟ - มิเตอร์ 9331",
    "ค่าน้ำ - มิเตอร์ 09", "ค่าน้ำ - มิเตอร์ 18", "ค่าน้ำ - ชลประทาน",
    "ค่าสารเคมี - PAC & Chlorine", "ค่าสารเคมี - C200", "ค่าสารเคมี - แอมโมเนีย",
    "ค่าสารเคมี - น้ำมันคอมม์", "ค่าสารเคมี - เกลือ",
    "ค่าบรรจุภัณฑ์ - กระสอบ", "ค่าบรรจุภัณฑ์ - ถุงแพ็ค & ถุงใส", "ค่าบรรจุภัณฑ์ - เชือก",
    "ค่าแรงฝ่ายผลิต (แพ็ค/ล้างกระสอบ)",
    "ค่าน้ำแข็งซื้อ - แก่งคอย", "ค่าน้ำแข็งซื้อ - สระบุรี", "ค่าน้ำแข็งซื้อ - บางปะอิน", "ค่าน้ำแข็งซื้อ - อื่น",
    "ค่าน้ำมัน - บางจาก", "ค่าน้ำมัน - PT",
    "ประกันรถยนต์ (คุณไอยลดา)", "ภาษี & พรบ รถยนต์ (คุณคงเกียรติ์)",
    "ค่าบำรุงเครื่องจักร - ร้านวิศวภัณฑ์", "ค่าบำรุงเครื่องจักร - วิบูลย์เจริญการไฟฟ้า",
    "ค่าบำรุงเครื่องจักร - สุขอนันต์การไฟฟ้า", "ค่าบำรุงเครื่องจักร - อื่นๆ",
    "ค่าซ่อมรถ - อู่ชมรมย์การช่าง", "ค่าซ่อมรถ - ศรการยาง", "ค่าซ่อมรถ - โชคพัฒนา",
    "ค่าซ่อมรถ - ศูนย์อิซูซุจึงกงเฮง", "ค่าซ่อมรถ - อู่ดรไดนาโม", "ค่าซ่อมรถ - อู่มงคลไฟรถยนต์",
    "ค่าซ่อมรถ - ปัญจบวรอะไหล่", "ค่าซ่อมรถ - สระบุรีอะไหล่", "ค่าซ่อมรถ - อื่นๆ",
    "ค่าใช้จ่ายบริหาร - ทำบัญชี", "ค่าใช้จ่ายบริหาร - โปรแกรม icepb", "ค่าใช้จ่ายบริหาร - โปรแกรม GPS",
    "อื่นๆ / ไม่แน่ใจ (Other / flag for review)",
]
DEFAULT_CATEGORY = "อื่นๆ / ไม่แน่ใจ (Other / flag for review)"

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "transaction_date": {
            "type": "string",
            "description": (
                "Date of the transfer, converted to YYYY-MM-DD format if "
                "possible. Thai apps/receipts often show the Buddhist Era "
                "year (e.g. 2569) instead of the Christian/Gregorian year "
                "-- if you see a year in the range 2500-2600, subtract 543 "
                "to get the Gregorian year (e.g. 2569 - 543 = 2026) before "
                "writing the date. Empty string if it cannot be determined."
            ),
        },
        "transaction_time": {
            "type": "string",
            "description": "Time of the transfer in 24-hour HH:MM format (e.g. '14:35'). Empty string if not shown.",
        },
        "from_display_name": {
            "type": "string",
            "description": "The account holder name shown for the SENDING (source) account, exactly as written -- do not translate.",
        },
        "from_account_digits": {
            "type": "string",
            "description": (
                "Whatever digits are shown for the sending account, exactly as displayed -- banking apps commonly "
                "mask most of the number (e.g. 'xxx-x-x3909' or 'XXX-X-X3909-9'), showing only the last 3-5 "
                "digits. Write out only the visible digits (e.g. '3909'), ignoring x's/masking characters and "
                "dashes. Empty string if no account number is shown at all."
            ),
        },
        "from_bank": {
            "type": "string",
            "description": "The sending account's bank name, as shown by logo text or app branding (e.g. 'กรุงไทย', 'ไทยพาณิชย์', 'กสิกรไทย'). Empty string if not shown/identifiable.",
        },
        "to_display_name": {
            "type": "string",
            "description": "The account holder / recipient name shown for the RECEIVING account, exactly as written -- do not translate. This is often a shop or vendor name.",
        },
        "to_account_digits": {
            "type": "string",
            "description": "Same rules as from_account_digits, but for the receiving account.",
        },
        "to_bank": {
            "type": "string",
            "description": "The receiving account's bank name, same rules as from_bank.",
        },
        "amount": {
            "type": "number",
            "description": "The transfer amount in Thai Baht. Use 0 if not legible.",
        },
        "purpose_note": {
            "type": "string",
            "description": (
                "ALWAYS actively check the BOTTOM of the slip for a memo/note field -- Thai banking apps commonly "
                "have one (often labeled 'บันทึกช่วยจำ', 'โน้ต', or similar) where the sender types free text "
                "explaining what the payment is for, separate from the main transfer details above it. This is "
                "the single most important field on the whole slip for bookkeeping purposes -- read it carefully "
                "even if it's small text near the bottom. Empty string only if you've genuinely checked and there "
                "is no memo/note field or it was left blank."
            ),
        },
        "reference_number": {
            "type": "string",
            "description": "The transaction/reference number shown, if any (useful for matching duplicates later). Empty string if not shown.",
        },
    },
    "required": [
        "transaction_date", "transaction_time", "from_display_name", "from_account_digits", "from_bank",
        "to_display_name", "to_account_digits", "to_bank", "amount", "purpose_note", "reference_number",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an expert at reading Thai bank transfer / \
payment confirmation slips for a family-run trucking and ice-delivery \
business in Saraburi, Thailand. These are typically screenshots from \
banking apps (K PLUS, SCB EASY, Krungthai NEXT, and similar) or printed \
ATM/bank receipts, showing a completed transfer from one account to \
another.

Read the slip carefully. If a field is illegible or missing, use an empty \
string (or 0 for the amount) rather than guessing a value. Keep all text \
fields in Thai (or whatever language they're actually written in) exactly \
as written -- do not translate anything into English.

Look at the ENTIRE image, not just the main transfer details in the \
middle -- banking apps commonly show a memo/note field near the BOTTOM of \
the receipt, sometimes below a "แชร์"/"บันทึก" (share/save) button row, \
which is easy to miss if you stop reading after the amount. This note \
field is the primary source of "what this payment was for" and matters a \
great deal for bookkeeping -- always check for it specifically before \
finishing."""


def _load_roster_rows() -> list[dict]:
    if not ACCOUNTS_ROSTER_PATH.exists():
        return []
    with open(ACCOUNTS_ROSTER_PATH, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def get_roster_rows() -> list[dict]:
    """Public wrapper for app/slips_routes.py, same role as
    bill_extraction.get_roster_rows()."""
    return _load_roster_rows()


def _normalize_digits(raw: str) -> str:
    return "".join(c for c in raw if c.isdigit())


def lookup_account(digits: str) -> Optional[dict]:
    """Deterministic digits -> bank_accounts.csv row lookup, tolerant of
    the sending app only showing a partial/masked account number. Matches
    on suffix (the last N digits actually shown almost always corresponds
    to the roster's last_digits, which itself is only ever the account's
    last 4-5 digits -- see Account_Master.xlsx). Returns None if nothing
    matches, or if more than one distinct account matches ambiguously --
    safer to leave unresolved than to silently guess, same rule
    bill_extraction.lookup_vehicle uses for plates."""
    digits_norm = _normalize_digits(digits)
    if not digits_norm:
        return None
    rows = [r for r in _load_roster_rows() if r.get("last_digits")]

    def matches(row_digits: str) -> bool:
        return row_digits.endswith(digits_norm) or digits_norm.endswith(row_digits)

    candidates = [r for r in rows if matches(r["last_digits"])]
    if not candidates:
        return None
    exact = [r for r in candidates if r["last_digits"] == digits_norm]
    pool = exact or candidates
    distinct = {(r.get("bank", ""), r.get("last_digits", "")) for r in pool}
    if len(distinct) > 1:
        logger.warning("account digits %r match multiple roster entries ambiguously: %s", digits, distinct)
        return None
    return pool[0]


def _suggest_category(to_display_name: str) -> str:
    """Best-effort substring match of the recipient name against the
    vendor-named categories (ค่าซ่อมรถ shops, ค่าบำรุงเครื่องจักร suppliers,
    ค่าน้ำแข็งซื้อ suppliers) -- a suggestion only, always confirmed/
    overridden by the reviewer via a dropdown, same as bills' category
    picker. Falls back to DEFAULT_CATEGORY if nothing matches."""
    name = (to_display_name or "").strip()
    if not name:
        return DEFAULT_CATEGORY
    for category in CATEGORIES:
        if " - " not in category:
            continue
        vendor_part = category.split(" - ", 1)[1]
        if vendor_part and (vendor_part in name or name in vendor_part):
            return category
    return DEFAULT_CATEGORY


def _cross_branch_note(purpose_note: str, matched_branch: str) -> str:
    """Informational only -- never changes `branch`. Flags the specific
    case the owner described: the memo mentions the OTHER branch than the
    one that's actually paying, so a reviewer sees explicitly why a
    Kaeng-Khoi-sounding expense landed as a Saraburi cost (or vice versa),
    per the "whoever paid" rule applied in extract_slip()."""
    if not purpose_note or not matched_branch:
        return ""
    mentions_srb = any(k in purpose_note for k in ("สระบุรี", "สรบ", "SRB"))
    mentions_kk = any(k in purpose_note for k in ("แก่งคอย", "กแคย", "KK"))
    other_branch = None
    if matched_branch == "SRB" and mentions_kk and not mentions_srb:
        other_branch = "แก่งคอย"
    elif matched_branch == "KK" and mentions_srb and not mentions_kk:
        other_branch = "สระบุรี"
    if other_branch is None:
        return ""
    paying_branch_th = "สระบุรี" if matched_branch == "SRB" else "แก่งคอย"
    return (
        f"หมายเหตุ: ข้อความในสลิประบุ{other_branch} แต่จ่ายจากบัญชีสาขา{paying_branch_th} "
        f"-- คิดต้นทุนตามบัญชีที่จ่ายตามนโยบาย (ไม่ใช่ตามสาขาที่ได้รับประโยชน์)"
    )


def _content_block(file_bytes: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def extract_slip(file_bytes: bytes, media_type: str) -> dict:
    """Sends one slip photo/PDF to Claude and returns the extracted data,
    enriched with the deterministic account-roster lookup and the
    "whoever paid" branch rule. media_type: 'image/jpeg', 'image/png', or
    'application/pdf'."""
    response = _client_lazy().messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    _content_block(file_bytes, media_type),
                    {"type": "text", "text": "Read this bank transfer slip and extract the data."},
                ],
            }
        ],
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
        },
    )

    text_block = next(b.text for b in response.content if b.type == "text")
    slip = json.loads(text_block)

    match = lookup_account(slip.get("from_account_digits") or "")
    if match:
        slip["branch"] = match.get("branch", "")
        slip["account_used_label"] = match.get("account_label", "")
        slip["account_match_warning"] = ""
    else:
        slip["branch"] = ""
        slip["account_used_label"] = ""
        slip["account_match_warning"] = (
            "ไม่พบบัญชีต้นทางในทะเบียน กรุณาตรวจสอบเลขบัญชีและกรอกสาขา/บัญชีที่ใช้เอง"
        )

    slip["cross_branch_note"] = _cross_branch_note(slip.get("purpose_note") or "", slip.get("branch") or "")
    slip["pl_category_suggestion"] = _suggest_category(slip.get("to_display_name") or "")
    return slip

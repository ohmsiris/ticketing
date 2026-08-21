"""
Reads a photographed/PDF repair bill via Claude vision and returns
structured data. Ported from the standalone OCR testing project
(bill_extractor.py) -- same schema, same prompt, same category rules,
tuned against several hundred real bills there. Adapted here to work on
in-memory bytes (downloaded from a LINE message) instead of local files,
and drops the local CSV/caching machinery that only made sense for batch
testing on a laptop.
"""
import base64
import csv
import json
import logging
from pathlib import Path
from typing import Optional

from anthropic import Anthropic

from app.config import settings

logger = logging.getLogger("ticketing.bill_extraction")

_client: Optional[Anthropic] = None


def _client_lazy() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


# Deliberately a different (stronger, pricier) model than classifier.py's
# claude-sonnet-5 -- OCR on messy handwriting benefits meaningfully from
# the extra accuracy, tested against real bills in the OCR project.
MODEL = "claude-opus-5"

VEHICLE_ROSTER_PATH = Path(__file__).resolve().parent / "vehicle_roster.csv"

# The 16 categories every repair line item must be sorted into (in Thai,
# so results are directly readable without translating). Keep in sync
# with the OCR project's bill_extractor.py if either ever changes.
CATEGORIES = [
    "เครื่องยนต์",       # Engine
    "เกียร์",            # Transmission
    "คลัตช์",            # Clutch
    "ระบบส่งกำลัง",       # Drivetrain
    "ช่วงล่าง",           # Suspension (includes bearings/seals -- see category rules below)
    "พวงมาลัย",          # Steering
    "เบรก",              # Brakes
    "ล้อและยาง",         # Wheels & Tires
    "ระบบไฟฟ้า",         # Electrical
    "ระบบหล่อเย็น",       # Cooling
    "ระบบเชื้อเพลิง",     # Fuel System
    "ระบบแอร์",           # Air Conditioning
    "ตัวถังและโครงรถ",     # Body & Chassis
    "ไส้กรองและของเหลว",  # Filters & Fluids / consumables
    "ค่าแรง",            # Labor
    "อื่นๆ",             # Other (catch-all)
]

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "shop_name": {
            "type": "string",
            "description": "Name of the repair shop, in the original language exactly as written on the bill (do not translate or add an English transliteration in parentheses). Empty string if not legible or not present.",
        },
        "date": {
            "type": "string",
            "description": (
                "Date of the bill, converted to YYYY-MM-DD format if possible. "
                "Thai bills often use the Buddhist Era year (e.g. 2567) instead "
                "of the Christian/Gregorian year -- if you see a year in the "
                "range 2500-2600, subtract 543 to get the Gregorian year "
                "(e.g. 2567 - 543 = 2024) before writing the date. If the date "
                "cannot be determined at all, use an empty string."
            ),
        },
        "branch": {
            "type": "string",
            "description": (
                "Which branch of the business the truck belongs to, if "
                "stated -- e.g. a note reading 'รถของสระบุรี' means Saraburi "
                "branch, 'รถของแก่งคอย' means Kaeng Khoi branch. A dealership "
                "document may instead show the branch as part of the "
                "service center's own name (e.g. '...ศูนย์ซ่อมสีและตัวถัง"
                "แก่งคอย' means the Kaeng Khoi service center) -- if so, "
                "just write the branch name (e.g. 'แก่งคอย'), not the whole "
                "shop name again. Empty string if the branch isn't stated "
                "anywhere on the bill."
            ),
        },
        "vehicle_license": {
            "type": "string",
            "description": (
                "The vehicle's plate NUMBER ONLY, written in the original "
                "language exactly as shown on the bill (do not translate or "
                "add an English transliteration) -- do NOT include the "
                "province name here, that goes in vehicle_license_province "
                "below (e.g. a plate reading '1ฒย 4267 กทม' becomes "
                "vehicle_license='1ฒย 4267' and vehicle_license_province="
                "'กทม'). This is NOT always near the top of the bill -- "
                "look over the WHOLE page, since it's sometimes written in "
                "the middle of the page instead. A reference list of a FEW "
                "known vehicles may be provided below, but it is heavily "
                "incomplete -- most real, legitimate plates will NOT be on "
                "it, even ones from the same branch. Only use that list as "
                "a tie-breaker when the handwriting is genuinely ambiguous "
                "between two similar-looking characters (e.g. it could be a "
                "4 or could be an 8) AND one reading matches the list. Do "
                "NOT change a clearly-written plate to match the list just "
                "because it's close -- transcribe exactly what's actually "
                "written by default, since the true vehicle very likely "
                "just isn't on this short partial list. Empty string if no "
                "plate is present or legible at all."
            ),
        },
        "vehicle_license_province": {
            "type": "string",
            "description": (
                "The province name shown with the plate, if any (Thai "
                "plates commonly show one below/beside the number) -- "
                "common ones for this fleet include กทม/กรุงเทพมหานคร "
                "(Bangkok), นนทบุรี (Nonthaburi), สระบุรี (Saraburi), and "
                "ยโสธร (Yasothon), but transcribe whatever province is "
                "actually written, even if it's not one of these. Keep "
                "whatever abbreviation/form is actually written (don't "
                "expand 'กทม' to the full 'กรุงเทพมหานคร' or vice versa). "
                "Empty string if no province is shown."
            ),
        },
        "vehicle_number": {
            "type": "string",
            "description": (
                "The fleet truck number, ONLY if it's actually written on "
                "the bill itself (e.g. 'เบอร์ 18', 'รถเบอร์ 1', or similar) "
                "-- do not infer or guess this from the plate, even if a "
                "reference list below happens to show a matching plate; a "
                "separate, more reliable lookup step handles that "
                "afterward. Empty string if no truck number is written on "
                "the bill."
            ),
        },
        "mileage": {
            "type": "string",
            "description": (
                "ALWAYS actively check the BOTTOM of the page for a "
                "mileage/odometer note, regardless of what else is on the "
                "bill -- this is easy to overlook because some shops write "
                "it as a small handwritten annotation in RED ink "
                "(different from the black ink used for the rest of the "
                "bill), separate from the main line-item table, and it's "
                "easy to mistake for a stray mark instead of real data. "
                "One common format: 'เลขไมล์[ปัจจุบัน] 650316 ครั้งต่อไป "
                "660316' -- this means CURRENT mileage 650316 and NEXT "
                "SERVICE DUE mileage 660316; put only the first (current) "
                "number here as plain digits with no commas or units (the "
                "second number goes in next_service_mileage below). If the "
                "shop instead wrote that the odometer was broken, write "
                "exactly 'ไมล์เสีย' instead of a number. Only use an empty "
                "string if you've actually checked the bottom of the page "
                "carefully and there's truly no mileage note there. As "
                "general background: dealership documents almost always "
                "show mileage, while independent mechanic shops more often "
                "write it when there's an engine oil change (น้ำมันเครื่อง) "
                "on the bill -- but that's just a pattern, not a reason to "
                "stop looking; some shops note it even without one."
            ),
        },
        "next_service_mileage": {
            "type": "string",
            "description": (
                "The NEXT service due mileage, if the bill shows one "
                "alongside the current mileage (e.g. the 'ครั้งต่อไป "
                "660316' part of 'เลขไมล์ 650316 ครั้งต่อไป 660316' means "
                "next service due at 660316) -- plain digits, no commas or "
                "units. Empty string if only a current mileage is shown "
                "with no next-service number, or if there's no mileage "
                "note at all."
            ),
        },
        "total_cost": {
            "type": "number",
            "description": (
                "The grand total shown on THIS PHOTO ONLY, in Thai Baht -- "
                "the actual final amount owed/paid, not a pre-tax subtotal. "
                "Some dealership documents show several totals (before "
                "discount, after discount, VAT 7%, then a final 'net "
                "amount' / จำนวนเงินรวมสุทธิ) -- always use that final net "
                "amount including VAT when multiple totals are shown, since "
                "that's what's actually paid. Use 0 if no grand total "
                "appears on this specific photo -- for example, this is "
                "normal for the first page of a bill that continues onto a "
                "second photo, where only a running subtotal (ยอดยกไป) may "
                "appear instead."
            ),
        },
        "has_total_this_page": {
            "type": "boolean",
            "description": "True if a grand total (รวมเงิน) is shown on this photo. False if this photo only shows a running subtotal, or no total at all -- which usually means the bill continues on another photo.",
        },
        "continues_next_page": {
            "type": "boolean",
            "description": "True if this photo shows a 'ยอดยกไป' note (often with an arrow) near the bottom, indicating the bill's line items continue onto another photo. False for a normal single-page bill or the final page of a multi-page bill.",
        },
        "line_items": {
            "type": "array",
            "description": "Every individual repair or part listed on the bill, in the order they appear.",
            "items": {
                "type": "object",
                "properties": {
                    "quantity": {
                        "type": "number",
                        "description": (
                            "The quantity/count from the left-most column of "
                            "the line item table, if the bill has one (e.g. "
                            "4 for a row showing '4' next to 'brake shoes'). "
                            "Use 0 if there is no quantity column or it "
                            "isn't legible. Labor/service charges (ค่าแรง "
                            "etc.) normally have NO quantity written at all "
                            "-- that's expected, not a legibility problem, "
                            "so use 0 rather than guessing a plausible-"
                            "looking number like 1."
                        ),
                    },
                    "unit": {
                        "type": "string",
                        "description": (
                            "The unit written next to the quantity, if any "
                            "(e.g. ชิ้น, ตัว, ลิตร, คู่). Empty string if no "
                            "unit is shown -- this is expected and normal for "
                            "labor/service charges (ค่าแรง etc.), which "
                            "usually have no unit written at all; don't "
                            "guess one. Helpful context: liquid oils "
                            "(engine oil น้ำมันเครื่อง, gear oil น้ำมันเกียร์, "
                            "differential oil น้ำมันเฟืองท้าย, brake fluid "
                            "น้ำมันเบรค, etc.) are almost always measured in "
                            "ลิตร (liters) -- if the handwriting is ambiguous "
                            "for one of these, ลิตร is far more likely than "
                            "กิโล. Grease (จารบี) is the notable exception and "
                            "is commonly measured by weight in กิโล instead. "
                            "Also watch for these specific unit misreadings "
                            "observed before: ก.ป misread as ก.ล or ถ.ม; ชุด "
                            "misread as อัน; หัว misread as ชิ้น; คัน misread "
                            "as ตัว or เส้น."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "The repair or part description exactly as written on the bill, in the original language, word for word -- do not translate, paraphrase, reword, or 'clean up' the text, and do not fold the quantity into this field since it has its own field above. If a word is genuinely illegible, write [illegible] in place of that word rather than guessing.",
                    },
                    "cost": {
                        "type": "number",
                        "description": "Cost of this individual line item in Thai Baht. Use 0 if not itemized or not legible.",
                    },
                    "category": {
                        "type": "string",
                        "enum": CATEGORIES,
                        "description": (
                            "The single best-fit category for this line item. Rules "
                            "for close calls:\n"
                            "1. Consumables win over location: any lubricant, fluid, "
                            "oil, grease, or filter goes in 'ไส้กรองและของเหลว' "
                            "(Filters & Fluids) even if it's used on a specific system "
                            "-- e.g. wheel grease (จารบีล้อ) is 'ไส้กรองและของเหลว', "
                            "NOT 'ช่วงล่าง' (Suspension), because it's a consumable, "
                            "not a physical part.\n"
                            "2. Bearings, seals, and other physical wear parts are "
                            "categorized by where they're installed, e.g. a wheel "
                            "bearing/seal (ลูกปืนล้อ, ซีลล้อ) goes under 'ช่วงล่าง' "
                            "(Suspension).\n"
                            "3. Labor/service/diagnostic-only charges (no specific "
                            "part named, e.g. ค่าแรง, ค่าบริการ, ค่าตรวจเช็ค) go under "
                            "'ค่าแรง' (Labor), even if the labor was for a specific "
                            "system -- parts and labor should be tracked separately.\n"
                            "4. Only use 'อื่นๆ' (Other) if nothing else genuinely fits."
                        ),
                    },
                },
                "required": ["quantity", "unit", "description", "cost", "category"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "shop_name", "date", "branch", "vehicle_license", "vehicle_license_province",
        "vehicle_number", "mileage", "next_service_mileage", "total_cost",
        "has_total_this_page", "continues_next_page", "line_items",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an expert at reading Thai vehicle repair bills for \
a fleet of Isuzu and Hino delivery trucks used in an ice delivery business \
in Saraburi, Thailand. These come in two forms: informal handwritten bills \
from independent mechanic shops, and official printed quotations/invoices \
from dealerships (e.g. Isuzu service centers) -- apply the same care to \
both, even though the printed ones are much easier to read accurately.

Read the bill photo carefully and extract every line item, even if the \
handwriting is messy or the photo quality is imperfect. If a field is \
illegible or missing, use an empty string (or 0 for numbers) rather than \
guessing a value.

Look at the ENTIRE page, not just the main table -- some shops add small \
handwritten annotations near the edges or bottom of the bill, sometimes \
in a different ink color (e.g. red) than the rest of the bill. Don't \
mistake these for stray marks; check whether they contain real data \
(mileage notes are a common example).

Keep all text fields in Thai, exactly as written on the bill. Do not \
translate anything into English or add an English transliteration, even \
in parentheses -- write only what is actually on the bill.

Read the line item table as a whole, column by column, the way it is laid \
out on the bill (quantity column, description column, price column, etc.). \
Transcribe descriptions verbatim -- do not reword, summarize, or "clean up" \
the wording, even if it seems repetitive or informally written.

Some bills span more than one photo (the line items continue onto a second \
page). Watch specifically for a handwritten "ยอดยกไป" note, often with an \
arrow, near the bottom of the page -- this means the bill continues on \
another photo, and this page will NOT show a grand total (รวมเงิน), only a \
running subtotal at most. Report this accurately in has_total_this_page \
and continues_next_page -- a separate step will stitch multi-page bills \
back together, so just describe THIS PHOTO accurately rather than trying \
to guess what's on another photo.

Specific misreadings observed before on these shops' handwriting -- look \
twice before writing any of the "misread as" versions below, and check \
which one is actually written:
- "แหนบ" (leaf spring) has been misread as "แท่นบน" (upper mount).
- "หัวฉีด" (fuel injector) has been misread as "น๊อตยึด" (mounting bolt).
- The unit abbreviation "ก.ป" has been misread as "ก.ล" -- these look \
similar handwritten; check the last character carefully."""


def _load_roster_rows() -> list[dict]:
    if not VEHICLE_ROSTER_PATH.exists():
        return []
    with open(VEHICLE_ROSTER_PATH, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _load_vehicle_roster_text() -> str:
    rows = _load_roster_rows()
    if not rows:
        return ""
    lines = []
    for r in rows:
        bits = [f"branch={r.get('branch','')}", f"truck#={r.get('truck_number','')}", f"plate={r.get('plate','')}"]
        if r.get("province"):
            bits.append(f"province={r['province']}")
        make_model = f"{r.get('make','')} {r.get('model','')}".strip()
        if make_model:
            bits.append(make_model)
        lines.append("- " + ", ".join(bits))
    return (
        "Known fleet vehicles. Saraburi branch is fairly complete here; "
        "Kaeng Khoi branch is INCOMPLETE (many Kaeng Khoi trucks are "
        "missing from this list). IMPORTANT: truck numbers are NOT unique "
        "across branches -- e.g. 'truck #6' exists in BOTH Saraburi and "
        "Kaeng Khoi as two entirely different real vehicles, and most "
        "numbers 1-10 are duplicated this way. Never assume which branch a "
        "bare truck number belongs to from this list alone -- only use it "
        "to help read an ambiguous PLATE, since plates (unlike truck "
        "numbers) are genuinely unique per vehicle:\n" + "\n".join(lines)
    )


def _normalize_plate(plate: str) -> str:
    return "".join(plate.split())


def lookup_vehicle(plate: str) -> Optional[dict]:
    """Deterministic plate -> roster row lookup (branch, truck_number,
    plate, province, make, model), tolerating minor formatting
    differences (missing space, an added province suffix like 'กท').
    Plates are the reliable unique key here -- truck numbers are NOT (the
    same number exists in both branches for different real trucks), so
    this never looks anything up by truck number alone.

    Returns None if nothing matches, OR if the plate matches more than
    one roster row for genuinely different vehicles -- safer to leave it
    unresolved (and let a human notice) than to silently guess one."""
    plate_norm = _normalize_plate(plate)
    if not plate_norm:
        return None
    rows = _load_roster_rows()

    def fuzzy_match(row_plate_norm: str) -> bool:
        return bool(row_plate_norm) and (
            row_plate_norm == plate_norm or row_plate_norm in plate_norm or plate_norm in row_plate_norm
        )

    candidates = [r for r in rows if r.get("plate") and fuzzy_match(_normalize_plate(r["plate"]))]
    if not candidates:
        return None
    exact = [r for r in candidates if _normalize_plate(r["plate"]) == plate_norm]
    pool = exact or candidates
    distinct_vehicles = {(r.get("branch", ""), r.get("truck_number", "")) for r in pool}
    if len(distinct_vehicles) > 1:
        logger.warning("plate %r matches multiple roster entries ambiguously: %s", plate, distinct_vehicles)
        return None
    return pool[0]


def _content_block(file_bytes: bytes, media_type: str) -> dict:
    """media_type: e.g. 'image/jpeg', 'image/png', or 'application/pdf'.
    PDFs use a 'document' block -- Claude reads it visually page by page,
    which sidesteps garbled text layers some Thai business software
    produces (see the OCR project's notes on this)."""
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def extract_bill(file_bytes: bytes, media_type: str) -> dict:
    """Sends one bill photo or PDF (as raw bytes) to Claude and returns the
    extracted data as a dict. media_type must be one of 'image/jpeg',
    'image/png', or 'application/pdf'."""
    roster_text = _load_vehicle_roster_text()
    prompt_text = "Read this repair bill and extract the data."
    if roster_text:
        prompt_text += "\n\n" + roster_text

    response = _client_lazy().messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    _content_block(file_bytes, media_type),
                    {"type": "text", "text": prompt_text},
                ],
            }
        ],
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
        },
    )

    text_block = next(b.text for b in response.content if b.type == "text")
    bill = json.loads(text_block)

    # Deterministic enrichment + validation against the roster -- see
    # lookup_vehicle's docstring for why this is plate-keyed, never
    # truck-number-keyed. Builds a plain-language warning (empty string
    # if nothing to flag) surfaced downstream in the review page and the
    # manager's LINE notification.
    warnings = []
    match = lookup_vehicle(bill.get("vehicle_license") or "")
    if match:
        if not (bill.get("vehicle_number") or "").strip():
            bill["vehicle_number"] = match.get("truck_number", "")
        if not (bill.get("vehicle_license_province") or "").strip():
            bill["vehicle_license_province"] = match.get("province", "")
        bill_branch = (bill.get("branch") or "").strip()
        roster_branch = (match.get("branch") or "").strip()
        if bill_branch and roster_branch and bill_branch != roster_branch:
            warnings.append(
                f"ป้ายทะเบียนตรงกับรถของสาขา{roster_branch} แต่บิลระบุสาขา{bill_branch} -- ช่วยตรวจสอบด้วย"
            )
    if not (bill.get("vehicle_number") or "").strip():
        warnings.append("ไม่พบเบอร์รถที่ตรงกัน กรุณาตรวจสอบและกรอกเบอร์รถเอง")

    bill["vehicle_match_warning"] = " / ".join(warnings)
    return bill

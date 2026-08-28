"""
Reads a photographed/PDF supplier bill for parts/supplies (belts,
bearings, chains, breakers, magnetic relays, etc.) via Claude vision and
returns structured data. Sibling to app/bill_extraction.py (vehicle repair
bills) -- same shape (header + line items, same multi-page continuation
handling), but no vehicle-specific fields (plate, mileage, roster
matching), since a parts purchase isn't tied to one vehicle.

CATEGORIES is a first draft, not yet corrected against real bills the way
bill_extraction.py's 16 repair categories were tuned in the standalone OCR
project -- expect this list to need a review/correction round once real
purchases start coming in, same as app/maintenance.py's DEFAULT_TASKS did.
"""
import base64
import json
import logging
from typing import Optional

from anthropic import Anthropic

from app.config import settings

logger = logging.getLogger("ticketing.supply_extraction")

_client: Optional[Anthropic] = None


def _client_lazy() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


# Same tier as bill_extraction.py -- OCR accuracy on messy supplier
# invoices benefits from the stronger model, same reasoning.
MODEL = "claude-opus-5"

CATEGORIES = [
    "สายพานและโซ่",            # Belts & Chains
    "ลูกปืนและบูช",             # Bearings & Bushings
    "อุปกรณ์ไฟฟ้า",             # Electrical (breakers, relays, contactors, fuses)
    "มอเตอร์และปั๊ม",           # Motors & Pumps
    "ซีลและปะเก็น",             # Seals & Gaskets
    "ท่อและข้อต่อ",             # Piping & Fittings
    "น้ำมันและสารหล่อลื่น",     # Oils & Lubricants
    "น้ำยาสารทำความเย็น",       # Refrigerant
    "ไส้กรอง",                  # Filters
    "อุปกรณ์ยึด",               # Fasteners / Hardware (bolts, nuts, screws, washers)
    "เครื่องมือ",               # Tools
    "ค่าแรงและบริการ",          # Labor / Service (some supplier bills include installation)
    "อื่นๆ",                    # Other (catch-all)
]

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": {
            "type": "string",
            "description": "Name of the supplier/shop, in the original language exactly as written on the bill (do not translate or add an English transliteration in parentheses). Empty string if not legible or not present.",
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
                "Which branch of the business the purchase is for, if stated "
                "-- e.g. a note reading 'สระบุรี' or 'แก่งคอย', or a delivery "
                "address matching one of those. Empty string if the branch "
                "isn't stated anywhere on the bill."
            ),
        },
        "total_cost": {
            "type": "number",
            "description": (
                "The grand total shown on THIS PHOTO ONLY, in Thai Baht -- "
                "the actual final amount owed/paid, not a pre-tax subtotal. "
                "If multiple totals are shown (before VAT, after VAT/7%, a "
                "final net amount), use the final net amount actually owed. "
                "Use 0 if no grand total appears on this specific photo -- "
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
            "description": "Every individual part/supply item listed on the bill, in the order they appear.",
            "items": {
                "type": "object",
                "properties": {
                    "quantity": {
                        "type": "number",
                        "description": "The quantity/count from the left-most column of the line item table, if the bill has one. Use 0 if there is no quantity column or it isn't legible.",
                    },
                    "unit": {
                        "type": "string",
                        "description": "The unit written next to the quantity, if any (e.g. ชิ้น, ตัว, เมตร, ม้วน, ชุด, คู่). Empty string if no unit is shown.",
                    },
                    "description": {
                        "type": "string",
                        "description": "The part/item description exactly as written on the bill, in the original language, word for word -- do not translate, paraphrase, reword, or 'clean up' the text. If a word is genuinely illegible, write [illegible] in place of that word rather than guessing.",
                    },
                    "cost": {
                        "type": "number",
                        "description": "Cost of this individual line item in Thai Baht. Use 0 if not itemized or not legible.",
                    },
                    "category": {
                        "type": "string",
                        "enum": CATEGORIES,
                        "description": "The single best-fit category for this line item. Use 'ค่าแรงและบริการ' for installation/service/delivery charges with no specific part named. Only use 'อื่นๆ' (Other) if nothing else genuinely fits.",
                    },
                },
                "required": ["quantity", "unit", "description", "cost", "category"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "supplier_name", "date", "branch", "total_cost",
        "has_total_this_page", "continues_next_page", "line_items",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an expert at reading Thai supplier invoices for \
mechanical/electrical parts and supplies (belts, bearings, chains, circuit \
breakers, magnetic relays, motors, seals, and similar industrial \
components) bought for an ice-manufacturing business in Saraburi, \
Thailand -- NOT vehicle repair bills, which are a separate document type \
handled elsewhere. These come as printed invoices/receipts from hardware \
stores, electrical supply shops, or industrial parts distributors, and \
occasionally informal handwritten receipts.

Read the bill photo carefully and extract every line item, even if the \
print is small or the photo quality is imperfect. If a field is illegible \
or missing, use an empty string (or 0 for numbers) rather than guessing a \
value.

Look at the ENTIRE page, not just the main table -- delivery address, \
branch notes, or a running subtotal note can appear near the edges or \
bottom of the bill.

Keep all text fields in Thai, exactly as written on the bill. Do not \
translate anything into English or add an English transliteration, even \
in parentheses -- write only what is actually on the bill.

Read the line item table as a whole, column by column, the way it is laid \
out on the bill (quantity column, description column, price column, etc.). \
Transcribe descriptions verbatim -- do not reword, summarize, or "clean up" \
the wording.

Some bills span more than one photo (the line items continue onto a second \
page). Watch specifically for a handwritten or printed "ยอดยกไป" note, \
often with an arrow, near the bottom of the page -- this means the bill \
continues on another photo, and this page will NOT show a grand total \
(รวมเงิน), only a running subtotal at most. Report this accurately in \
has_total_this_page and continues_next_page -- a separate step stitches \
multi-page bills back together, so just describe THIS PHOTO accurately \
rather than trying to guess what's on another photo."""


def _content_block(file_bytes: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def extract_supply_purchase(file_bytes: bytes, media_type: str) -> dict:
    """Sends one supplier-bill photo or PDF (as raw bytes) to Claude and
    returns the extracted data as a dict. media_type must be one of
    'image/jpeg', 'image/png', or 'application/pdf'."""
    response = _client_lazy().messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    _content_block(file_bytes, media_type),
                    {"type": "text", "text": "Read this supplier bill and extract the data."},
                ],
            }
        ],
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
        },
    )
    text_block = next(b.text for b in response.content if b.type == "text")
    return json.loads(text_block)

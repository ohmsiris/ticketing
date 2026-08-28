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

Each line item also gets a `canonical_part` field alongside the verbatim
`description` -- the whole point of this feature per the user is "who
sells this part cheapest", not stock tracking, so every purchase of the
"same" part needs to land under one consistent name for the PartPrices
Sheet to actually be comparable across suppliers/bills (see
app/sheets_client.py's PART_PRICES_HEADER). The user flagged a real risk
here: different shops describe the identical part in different languages
(one bill in Thai, another in English) or different phrasing -- without
help, that silently fragments one real part into multiple canonical_part
strings that never get compared against each other. Two mitigations, no
literal web search involved:

1. Brand names/model numbers are always normalized to standard
   Roman-script spelling (e.g. "Shell", "Rimula", "SKF") regardless of
   what script the surrounding bill uses -- see the prompt.
2. extract_supply_purchase()'s known_canonical_parts parameter feeds back
   every canonical_part name already in use (app/supplies.py's
   get_known_canonical_parts) so the model matches against real prior
   usage instead of inventing a fresh name from nothing each time -- same
   trick as bill_extraction.py's vehicle roster and maintenance.py's
   DEFAULT_TASKS matching.

Still Claude's best-effort at extraction time, not a real database-
enforced catalog -- there's no hard uniqueness constraint, nothing stops
drift entirely. The reviewer can (and should) hand-fix canonical_part on
the review page when it doesn't match how the same part was named before;
that correction itself becomes part of next time's known-parts list, so
the system gets more consistent over time rather than less.
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
                    "canonical_part": {
                        "type": "string",
                        "description": (
                            "A short, normalized name for this exact part, used later to compare "
                            "prices for the SAME part across different bills/suppliers -- so "
                            "consistency matters more than style, and more than matching this bill's "
                            "own wording. Strip shop-specific phrasing and unit/quantity words, but "
                            "KEEP any part number, size, or spec that actually distinguishes it from a "
                            "similar part (e.g. 'สายพานพัดลม A47 ยี่ห้อ Bando 1 เส้น' -> 'สายพาน A47'; "
                            "'ลูกปืนล้อหลัง เบอร์ 6205' -> 'ลูกปืน 6205'; "
                            "'น้ำมันเครื่อง Shell Rimula 15W-40 ถัง 18 ลิตร' -> 'น้ำมันเครื่อง Rimula 15W-40'). "
                            "IMPORTANT: two shops selling the exact same part often write it in "
                            "different languages (one bill in Thai, another in English) -- always write "
                            "any brand name or model/part number in its standard Roman-script form "
                            "(e.g. 'Shell', 'Rimula', 'Bando', 'SKF'), never a Thai transliteration of "
                            "it, even when the rest of the bill (and the description field) is in Thai. "
                            "Generic Thai part-type words (สายพาน, ลูกปืน, น้ำมันเครื่อง, etc.) stay in "
                            "Thai -- only the brand/model portion needs to be language-normalized. If a "
                            "list of already-used canonical_part names is provided below, check it "
                            "FIRST: if a line item is the same real part as one already in that list "
                            "(regardless of what language or phrasing THIS bill uses for it), reuse "
                            "that EXACT existing string instead of writing a new one. Only write a new "
                            "canonical_part when this genuinely isn't one of the known ones. For a "
                            "labor/service charge with no specific part, use the same value as "
                            "description. Empty string only if description itself is empty."
                        ),
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
                "required": ["quantity", "unit", "description", "canonical_part", "cost", "category"],
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

Alongside each verbatim description, also produce canonical_part: a short \
normalized name for that exact part, used later to compare prices for the \
SAME part across different bills and suppliers. This is the one field \
where consistency matters more than matching the bill's exact wording, and \
more than matching this bill's language -- different shops often write the \
identical part in different languages or phrasing, so always normalize any \
brand name or model/part number to its standard Roman-script spelling \
(e.g. "Shell", "Rimula", "Bando", "SKF") even on an otherwise all-Thai \
bill, and check the list of already-known canonical_part names (given to \
you in the user message, if any) FIRST -- reuse an existing one exactly \
whenever a line item is the same real part, only inventing a new string \
when it genuinely isn't already in that list.

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


def _extraction_prompt_text(known_canonical_parts: Optional[list[str]]) -> str:
    if not known_canonical_parts:
        return "Read this supplier bill and extract the data."
    parts_list = "\n".join(f"- {p}" for p in known_canonical_parts)
    return (
        "Read this supplier bill and extract the data.\n\n"
        "Canonical parts already in use from past purchases -- for canonical_part, "
        "reuse one of these EXACTLY whenever a line item is the same real part "
        "(regardless of what language/phrasing THIS bill uses), even if that means "
        "writing a different language than the rest of this bill uses. Only write a "
        "new canonical_part for a line item that genuinely isn't one of these:\n"
        f"{parts_list}"
    )


def extract_supply_purchase(
    file_bytes: bytes, media_type: str, known_canonical_parts: Optional[list[str]] = None
) -> dict:
    """
    Sends one supplier-bill photo or PDF (as raw bytes) to Claude and
    returns the extracted data as a dict. media_type must be one of
    'image/jpeg', 'image/png', or 'application/pdf'.

    known_canonical_parts (see app/supplies.py's get_known_canonical_parts)
    is fed back in as matching context for the canonical_part field -- see
    this module's docstring for why that's needed to make cross-language/
    cross-shop price comparison actually work. Optional; omitting it just
    means every line item's canonical_part is a fresh guess with nothing
    to match against, same as before this parameter existed.
    """
    response = _client_lazy().messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    _content_block(file_bytes, media_type),
                    {"type": "text", "text": _extraction_prompt_text(known_canonical_parts)},
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

"""
A small, cheap first step that runs on every incoming photo/PDF BEFORE
deciding which extractor to hand it to: is this a photographed vehicle
repair bill, a bank transfer/payment confirmation slip (สลิปโอนเงิน), a
supplier bill for parts/supplies (belts, bearings, chains, breakers,
relays, etc.), or not a financial document at all?

Deliberately a separate, lightweight call on claude-sonnet-5 (same tier as
classifier.py's text intent classifier) rather than folding this into
bill_extraction.py / slip_extraction.py / supply_extraction.py's own
schemas -- those are already big, carefully-tuned prompts for their one
document type each, and mixing "what kind of document is this" into any
of them would make all three harder to maintain. This call is short and
cheap: one image in, a type + confidence out.

That 4th type -- "not_a_document" -- exists because of how these photos
actually arrive in practice: a mechanic finishes a job and sends a whole
batch of photos together (truck exterior, someone mid-repair, an odometer
close-up, a handwritten checklist with no cost figures, THEN the actual
receipt). Every one of those non-receipt photos is still its own separate
LINE message/webhook event. Before this type existed, "unclear" always
fell back to the bill-extraction flow -- meaning every single reference
photo in a batch like that would run a full (wasted) OCR pass and create
a junk pending_review row, and worse, could get sucked into
bills.find_open_chain/supplies.find_open_chain's 30-minute window and
have its garbage "line items" merged into the real receipt sitting next
to it. not_a_document photos skip extraction entirely -- see
webhook_handler._handle_non_document_photo.

confidence is the redundancy mechanism requested for this: when the model
itself isn't confident which of the three real document types this is,
OR isn't sure whether it's a real document at all vs. just a reference
photo, webhook_handler.py sends a tap-to-choose picker (which now
includes a 4th "not a bill, skip it" option) instead of guessing wrong.
Only high-confidence not_a_document skips silently with no picker --
otherwise a genuine bill that's just hard to read could get silently
dropped instead of asked about.
"""
import base64
import json
import logging
from typing import NamedTuple, Optional

from anthropic import Anthropic

from app.config import settings

logger = logging.getLogger("ticketing.image_classifier")

_client: Optional[Anthropic] = None


def _client_lazy() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=settings.anthropic_api_key)
    return _client


MODEL = "claude-sonnet-5"

# The only values Claude itself is ever asked to choose from -- see
# EXTRACTION_SCHEMA's enum. "unclear" (below, python-side only) is a
# separate, narrower sentinel: it means the classification CALL failed or
# returned something unparseable, never a real judgment call the model
# made about the photo. Keeping these separate matters -- an infra hiccup
# should still fail safe into the bill flow (today's long-standing safety
# net), while a confident not_a_document judgment should NOT go anywhere
# near bill extraction, or every reference photo in a batch creates junk.
DOCUMENT_TYPES = ["repair_bill", "payment_slip", "supply_purchase", "not_a_document"]
CONFIDENCE_LEVELS = ["high", "low"]

SYSTEM_PROMPT = """You classify one photographed document for a Thai \
ice-manufacturing business with a delivery truck fleet. Look at the image \
and decide which of these it is:

- "repair_bill": a vehicle repair/maintenance bill, invoice, or quotation \
from a mechanic shop or dealership -- usually a table of line items \
(parts, labor) with a total, either handwritten or printed on shop/ \
dealership letterhead.
- "payment_slip": a bank transfer or payment confirmation slip -- a \
screenshot from a banking app (e.g. K PLUS, SCB EASY, Krungthai NEXT) or a \
printed ATM/bank receipt, showing a transfer FROM one account TO another, \
an amount, a date/time, and usually a green checkmark or "สำเร็จ"/"transfer \
successful" indicator.
- "supply_purchase": an invoice/receipt from a hardware store, electrical \
supply shop, or industrial parts distributor for mechanical/electrical \
parts and supplies -- belts, bearings, chains, circuit breakers, magnetic \
relays, motors, seals, filters, and similar components. NOT tied to one \
specific vehicle the way a repair_bill is (no plate/mileage focus) -- \
reads as a general parts/materials purchase instead.
- "not_a_document": NOT a financial document of any kind -- a photo of a \
vehicle itself (exterior, engine bay, damage), someone doing repair work, \
an odometer/gauge close-up, a handwritten checklist or work-order note \
with no cost/price figures on it, or anything else unrelated. These often \
arrive in a batch alongside the real bill/slip photos (a mechanic \
photographing the whole job, not just the receipt) -- the KEY tell is the \
absence of any monetary total/line-item pricing table; a note that only \
lists WHAT was done, with no บาท amounts, is not_a_document even if it's \
clearly about a repair.

Also rate your confidence: "high" if you're confident in your document_type \
choice, "low" if either (a) you can tell it's LIKELY one of repair_bill / \
payment_slip / supply_purchase but genuinely aren't sure which, or (b) you \
can't tell whether this is a real financial document at all vs. just a \
reference/context photo -- e.g. a blurry or heavily cropped photo where \
you can't rule out there being a price table just out of frame. Use "low" \
honestly whenever you're genuinely torn; don't force "high" just to avoid \
saying you're unsure. For not_a_document specifically, only use "high" \
when you're confident it's genuinely not any kind of financial document at \
all (a truck photo, a gauge, a plain checklist with no prices) -- use \
"low" instead if there's a real chance it's actually a bill/slip you just \
can't read well enough to tell."""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": DOCUMENT_TYPES},
        "confidence": {"type": "string", "enum": CONFIDENCE_LEVELS},
    },
    "required": ["document_type", "confidence"],
    "additionalProperties": False,
}


def _content_block(file_bytes: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


class ImageClassification(NamedTuple):
    document_type: str  # one of DOCUMENT_TYPES, OR the "unclear" error sentinel -- see module docstring
    confidence: str  # one of CONFIDENCE_LEVELS -- "high" unless genuinely torn/unsure


def classify_image(file_bytes: bytes, media_type: str) -> ImageClassification:
    """Never raises -- a classification failure (API error, unparseable
    response) falls back to ("unclear", "high"). "unclear" is deliberately
    NOT one of DOCUMENT_TYPES -- webhook_handler.py's routing treats
    anything other than payment_slip/supply_purchase/not_a_document as the
    bill flow, so this sentinel rides that same default fallback path,
    today's long-standing safety net for "we don't actually know" -- kept
    completely separate from a genuine not_a_document judgment, which
    means the opposite (skip, don't extract)."""
    try:
        response = _client_lazy().messages.create(
            model=MODEL,
            max_tokens=100,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        _content_block(file_bytes, media_type),
                        {"type": "text", "text": "What kind of document is this?"},
                    ],
                }
            ],
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
            },
        )
        text_block = next(b.text for b in response.content if b.type == "text")
        parsed = json.loads(text_block)
        doc_type = parsed.get("document_type")
        confidence = parsed.get("confidence")
        if doc_type not in DOCUMENT_TYPES:
            logger.warning("image classifier returned unexpected document_type %r -- treating as unclear", doc_type)
            return ImageClassification("unclear", "high")
        if confidence not in CONFIDENCE_LEVELS:
            confidence = "high"  # unrecognized value -- don't force a picker over a schema hiccup
        return ImageClassification(doc_type, confidence)
    except Exception:
        logger.exception("image classification failed -- treating as unclear")
        return ImageClassification("unclear", "high")

"""
A small, cheap first step that runs on every incoming photo/PDF BEFORE
deciding which extractor to hand it to: is this a photographed vehicle
repair bill, a bank transfer/payment confirmation slip (สลิปโอนเงิน), or a
supplier bill for parts/supplies (belts, bearings, chains, breakers,
relays, etc.)?

Deliberately a separate, lightweight call on claude-sonnet-5 (same tier as
classifier.py's text intent classifier) rather than folding this into
bill_extraction.py / slip_extraction.py / supply_extraction.py's own
schemas -- those are already big, carefully-tuned prompts for their one
document type each, and mixing "what kind of document is this" into any
of them would make all three harder to maintain. This call is short and
cheap: one image in, a type + confidence out.

confidence is the redundancy mechanism requested for this: when the model
itself isn't confident which of the three real document types this is
(not "unclear" -- genuinely a real document, just ambiguous which kind),
webhook_handler.py sends a tap-to-choose picker instead of guessing wrong.
"unclear" (an unrelated/unreadable photo) is a separate case that still
falls back to the bill flow, same as before this module had more than two
categories -- there's nothing to usefully ask about for a photo that
isn't clearly any of the three in the first place.
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

DOCUMENT_TYPES = ["repair_bill", "payment_slip", "supply_purchase", "unclear"]
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
- "unclear": anything else, or a photo too unclear/unrelated to tell.

Also rate your confidence: "high" if you're confident in your document_type \
choice, "low" if you can tell it's LIKELY one of repair_bill / \
payment_slip / supply_purchase but genuinely aren't sure which -- e.g. a \
vehicle-related invoice that could plausibly be either a repair bill or a \
parts purchase depending on details you can't quite make out. Use "low" \
honestly whenever you're genuinely torn between two of the three real \
types; don't force "high" just to avoid saying you're unsure. confidence \
doesn't apply to "unclear" the same way -- use "high" there too as long as \
you're confident it's genuinely NOT any of the three (not just confidence \
about the "unclear" label itself)."""

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
    document_type: str  # one of DOCUMENT_TYPES
    confidence: str  # one of CONFIDENCE_LEVELS -- "high" unless genuinely torn between two real types


def classify_image(file_bytes: bytes, media_type: str) -> ImageClassification:
    """Never raises -- a classification failure falls back to
    ("unclear", "high"), which webhook_handler.py routes to the existing
    bill flow, today's only behavior before this classifier existed, so a
    hiccup here never regresses bill handling that already works."""
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

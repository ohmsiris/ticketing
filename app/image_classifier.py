"""
A small, cheap first step that runs on every incoming photo/PDF BEFORE
deciding which extractor to hand it to: is this a photographed vehicle
repair bill, or a bank transfer/payment confirmation slip (สลิปโอนเงิน)?

Deliberately a separate, lightweight call on claude-sonnet-5 (same tier as
classifier.py's text intent classifier) rather than folding this into
bill_extraction.py or slip_extraction.py's own schema -- those are already
big, carefully-tuned prompts for their one document type each, and mixing
"what kind of document is this" into either one would make both harder to
maintain. This call is short and cheap: one image in, one word out.
"""
import base64
import json
import logging
from typing import Optional

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

DOCUMENT_TYPES = ["repair_bill", "payment_slip", "unclear"]

SYSTEM_PROMPT = """You classify one photographed document for a Thai \
trucking/ice-delivery business. Look at the image and decide which of \
these it is:

- "repair_bill": a vehicle repair/maintenance bill, invoice, or quotation \
from a mechanic shop or dealership -- usually a table of line items \
(parts, labor) with a total, either handwritten or printed on shop/ \
dealership letterhead.
- "payment_slip": a bank transfer or payment confirmation slip -- a \
screenshot from a banking app (e.g. K PLUS, SCB EASY, Krungthai NEXT) or a \
printed ATM/bank receipt, showing a transfer FROM one account TO another, \
an amount, a date/time, and usually a green checkmark or "สำเร็จ"/"transfer \
successful" indicator.
- "unclear": anything else, or a photo too unclear/unrelated to tell.

Respond with ONLY the single matching word -- repair_bill, payment_slip, \
or unclear -- nothing else."""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": DOCUMENT_TYPES},
    },
    "required": ["document_type"],
    "additionalProperties": False,
}


def _content_block(file_bytes: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def classify_image(file_bytes: bytes, media_type: str) -> str:
    """Returns one of DOCUMENT_TYPES. Never raises -- a classification
    failure falls back to "unclear" (which webhook_handler.py routes to
    the existing bill flow, today's only behavior, so a hiccup here never
    regresses bill handling that already works)."""
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
        doc_type = json.loads(text_block).get("document_type")
        if doc_type in DOCUMENT_TYPES:
            return doc_type
        logger.warning("image classifier returned unexpected value %r -- treating as unclear", doc_type)
        return "unclear"
    except Exception:
        logger.exception("image classification failed -- treating as unclear")
        return "unclear"

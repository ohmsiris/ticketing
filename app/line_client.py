"""
Minimal LINE Messaging API client: just the three things we need
(verify a webhook signature, reply, push). Talks to the REST API directly
with httpx rather than pulling in the full line-bot-sdk, since our surface
area is tiny and this is easier to read end-to-end.

Docs:
- https://developers.line.biz/en/reference/messaging-api/#verify-signature
- https://developers.line.biz/en/reference/messaging-api/#send-reply-message
- https://developers.line.biz/en/reference/messaging-api/#send-push-message
"""
import base64
import hashlib
import hmac
import logging

import httpx

from app.config import settings

logger = logging.getLogger("ticketing.line")

REPLY_URL = "https://api.line.me/v2/bot/message/reply"
PUSH_URL = "https://api.line.me/v2/bot/message/push"


def verify_signature(body: bytes, signature: str) -> bool:
    """
    Validates the X-Line-Signature header against the raw request body.
    If LINE_CHANNEL_SECRET isn't configured yet (e.g. very first local
    setup, before the channel exists), verification is skipped with a
    warning so the rest of the flow can still be tested.
    """
    if not settings.line_channel_secret:
        logger.warning("LINE_CHANNEL_SECRET not set -- skipping signature verification")
        return True
    expected = base64.b64encode(
        hmac.new(settings.line_channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.line_channel_access_token}",
    }


def _as_messages(texts: list[str]) -> list[dict]:
    # LINE allows up to 5 message bubbles per reply/push call.
    return [{"type": "text", "text": t} for t in texts[:5]]


def reply_message(reply_token: str, texts: list[str]) -> None:
    payload = {"replyToken": reply_token, "messages": _as_messages(texts)}
    _post(REPLY_URL, payload)


def push_message(user_id: str, texts: list[str]) -> None:
    if not user_id:
        logger.warning("push_message called with no user_id configured -- skipping send")
        return
    payload = {"to": user_id, "messages": _as_messages(texts)}
    _post(PUSH_URL, payload)


def _post(url: str, payload: dict) -> None:
    try:
        resp = httpx.post(url, json=payload, headers=_headers(), timeout=10)
        if resp.status_code >= 400:
            logger.error("LINE API error %s: %s", resp.status_code, resp.text)
    except httpx.HTTPError:
        logger.exception("failed to call LINE API at %s", url)

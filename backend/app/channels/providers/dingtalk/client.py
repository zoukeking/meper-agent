"""DingTalk robot message-sending client.

DingTalk bots reply via the ``session_webhook`` carried by each inbound event
(a short-lived per-conversation reply URL, ~2h expiry). The outbound pipeline
threads that URL through ``OutboundEnvelope.context["session_webhook"]`` when
available (see ChannelService._extract_send_context).

If no session_webhook is present (e.g. a proactive push not triggered by an
inbound event), we fall back to the channel's configured ``webhook_url`` —
the robot's outgoing webhook, which broadcasts to the robot's default room.
"""
from __future__ import annotations

import logging

import httpx

from app.channels.errors import InvalidCredentialsError, SendFailedError
from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


def _resolve_webhook_url(config: ChannelConfig, session_webhook: str | None) -> str:
    """Pick the URL to POST the reply to.

    Preference order:
      1. session_webhook from the inbound event (per-conversation, short-lived)
      2. webhook_url from channel credentials (broadcast fallback)
    """
    if session_webhook:
        return session_webhook
    webhook_url_enc = config.credentials.get("webhook_url")
    if not webhook_url_enc:
        raise InvalidCredentialsError(
            "no session_webhook on envelope and no webhook_url configured "
            "for dingtalk channel — cannot send reply"
        )
    return decrypt_secret(webhook_url_enc)


async def send_text_message(
    *,
    config: ChannelConfig,
    conversation_id: str,
    text: str,
    session_webhook: str | None = None,
) -> str:
    """Send a text message back to the DingTalk conversation.

    Args:
        config: Channel config (used for webhook_url fallback).
        conversation_id: Originating conversation (unused for routing —
            session_webhook already encodes it — but kept for interface parity).
        text: Message body.
        session_webhook: Inbound-derived reply URL (preferred over webhook_url).
    """
    webhook_url = _resolve_webhook_url(config, session_webhook)

    payload = {
        "msgtype": "text",
        "text": {"content": text},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode") != 0:
                raise SendFailedError(f"dingtalk send error: {data.get('errmsg')}")
            # dingtalk's session_webhook response doesn't include a messageId;
            # synthesize a stable-ish id from the response for our log surface.
            return data.get("messageId") or data.get("msgid") or f"dt_reply_{webhook_url[-12:]}"
    except httpx.HTTPError as e:
        raise SendFailedError(f"dingtalk http error: {e}") from e

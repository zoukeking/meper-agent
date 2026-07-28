"""LarkChannel — wires verify + client into the Channel ABC."""
from __future__ import annotations

import logging

from fastapi import Request

from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.errors import (
    InvalidCredentialsError,
    SendFailedError,
    TransientChannelError,
)
from app.channels.providers.lark.client import send_text_message
from app.channels.providers.lark.verify import (
    URL_CHALLENGE_MARKER,
    LarkVerificationError,
    parse_lark_event,
    verify_lark_signature,
)
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


@ChannelRegistry.register("lark")
class LarkChannel(Channel):
    provider = "lark"

    def verify_inbound(
        self, request: Request, config: ChannelConfig
    ) -> InboundMessage | None:
        body = request._body.decode("utf-8") if request._body else ""
        timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
        signature = request.headers.get("X-Lark-Signature", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")

        try:
            verify_lark_signature(
                body=body, timestamp=timestamp, signature=signature,
                nonce=nonce, config=config,
            )
            result = parse_lark_event(body, config)
        except LarkVerificationError as e:
            logger.warning("lark verification failed: %s", e)
            raise InvalidCredentialsError(f"lark verify failed: {e}") from e

        # URL verification challenge — return None so the caller acks directly
        if isinstance(result, dict) and URL_CHALLENGE_MARKER in result:
            request.state.lark_challenge = result[URL_CHALLENGE_MARKER]
            return None
        return result

    async def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        # send_text_message raises InvalidCredentialsError / SendFailedError
        # directly. Anything else (network blip, unexpected SDK exception) is
        # treated as transient so ChannelService retries the send.
        try:
            return await send_text_message(
                config=config, receive_id=envelope.platform_chat_id, text=envelope.text,
            )
        except (InvalidCredentialsError, SendFailedError):
            raise
        except Exception as e:
            raise TransientChannelError(f"lark send transient: {e}") from e

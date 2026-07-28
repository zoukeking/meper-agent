"""WecomChannel — wires verify + decrypt + client."""
from __future__ import annotations

import logging

from fastapi import Request

from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.errors import (
    InvalidCredentialsError,
    PermanentChannelError,
    TransientChannelError,
)
from app.channels.providers.wecom.client import send_text_message
from app.channels.providers.wecom.verify import (
    WecomVerificationError,
    decrypt_wecom_message,
    extract_encrypt_from_xml,
    verify_wecom_signature,
)
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


@ChannelRegistry.register("wecom")
class WecomChannel(Channel):
    provider = "wecom"

    def verify_inbound(
        self, request: Request, config: ChannelConfig
    ) -> InboundMessage | None:
        body = request._body.decode("utf-8") if request._body else ""
        msg_signature = request.query_params.get("msg_signature", "")
        timestamp = request.query_params.get("timestamp", "")
        nonce = request.query_params.get("nonce", "")

        try:
            encrypt_body = extract_encrypt_from_xml(body)
            verify_wecom_signature(
                msg_signature=msg_signature, timestamp=timestamp, nonce=nonce,
                encrypt_body=encrypt_body, config=config,
            )
            return decrypt_wecom_message(encrypt_body, config)
        except WecomVerificationError as e:
            logger.warning("wecom verification failed: %s", e)
            raise InvalidCredentialsError(f"wecom verify failed: {e}") from e

    async def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        try:
            return await send_text_message(
                config=config,
                to_user=envelope.platform_chat_id,
                text=envelope.text,
            )
        except PermanentChannelError:
            raise
        except Exception as e:
            raise TransientChannelError(f"wecom send transient: {e}") from e

"""DingtalkChannel — wires verify + client."""
from __future__ import annotations

import logging

from fastapi import Request

from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.errors import (
    InvalidCredentialsError,
    PermanentChannelError,
    TransientChannelError,
)
from app.channels.providers.dingtalk.client import send_text_message
from app.channels.providers.dingtalk.verify import (
    DingtalkVerificationError,
    parse_dingtalk_event,
    verify_dingtalk_signature,
)
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


@ChannelRegistry.register("dingtalk")
class DingtalkChannel(Channel):
    provider = "dingtalk"

    def verify_inbound(
        self, request: Request, config: ChannelConfig
    ) -> InboundMessage | None:
        body = request._body.decode("utf-8") if request._body else ""
        timestamp = request.headers.get("timestamp", "") or request.headers.get(
            "Timestamp", ""
        )
        sign = request.headers.get("sign", "") or request.headers.get("Sign", "")

        try:
            verify_dingtalk_signature(
                timestamp=timestamp, sign=sign, config=config
            )
            return parse_dingtalk_event(body, config)
        except DingtalkVerificationError as e:
            logger.warning("dingtalk verification failed: %s", e)
            raise InvalidCredentialsError(f"dingtalk verify failed: {e}") from e

    async def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        try:
            return await send_text_message(
                config=config,
                conversation_id=envelope.platform_chat_id,
                text=envelope.text,
                session_webhook=envelope.context.get("session_webhook"),
            )
        except PermanentChannelError:
            raise
        except Exception as e:
            raise TransientChannelError(f"dingtalk send transient: {e}") from e

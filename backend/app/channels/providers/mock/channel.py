"""Mock channel adapter — local testing / CI.

Protocol (deliberately trivial):
- Inbound: POST a JSON body {message_id, chat_id, user_id, user_name?, text}.
  No signature verification (it's a test shim).
- Outbound: send() records the envelope into a module-level list so tests
  can assert on what would have been delivered.

Do NOT use in production.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import Request

from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig

# Module-global record of messages "sent" — tests assert on this.
# Cleared per-test by the test's setup_method.
MOCK_SENT_MESSAGES: list[dict] = []


@ChannelRegistry.register("mock")
class MockChannel(Channel):
    provider = "mock"

    def verify_inbound(
        self, request: Request, config: ChannelConfig
    ) -> InboundMessage | None:
        body = request._body.decode("utf-8") if request._body else "{}"
        payload = json.loads(body) if body else {}

        text = (payload.get("text") or "").strip()
        if not text:
            return None

        return InboundMessage(
            channel_id=config.id,
            platform_chat_id=payload.get("chat_id", ""),
            platform_user_id=payload.get("user_id", ""),
            platform_user_name=payload.get("user_name"),
            message_id=payload.get("message_id", ""),
            text=text,
            raw=payload,
            timestamp=payload.get("timestamp") or datetime.now(UTC),
        )

    def send(self, envelope: OutboundEnvelope, config: ChannelConfig) -> str:
        import ulid
        msg_id = f"mock_msg_{ulid.ULID()}"
        MOCK_SENT_MESSAGES.append({
            "msg_id": msg_id,
            "channel_id": envelope.channel_id,
            "platform_chat_id": envelope.platform_chat_id,
            "text": envelope.text,
            "reply_to_message_id": envelope.reply_to_message_id,
        })
        return msg_id

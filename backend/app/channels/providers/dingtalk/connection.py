"""DingTalk (钉钉) long-connection client — receive events without a public URL.

Wraps ``dingtalk_stream.DingTalkStreamClient`` (Stream mode, WebSocket reverse
connection). Unlike lark, the dingtalk SDK is natively async, so no thread
bridge is needed — the SDK runs on the same asyncio loop.

Event flow on receipt:
  ChatbotHandler.process() → build webhook-style JSON body →
  ``dispatch_inbound`` (parses, dedups, persists, enqueues Celery) →
  the same pipeline as HTTP webhook mode.
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.channels.connections.base import ConnectionClient
from app.channels.connections.dispatch import dispatch_inbound
from app.channels.connections.manager import get_connection_manager
from app.channels.errors import InvalidCredentialsError
from app.channels.providers.dingtalk.verify import parse_dingtalk_event
from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


def register_dingtalk_connection() -> None:
    """Register the dingtalk ConnectionClient factory with the global manager.

    Called from ``app.channels.providers.__init__`` so the manager knows
    dingtalk supports long-connection mode.
    """
    if not _is_long_connection_enabled():
        logger.info("dingtalk_long_connection_disabled_by_config")
        return
    get_connection_manager().register_factory("dingtalk", DingtalkConnectionClient)
    logger.info("dingtalk_long_connection_registered")


def _is_long_connection_enabled() -> bool:
    from app.core.config import settings
    return settings.CHANNEL_DINGTALK_LONG_CONNECTION_ENABLED


class DingtalkConnectionClient(ConnectionClient):
    """One Stream-mode WebSocket connection to DingTalk for one ChannelConfig."""

    def __init__(self, config: ChannelConfig) -> None:
        super().__init__(config)
        self._sdk_client = None
        self._handler: _DingtalkMessageHandler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        import dingtalk_stream

        client_id = self._get_credential("app_key")
        client_secret = self._get_credential("app_secret")

        self._loop = asyncio.get_running_loop()
        credential = dingtalk_stream.Credential(client_id, client_secret)
        self._sdk_client = dingtalk_stream.DingTalkStreamClient(credential)
        self._handler = _DingtalkMessageHandler(self)
        self._sdk_client.register_callback_handler(
            _DingtalkMessageHandler.TOPIC, self._handler,
        )

        self._connected = True
        try:
            # SDK's start() is async and blocks until disconnected. We're
            # already on the asyncio loop, so await directly.
            await self._sdk_client.start()
        except asyncio.CancelledError:
            raise
        finally:
            self._connected = False

    async def disconnect(self) -> None:
        """Best-effort disconnect. The dingtalk SDK doesn't expose a clean
        shutdown; cancellation of the manager task (which cancels connect's
        await) is the actual signal. We just clear state here."""
        self._connected = False
        self._sdk_client = None
        self._handler = None

    # ── Credential access ──

    def _get_credential(self, key: str) -> str:
        encrypted = self.config.credentials.get(key)
        if not encrypted:
            raise InvalidCredentialsError(f"missing credential: {key}")
        return decrypt_secret(encrypted)


class _DingtalkMessageHandler:
    """Custom handler (duck-typed, NOT subclassing ChatbotHandler to avoid
    SDK __init__ requirements). The dingtalk SDK calls ``process`` on
    registered handler objects; we just need the method + TOPIC attribute.

    Using a plain class avoids the SDK's ChatbotHandler constructor side
    effects and lets us capture the parent client cleanly.
    """

    TOPIC = "/v1.0/im/bot/messages/get"

    def __init__(self, owner: DingtalkConnectionClient) -> None:
        self.owner = owner
        # SDK attaches dingtalk_client to handlers after registration
        self.dingtalk_client = None

    async def process(self, callback):  # type: ignore[no-untyped-def]
        """SDK callback for each incoming chatbot message.

        ``callback`` is dingtalk_stream.CallbackMessage; its ``data`` field
        carries the inbound JSON. We forward it through dispatch_inbound
        using the same parser as webhook mode (parse_dingtalk_event).
        """
        import dingtalk_stream

        try:
            body = self._extract_body(callback)
        except Exception as exc:
            logger.warning(
                "dingtalk_stream_extract_failed channel=%s err=%s",
                self.owner.config.id, exc,
            )
            return dingtalk_stream.AckMessage.STATUS_OK, "skipped"

        await dispatch_inbound(
            config=self.owner.config, body=body, parser=parse_dingtalk_event,
        )
        return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    @staticmethod
    def _extract_body(callback) -> str:
        """Pull the inbound JSON out of the SDK's CallbackMessage.

        dingtalk's webhook receives a JSON body like:
          {"msgtype":"text","text":{"content":"..."}, "conversationId":"...",
           "senderStaffId":"...", "messageId":"...", ...}
        The stream SDK wraps this as callback.data (a dict). We re-serialize
        so parse_dingtalk_event (which takes a JSON string) can consume it.
        """
        data = getattr(callback, "data", None)
        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False)
        if isinstance(data, (bytes, bytearray)):
            return data.decode("utf-8", errors="replace")
        if isinstance(data, str):
            return data
        # Fallback: build from common fields
        logger.warning("dingtalk_stream_unexpected_callback_data type=%s", type(data))
        return "{}"

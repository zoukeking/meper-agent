"""Lark OpenAPI client — send messages via the lark-oapi SDK.

Receives events via the long-connection client (connection.py) when the
channel is in long_connection mode, or via the inbound webhook route when in
webhook mode. Both paths share this single send implementation.

Uses lark-oapi's Client (token management + retry built in) instead of raw
httpx, so we no longer maintain our own tenant_access_token cache.
"""
from __future__ import annotations

import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageResponse,
)

from app.channels.errors import InvalidCredentialsError
from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

# Cache lark.Client per app_id so we don't re-auth on every send.
# lark.Client does lazy token refresh internally.
_client_cache: dict[str, lark.Client] = {}


def _get_lark_client(config: ChannelConfig) -> lark.Client:
    """Build (and cache) a lark-oapi Client from the channel credentials."""
    app_id_enc = config.credentials.get("app_id")
    if not app_id_enc:
        raise InvalidCredentialsError("missing credential: app_id")
    app_id = decrypt_secret(app_id_enc)
    cached = _client_cache.get(app_id)
    if cached is not None:
        return cached

    app_secret_enc = config.credentials.get("app_secret")
    if not app_secret_enc:
        raise InvalidCredentialsError("missing credential: app_secret")
    app_secret = decrypt_secret(app_secret_enc)

    client = (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .log_level(lark.LogLevel.INFO)
        .build()
    )
    _client_cache[app_id] = client
    return client


def _receive_id_type(receive_id: str) -> str:
    """Infer receive_id_type from the id prefix.

    chat_id  → "oc_..." or "o_..." (group chats, typically oc_)
    open_id  → "ou_..." (user direct messages)
    """
    if receive_id.startswith("ou_"):
        return "open_id"
    return "chat_id"


async def send_text_message(
    *, config: ChannelConfig, receive_id: str, text: str
) -> str:
    """Send a text message. Returns the platform message id.

    Raises InvalidCredentialsError on auth failure, SendFailedError on
    platform-side rejection.
    """
    from app.channels.errors import SendFailedError

    client = _get_lark_client(config)
    content = json.dumps({"text": text})

    request = (
        CreateMessageRequest.builder()
        .receive_id_type(_receive_id_type(receive_id))
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(content)
            .build()
        )
        .build()
    )

    # lark-oapi's client is sync. Run in a thread to avoid blocking the loop.
    import asyncio
    response: CreateMessageResponse = await asyncio.to_thread(
        client.im.v1.message.create, request
    )

    if not response.success():
        msg = f"lark send failed: code={response.code} msg={response.msg}"
        if response.code in (99991663, 99991661, 99991664):
            # token / app_id / app_secret related codes
            raise InvalidCredentialsError(msg)
        raise SendFailedError(msg)
    if response.data is None or not response.data.message_id:
        raise SendFailedError("lark send returned no message_id")
    return response.data.message_id

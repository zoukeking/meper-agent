"""DingTalk (钉钉) signature verification + parsing.

First iteration supports plaintext callback mode:
- Header timestamp + sign; sign = Base64(HMAC-SHA256(secret, f"{ts}\n{secret}"))
- Body plaintext JSON

Encrypted mode ({"encrypt": "..."}) is recorded as an open issue — raises
DingtalkVerificationError so callers see a clear message until implemented.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

_TIMESTAMP_TOLERANCE_SECONDS = 3600


class DingtalkVerificationError(Exception):
    pass


def _get_app_secret(config: ChannelConfig) -> str:
    encrypted = config.credentials.get("app_secret")
    if not encrypted:
        raise DingtalkVerificationError("missing credential: app_secret")
    return decrypt_secret(encrypted)


def verify_dingtalk_signature(
    *, timestamp: str, sign: str, config: ChannelConfig
) -> None:
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        raise DingtalkVerificationError("invalid timestamp") from None
    if abs(int(time.time()) - ts_int) > _TIMESTAMP_TOLERANCE_SECONDS:
        raise DingtalkVerificationError("timestamp out of tolerance (replay?)")

    secret = _get_app_secret(config)
    string_to_sign = f"{timestamp}\n{secret}"
    expected = base64.b64encode(hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()).decode("utf-8")
    if not hmac.compare_digest(expected, sign):
        raise DingtalkVerificationError("signature mismatch")


def parse_dingtalk_event(body: str, config: ChannelConfig):
    from datetime import UTC, datetime

    from app.channels.base import InboundMessage

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise DingtalkVerificationError("body is not valid JSON") from None

    # Encrypted mode — not supported in first iteration
    if "encrypt" in payload:
        raise DingtalkVerificationError(
            "encrypted callback not yet supported (configure plaintext mode)"
        )

    if payload.get("msgtype") != "text":
        return None
    text = (payload.get("text", {}).get("content") or "").strip()
    if not text:
        return None

    return InboundMessage(
        channel_id=config.id,
        platform_chat_id=payload.get("conversationId", ""),
        platform_user_id=payload.get("senderStaffId", ""),
        message_id=payload.get("messageId", ""),
        text=text,
        raw=payload,
        timestamp=datetime.now(UTC),
    )

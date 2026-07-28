"""Lark (飞书) signature verification + event parsing.

Verification rules (v2 events):
- X-Lark-Signature header = "sha256=" + HMAC-SHA256(app_secret, timestamp + body)
- Reject if |now - timestamp| > 3600s (replay protection)
- URL verification: body {"challenge":..., "token":...} → respond {"challenge":...}
- Encrypted body (optional encrypt_key): {"encrypt": "<base64 AES-256-CBC>"}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

URL_CHALLENGE_MARKER = "__url_challenge__"
_TIMESTAMP_TOLERANCE_SECONDS = 3600


class LarkVerificationError(Exception):
    """Raised when signature/timestamp/token verification fails."""


def _get_credential(config: ChannelConfig, key: str) -> str:
    """Fetch a decrypted credential value from config.credentials."""
    encrypted = config.credentials.get(key)
    if not encrypted:
        raise LarkVerificationError(f"missing credential: {key}")
    return decrypt_secret(encrypted)


def verify_lark_signature(
    *, body: str, timestamp: str, signature: str, config: ChannelConfig,
    nonce: str = "",
) -> None:
    """Verify X-Lark-Signature. Raises LarkVerificationError on failure.

    If the request has no signature headers (timestamp/signature empty),
    we skip signature verification — this happens when the app has no
    Encrypt Key configured. The webhook_secret URL parameter (checked by
    the inbound route) already provides basic anti-forgery protection in
    that case.
    """
    # No signature headers → app has no Encrypt Key → skip signature check.
    # The inbound route's webhook_secret gate is the sole protection here.
    if not timestamp or not signature:
        return

    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        raise LarkVerificationError("invalid timestamp format") from None
    if abs(int(time.time()) - ts_int) > _TIMESTAMP_TOLERANCE_SECONDS:
        raise LarkVerificationError("timestamp out of tolerance (replay?)")

    # Signature algorithm (when Encrypt Key is configured):
    #   sha256(timestamp + nonce + encrypt_key + body)
    # See https://open.feishu.cn/document/server-docs/event-subscription-guide/
    encrypt_key = config.credentials.get("encrypt_key")
    if encrypt_key:
        key = decrypt_secret(encrypt_key)
    else:
        # Fallback: some legacy setups sign with app_secret via HMAC.
        key = _get_credential(config, "app_secret")
        msg = f"{timestamp}{body}".encode()
        expected = "sha256=" + hmac.new(
            key.encode("utf-8"), msg, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise LarkVerificationError("signature mismatch")
        return

    msg = f"{timestamp}{nonce}{key}{body}".encode()
    expected = hashlib.sha256(msg).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise LarkVerificationError("signature mismatch")


def _maybe_decrypt_body(body: str, config: ChannelConfig) -> str:
    """If body is {"encrypt": "..."}, decrypt with encrypt_key. Else return as-is."""
    try:
        wrapped = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(wrapped, dict) or "encrypt" not in wrapped:
        return body

    encrypt_key = config.credentials.get("encrypt_key")
    if not encrypt_key:
        raise LarkVerificationError("received encrypted body but no encrypt_key configured")
    key = decrypt_secret(encrypt_key)
    return _aes_decrypt(wrapped["encrypt"], key)


def _aes_decrypt(ciphertext_b64: str, key: str) -> str:
    """Lark AES-256-CBC decrypt: key = SHA256(app_encrypt_key)[:32], IV = first 16 bytes."""
    import base64

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    raw = base64.b64decode(ciphertext_b64)
    iv, ciphertext = raw[:16], raw[16:]
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode("utf-8")


def parse_lark_event(body: str, config: ChannelConfig):
    """Parse a (verified) Lark event body.

    Returns:
        dict {URL_CHALLENGE_MARKER: challenge} for URL verification (caller acks).
        InboundMessage for a text message.
        None for non-text messages / events we don't process.
    """
    from datetime import UTC, datetime

    from app.channels.base import InboundMessage

    body = _maybe_decrypt_body(body, config)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise LarkVerificationError("body is not valid JSON") from None

    # URL verification flow
    if "challenge" in payload:
        token = payload.get("token", "")
        expected_token = _get_credential(config, "verification_token")
        if not hmac.compare_digest(token, expected_token):
            raise LarkVerificationError("verification_token mismatch")
        return {URL_CHALLENGE_MARKER: payload["challenge"]}

    # v2 event envelope
    header = payload.get("header", {})
    event = payload.get("event", {})
    event_type = header.get("event_type", "")
    if event_type != "im.message.receive_v1":
        return None

    msg_obj = event.get("message", {})
    if msg_obj.get("message_type") != "text":
        return None

    try:
        content = json.loads(msg_obj.get("content", "{}"))
    except json.JSONDecodeError:
        return None
    text = (content.get("text") or "").strip()
    if not text:
        return None

    sender_id = event.get("sender", {}).get("sender_id", {})
    return InboundMessage(
        channel_id=config.id,
        platform_chat_id=msg_obj.get("chat_id", ""),
        platform_user_id=sender_id.get("open_id", ""),
        platform_user_name=None,
        message_id=msg_obj.get("message_id", ""),
        text=text,
        raw=payload,
        timestamp=datetime.now(UTC),
    )

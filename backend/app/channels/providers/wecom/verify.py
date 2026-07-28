"""WeCom (企业微信) callback verification + AES decryption.

WeCom only supports encrypted callbacks (no plaintext mode):
- Query params: msg_signature, timestamp, nonce
- msg_signature = SHA1(sorted([token, timestamp, nonce, encrypt_body]).join)
- Body: XML <xml><Encrypt><![CDATA[base64]]></Encrypt></xml>
- AES-256-CBC: key = Base64Decode(encoding_aes_key + "="), iv = key[:16]
- Plaintext format: 16 random + 4-byte big-endian msg_len + msg + corp_id
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import struct
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from app.core.crypto import decrypt_secret
from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)


class WecomVerificationError(Exception):
    pass


def _cred(config: ChannelConfig, key: str) -> str:
    enc = config.credentials.get(key)
    if not enc:
        raise WecomVerificationError(f"missing credential: {key}")
    return decrypt_secret(enc)


def _aes_key(encoding: str) -> bytes:
    """WeCom AES key = Base64Decode(encoding_aes_key + "=") → 32 bytes."""
    return base64.b64decode(encoding + "=")


def verify_wecom_signature(
    *, msg_signature: str, timestamp: str, nonce: str,
    encrypt_body: str, config: ChannelConfig,
) -> None:
    """Verify msg_signature = SHA1(sorted([token, ts, nonce, encrypt_body]).join).

    WeCom uses a plain SHA1 over the lexicographically-sorted concatenation
    (NOT HMAC). Use hmac.compare_digest for the final compare to avoid timing
    leaks on the signature itself.
    """
    token = _cred(config, "token")
    parts = sorted([token, timestamp, nonce, encrypt_body])
    expected = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected, msg_signature):
        raise WecomVerificationError("msg_signature mismatch")


def _aes_decrypt(
    ciphertext_b64: str, encoding_aes_key: str, expected_corp_id: str
) -> bytes:
    """AES-256-CBC decrypt + unpad + corp_id check. Returns the inner XML bytes."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _aes_key(encoding_aes_key)
    iv = key[:16]
    raw = base64.b64decode(ciphertext_b64)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(raw) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    msg_len = struct.unpack(">I", plain[16:20])[0]
    msg = plain[20:20 + msg_len]
    corp_id = plain[20 + msg_len:].decode("utf-8")
    if corp_id != expected_corp_id:
        raise WecomVerificationError(f"corp_id mismatch: {corp_id}")
    return msg


def decrypt_wecom_message(encrypt_b64: str, config: ChannelConfig):
    """Decrypt + parse a WeCom callback into InboundMessage.

    Returns None for non-text messages or empty content (downstream ignores
    them). Raises WecomVerificationError on decrypt/corp_id failure.
    """
    from app.channels.base import InboundMessage

    encoding = _cred(config, "encoding_aes_key")
    corp_id = _cred(config, "corp_id")
    plain = _aes_decrypt(encrypt_b64, encoding, corp_id)
    root = ET.fromstring(plain)

    msg_type = (root.findtext("MsgType") or "").strip()
    if msg_type != "text":
        return None
    content = (root.findtext("Content") or "").strip()
    if not content:
        return None

    return InboundMessage(
        channel_id=config.id,
        platform_chat_id=root.findtext("FromUserName") or "",
        platform_user_id=root.findtext("FromUserName") or "",
        message_id=root.findtext("MsgId") or "",
        text=content,
        raw={"xml": plain.decode("utf-8")},
        timestamp=datetime.now(UTC),
    )


def extract_encrypt_from_xml(body: str) -> str:
    """Pull the <Encrypt> CDATA out of the callback body."""
    root = ET.fromstring(body)
    enc = root.findtext("Encrypt")
    if not enc:
        raise WecomVerificationError("no <Encrypt> in body")
    return enc

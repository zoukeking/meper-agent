"""WeCom (企业微信) signature verification + decryption tests."""
import base64
import hashlib
import os
import struct
import time

import pytest
from app.channels.providers.wecom.verify import (
    WecomVerificationError,
    decrypt_wecom_message,
    verify_wecom_signature,
)
from app.core.crypto import encrypt_secret
from app.models.channel import ChannelConfig, ChannelProvider

# WeCom encoding_aes_key MUST be 43 base64 chars (decodes to a 32-byte AES key
# when a single "=" padding char is appended). Use a fixed valid value so the
# test is deterministic.
_ENCODING_AES_KEY_RAW = "CDjmNCsYV3EJfXZZp8zPYkhmnUQFbOuNxYSRv2QocKg"  # 43 chars
_TOKEN = "test_wecom_token_Qm"
_CORP_ID = "test_corp_id"


def _make_config() -> ChannelConfig:
    return ChannelConfig(
        name="wecom-test", provider=ChannelProvider.WECOM,
        agent_id="a", owner_user_id="u",
        webhook_secret="wecom_secondary_secret_",
        credentials={
            "token": encrypt_secret(_TOKEN),
            "encoding_aes_key": encrypt_secret(_ENCODING_AES_KEY_RAW),
            "corp_id": encrypt_secret(_CORP_ID),
        },
    )


def _aes_key_from_encoding(encoding: str) -> bytes:
    """WeCom AES key = Base64Decode(encoding + "=")."""
    return base64.b64decode(encoding + "=")


def _encrypt_wecom(plain_body: bytes) -> str:
    """Helper: encrypt a WeCom message body, return base64 ciphertext."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _aes_key_from_encoding(_ENCODING_AES_KEY_RAW)
    iv = key[:16]
    # WeCom format: 16 random bytes + 4-byte big-endian msg_len + msg + corp_id
    rand = os.urandom(16)
    msg_len = struct.pack(">I", len(plain_body))
    plain = rand + msg_len + plain_body + _CORP_ID.encode()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ct).decode("utf-8")


class TestVerifyWecomSignature:
    def test_valid_signature_passes(self):
        timestamp = str(int(time.time()))
        nonce = "nonce_abc"
        encrypt = _encrypt_wecom(b"<xml/>")
        parts = sorted([_TOKEN, timestamp, nonce, encrypt])
        sig = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
        verify_wecom_signature(
            msg_signature=sig, timestamp=timestamp, nonce=nonce,
            encrypt_body=encrypt, config=_make_config(),
        )

    def test_invalid_signature_raises(self):
        with pytest.raises(WecomVerificationError):
            verify_wecom_signature(
                msg_signature="bad", timestamp="1", nonce="x",
                encrypt_body="y", config=_make_config(),
            )


class TestDecryptWecomMessage:
    def test_decrypt_text_message(self):
        inner_xml = (
            "<xml><MsgId>msg_001</MsgId><FromUserName>u_001</FromUserName>"
            "<MsgType>text</MsgType><Content>你好</Content></xml>"
        )
        encrypt = _encrypt_wecom(inner_xml.encode("utf-8"))
        msg = decrypt_wecom_message(encrypt, _make_config())
        assert msg is not None
        assert msg.message_id == "msg_001"
        assert msg.platform_user_id == "u_001"
        assert msg.text == "你好"

    def test_decrypt_non_text_returns_none(self):
        inner_xml = (
            "<xml><MsgId>msg_002</MsgId><FromUserName>u_002</FromUserName>"
            "<MsgType>image</MsgType><Content>你好</Content></xml>"
        )
        encrypt = _encrypt_wecom(inner_xml.encode("utf-8"))
        assert decrypt_wecom_message(encrypt, _make_config()) is None

    def test_decrypt_corp_id_mismatch_raises(self):
        inner_xml = (
            "<xml><MsgId>msg_003</MsgId><FromUserName>u_003</FromUserName>"
            "<MsgType>text</MsgType><Content>x</Content></xml>"
        )
        # Encrypt with a different corp_id appended (forgery simulation).
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = _aes_key_from_encoding(_ENCODING_AES_KEY_RAW)
        iv = key[:16]
        rand = os.urandom(16)
        body = inner_xml.encode("utf-8")
        msg_len = struct.pack(">I", len(body))
        plain = rand + msg_len + body + b"WRONG_CORP_ID"
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        ct = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        encrypt = base64.b64encode(
            ct.update(padded) + ct.finalize()
        ).decode("utf-8")
        with pytest.raises(WecomVerificationError, match="corp_id"):
            decrypt_wecom_message(encrypt, _make_config())

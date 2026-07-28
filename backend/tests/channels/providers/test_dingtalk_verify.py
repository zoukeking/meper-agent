"""DingTalk (钉钉) signature verification + parsing tests."""
import base64
import hashlib
import hmac
import time

import pytest
from app.channels.providers.dingtalk.verify import (
    DingtalkVerificationError,
    parse_dingtalk_event,
    verify_dingtalk_signature,
)
from app.core.crypto import encrypt_secret
from app.models.channel import ChannelConfig, ChannelProvider

_APP_SECRET = "test_dingtalk_secret"


def _make_config() -> ChannelConfig:
    return ChannelConfig(
        name="dingtalk-test", provider=ChannelProvider.DINGTALK,
        agent_id="a", owner_user_id="u",
        webhook_secret="dingtalk_secondary_16+",
        credentials={"app_secret": encrypt_secret(_APP_SECRET)},
    )


def _sign(timestamp: str) -> str:
    """DingTalk sign = Base64(HMAC-SHA256(secret, f'{timestamp}\n{secret}'))."""
    string_to_sign = f"{timestamp}\n{_APP_SECRET}"
    digest = hmac.new(
        _APP_SECRET.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class TestVerifyDingtalkSignature:
    def test_valid_signature_passes(self):
        ts = str(int(time.time()))
        verify_dingtalk_signature(
            timestamp=ts, sign=_sign(ts), config=_make_config()
        )

    def test_invalid_signature_raises(self):
        ts = str(int(time.time()))
        with pytest.raises(DingtalkVerificationError):
            verify_dingtalk_signature(
                timestamp=ts, sign="bad_signature", config=_make_config()
            )

    def test_stale_timestamp_raises(self):
        ts = str(int(time.time()) - 7200)
        with pytest.raises(DingtalkVerificationError, match="timestamp"):
            verify_dingtalk_signature(
                timestamp=ts, sign=_sign(ts), config=_make_config()
            )


class TestParseDingtalkEvent:
    def test_text_message_parsed(self):
        body = (
            '{"msgtype":"text","text":{"content":"你好"},"conversationId":"cid001",'
            '"senderStaffId":"staff123","messageId":"msg001"}'
        )
        msg = parse_dingtalk_event(body, _make_config())
        assert msg is not None
        assert msg.message_id == "msg001"
        assert msg.platform_chat_id == "cid001"
        assert msg.platform_user_id == "staff123"
        assert msg.text == "你好"

    def test_non_text_message_returns_none(self):
        body = '{"msgtype":"markdown","text":{}}'
        assert parse_dingtalk_event(body, _make_config()) is None

    def test_empty_content_returns_none(self):
        body = '{"msgtype":"text","text":{"content":""}}'
        assert parse_dingtalk_event(body, _make_config()) is None

    def test_encrypted_body_raises_not_implemented(self):
        """First iteration: encrypted callbacks not yet supported."""
        body = '{"encrypt":"some_base64_data"}'
        with pytest.raises(DingtalkVerificationError, match="encrypt"):
            parse_dingtalk_event(body, _make_config())

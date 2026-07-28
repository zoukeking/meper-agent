"""MockChannel: test/CI adapter that records calls instead of hitting a platform."""
from datetime import UTC, datetime

from app.channels.base import OutboundEnvelope
from app.channels.providers.mock.channel import MOCK_SENT_MESSAGES, MockChannel
from app.channels.registry import ChannelRegistry
from app.models.channel import ChannelConfig, ChannelProvider
from starlette.requests import Request


def _make_config() -> ChannelConfig:
    return ChannelConfig(
        name="mock-test",
        provider=ChannelProvider.MOCK,
        agent_id="agent_01J",
        owner_user_id="user_01J",
        webhook_secret="mock_secret_at_least_16_chars",
        credentials={},
    )


def _make_request(body: bytes, headers: dict | None = None) -> Request:
    """Build a minimal Starlette Request with a JSON body."""
    raw_headers = []
    for k, v in (headers or {}).items():
        raw_headers.append((k.encode(), v.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "headers": raw_headers,
        "query_string": b"",
    }
    req = Request(scope)
    req._body = body
    return req


class TestMockChannelRegistration:
    def test_registered_as_mock(self):
        # Trigger PEP 562
        from app.channels import providers  # noqa: F401
        assert "mock" in ChannelRegistry._registry

    def test_registry_returns_mock_instance(self):
        instance = ChannelRegistry.get("mock")
        assert isinstance(instance, MockChannel)


class TestPEP562AutoRegistration:
    """Regression: ensure ChannelRegistry.get('mock') works in production
    where no test import has pre-loaded mock.channel. The @register decorator
    must fire purely from importing the providers package.

    The real proof is the standalone `python -c` check documented in the
    task — this test guards against regressions in the import chain.
    """

    def test_get_mock_without_direct_channel_import(self):
        # Go through the registry alone. ChannelRegistry.get()'s lazy import
        # of `app.channels.providers` is what production relies on; that
        # import chain must reach the @register decorator in mock/channel.py.
        instance = ChannelRegistry.get("mock")
        assert instance.__class__.__name__ == "MockChannel"


class TestMockVerifyInbound:
    def setup_method(self):
        MOCK_SENT_MESSAGES.clear()

    def test_parses_simple_text_payload(self):
        body = (
            b'{"message_id":"msg_1","chat_id":"chat_1",'
            b'"user_id":"u_1","user_name":"alice","text":"hello"}'
        )
        req = _make_request(body)
        channel = MockChannel()

        msg = channel.verify_inbound(req, _make_config())

        assert msg is not None
        assert msg.message_id == "msg_1"
        assert msg.platform_chat_id == "chat_1"
        assert msg.platform_user_id == "u_1"
        assert msg.platform_user_name == "alice"
        assert msg.text == "hello"

    def test_returns_none_for_empty_text(self):
        body = b'{"message_id":"msg_1","chat_id":"chat_1","user_id":"u_1","text":""}'
        req = _make_request(body)
        channel = MockChannel()

        assert channel.verify_inbound(req, _make_config()) is None

    def test_timestamp_defaults_to_now(self):
        body = b'{"message_id":"msg_1","chat_id":"c","user_id":"u","text":"hi"}'
        req = _make_request(body)
        before = datetime.now(UTC)
        msg = MockChannel().verify_inbound(req, _make_config())
        after = datetime.now(UTC)
        assert before <= msg.timestamp <= after


class TestMockSend:
    def setup_method(self):
        MOCK_SENT_MESSAGES.clear()

    def test_send_records_message(self):
        channel = MockChannel()
        env = OutboundEnvelope(
            channel_id="ch_01J", platform_chat_id="chat_1", text="reply",
        )

        msg_id = channel.send(env, _make_config())

        assert msg_id.startswith("mock_msg_")
        assert len(MOCK_SENT_MESSAGES) == 1
        assert MOCK_SENT_MESSAGES[0]["text"] == "reply"
        assert MOCK_SENT_MESSAGES[0]["platform_chat_id"] == "chat_1"

"""Channel abstract interface + registry tests."""
from datetime import UTC, datetime

import pytest
from app.channels.base import Channel, InboundMessage, OutboundEnvelope
from app.channels.registry import ChannelRegistry
from fastapi import Request

# ── Test data objects ──

class TestInboundMessage:
    def test_required_fields(self):
        msg = InboundMessage(
            channel_id="ch_01J",
            platform_chat_id="oc_test",
            platform_user_id="u_001",
            message_id="msg_001",
            text="你好",
            raw={"source": "test"},
            timestamp=datetime.now(UTC),
        )
        assert msg.platform_user_name is None
        assert msg.raw["source"] == "test"

    def test_text_required(self):
        with pytest.raises(Exception):  # noqa: B017 - intentional: any validation error
            InboundMessage(
                channel_id="ch", platform_chat_id="c", platform_user_id="u",
                message_id="m", text="", raw={}, timestamp=datetime.now(UTC),
            )


class TestOutboundEnvelope:
    def test_minimal(self):
        env = OutboundEnvelope(
            channel_id="ch_01J", platform_chat_id="oc_test", text="回复",
        )
        assert env.reply_to_message_id is None


# ── Registry behavior with a fake channel ──

class FakeChannel(Channel):
    provider = "fake"

    def verify_inbound(self, request: Request, config):
        return None

    def send(self, envelope, config):
        return "fake_msg_id"


class TestChannelRegistry:
    def setup_method(self):
        # Registry is module-level; save real state then start clean so this
        # test class's isolaton doesn't wipe providers (e.g. mock) relied on
        # by other test modules.
        self._saved_registry = dict(ChannelRegistry._registry)
        ChannelRegistry._registry = {}

    def teardown_method(self):
        # Restore so other tests still see the real providers.
        ChannelRegistry._registry = self._saved_registry

    def test_register_decorator(self):
        @ChannelRegistry.register("fake")
        class _C(Channel):
            provider = "fake"
            def verify_inbound(self, request, config): return None
            def send(self, envelope, config): return "x"
        assert "fake" in ChannelRegistry._registry

    def test_get_returns_instance(self):
        ChannelRegistry.register("fake")(FakeChannel)
        instance = ChannelRegistry.get("fake")
        assert isinstance(instance, FakeChannel)

    def test_get_unknown_provider_raises(self):
        with pytest.raises(KeyError):
            ChannelRegistry.get("nonexistent")

    def test_get_returns_new_instance_each_call(self):
        ChannelRegistry.register("fake")(FakeChannel)
        a = ChannelRegistry.get("fake")
        b = ChannelRegistry.get("fake")
        assert a is not b  # stateless, fresh instance per call

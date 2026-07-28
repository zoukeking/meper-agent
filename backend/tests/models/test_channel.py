"""ChannelConfig / InboundEventLog model tests."""
import pytest
from app.models.channel import (
    ChannelConfig,
    ChannelProvider,
    ChannelStatus,
    InboundEventLog,
    InboundEventLogStatus,
)


class TestChannelConfig:
    def test_default_id_has_ch_prefix(self):
        cfg = ChannelConfig(
            name="售后-飞书",
            provider=ChannelProvider.LARK,
            agent_id="agent_01J",
            owner_user_id="user_01J",
            webhook_secret="a" * 32,
        )
        assert cfg.id.startswith("ch_")
        assert cfg.enabled is True
        assert cfg.status == ChannelStatus.ACTIVE
        assert cfg.consecutive_failures == 0
        assert cfg.receive_mode == "webhook"

    def test_provider_enum_values(self):
        assert ChannelProvider.LARK == "lark"
        assert ChannelProvider.DINGTALK == "dingtalk"
        assert ChannelProvider.WECOM == "wecom"
        assert ChannelProvider.MOCK == "mock"

    def test_status_enum_values(self):
        assert ChannelStatus.ACTIVE == "active"
        assert ChannelStatus.DEGRADED == "degraded"
        assert ChannelStatus.DISABLED == "disabled"

    def test_credentials_defaults_to_empty_dict(self):
        cfg = ChannelConfig(
            name="x", provider=ChannelProvider.MOCK,
            agent_id="a", owner_user_id="u", webhook_secret="b" * 32,
        )
        assert cfg.credentials == {}

    def test_populate_by_alias(self):
        """Model can be constructed with _id alias and dumped back with alias."""
        cfg = ChannelConfig(
            _id="ch_test",
            name="x", provider=ChannelProvider.MOCK,
            agent_id="a", owner_user_id="u", webhook_secret="b" * 32,
        )
        dumped = cfg.model_dump(by_alias=True)
        assert dumped["_id"] == "ch_test"
        assert dumped["provider"] == "mock"

    def test_name_required(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ChannelConfig(
                provider=ChannelProvider.MOCK,
                agent_id="a", owner_user_id="u", webhook_secret="b" * 32,
            )


class TestInboundEventLog:
    def test_default_id_has_inb_prefix(self):
        log = InboundEventLog(
            channel_id="ch_01J",
            platform_message_id="msg_001",
            payload={"text": "hi"},
        )
        assert log.id.startswith("inb_")
        assert log.status == InboundEventLogStatus.PENDING
        assert log.processed_at is None
        assert log.error is None

    def test_status_enum_values(self):
        assert InboundEventLogStatus.PENDING == "pending"
        assert InboundEventLogStatus.PROCESSING == "processing"
        assert InboundEventLogStatus.DONE == "done"
        assert InboundEventLogStatus.FAILED == "failed"

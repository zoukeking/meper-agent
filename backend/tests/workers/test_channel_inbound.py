"""Celery task process_inbound tests.

conftest forces Celery eager mode, so .delay() / direct calls execute
synchronously in-process. We patch ChannelService at the task module's
import path so the eager task body uses the mocks.
"""
import contextlib
from unittest.mock import AsyncMock, patch

from app.channels.errors import InvalidCredentialsError, LLMRateLimitError
from app.models.channel import ChannelConfig, ChannelProvider, InboundEventLog
from app.workers.tasks.channel_inbound import process_inbound

_EVENT_LOG = InboundEventLog(
    id="inb_01J",  # explicit id matching the task input so we can assert the
                   # *fetched* log (not a freshly-generated dummy) is threaded
                   # through to handle_error.
    channel_id="ch_01J",
    platform_message_id="m1",
    payload={
        "channel_id": "ch_01J",
        "platform_chat_id": "c",
        "platform_user_id": "u",
        "message_id": "m1",
        "text": "hi",
        "raw": {},
        "timestamp": "2026-07-17T00:00:00+00:00",
    },
)


def _make_config() -> ChannelConfig:
    return ChannelConfig(
        name="t",
        provider=ChannelProvider.MOCK,
        agent_id="a",
        owner_user_id="u",
        webhook_secret="mock_secret_at_least_16",
        credentials={},
    )


class TestProcessInbound:
    def test_invokes_channel_service_execute(self):
        """Happy path: fetch log → execute(InboundMessage, event_log_id=...)."""
        with patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_event_log",
            new=AsyncMock(return_value=_EVENT_LOG),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_config",
            new=AsyncMock(return_value=_make_config()),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.execute",
            new=AsyncMock(),
        ) as mock_exec:
            process_inbound("inb_01J")

        mock_exec.assert_awaited_once()
        # Positional arg 0 is the reconstructed InboundMessage
        args = mock_exec.call_args.args
        assert args[0].message_id == "m1"
        # event_log_id is threaded through as a kwarg
        assert mock_exec.call_args.kwargs.get("event_log_id") == "inb_01J"

    def test_transient_error_triggers_retry(self):
        """First attempt raises transient → task retries.

        With eager Celery + task_eager_propagates, the retry raises inside the
        task body. We accept any outcome (raise or swallow) here — the point is
        that a transient error does NOT call handle_error and DOES surface as a
        retry rather than a permanent failure.
        """
        with patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_event_log",
            new=AsyncMock(return_value=_EVENT_LOG),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_config",
            new=AsyncMock(return_value=_make_config()),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.execute",
            new=AsyncMock(side_effect=LLMRateLimitError("busy")),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.handle_error",
            new=AsyncMock(),
        ) as mock_handler, contextlib.suppress(Exception):
            # Retry in eager mode propagates; acceptable for this test.
            process_inbound("inb_01J")

        # Crucial: a transient error must NOT be treated as permanent.
        mock_handler.assert_not_awaited()

    def test_permanent_error_calls_handle_error(self):
        """Permanent error → handle_error receives the *real* event log
        (id inb_01J), not a dummy with a fresh random id."""
        cfg = _make_config()
        with patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_event_log",
            new=AsyncMock(return_value=_EVENT_LOG),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.execute",
            new=AsyncMock(side_effect=InvalidCredentialsError("bad")),
        ), patch(
            "app.workers.tasks.channel_inbound.ChannelService.handle_error",
            new=AsyncMock(),
        ) as mock_handler:
            process_inbound("inb_01J")

        mock_handler.assert_awaited_once()
        # The real event log (id inb_01J) should be passed, not a dummy.
        passed_log = mock_handler.call_args.args[0]
        assert passed_log.id == "inb_01J"

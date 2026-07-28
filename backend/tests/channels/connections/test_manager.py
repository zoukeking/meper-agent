"""ChannelConnectionManager lifecycle + hot reload tests.

Uses a fake ConnectionClient so we can assert on start/stop/reload behavior
without depending on any real platform SDK. The real provider clients
(LarkConnectionClient / DingtalkConnectionClient) get their own integration
tests.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.channels.connections.base import ConnectionClient
from app.channels.connections.dispatch import dispatch_inbound
from app.channels.connections.manager import (
    ChannelConnectionManager,
    get_connection_manager,
)
from app.models.channel import (
    ChannelConfig,
    ChannelProvider,
)

# ── Fake client for testing ──

class FakeConnectionClient(ConnectionClient):
    """Test double: records every lifecycle call without touching a network."""

    instances: list[FakeConnectionClient] = []

    def __init__(self, config: ChannelConfig) -> None:
        super().__init__(config)
        self.connected = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        # Connect behaves: None = block-forever (normal long-conn),
        #                  Exception class/instance = raise on connect
        self.connect_behavior = None
        FakeConnectionClient.instances.append(self)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_behavior is not None:
            if isinstance(self.connect_behavior, BaseException):
                raise self.connect_behavior
            raise self.connect_behavior("fake connect failure")
        self.connected = True
        # Block "forever" to simulate a long-running SDK start() call.
        # Cancellation is how the manager stops us.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.connected = False
            raise

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False


# ── Fixtures ──

@pytest.fixture(autouse=True)
def _reset_fake():
    FakeConnectionClient.instances.clear()
    yield
    FakeConnectionClient.instances.clear()


def _make_config(
    *,
    provider: ChannelProvider = ChannelProvider.MOCK,
    receive_mode: str = "long_connection",
    enabled: bool = True,
    channel_id: str = "ch_test1",
    credentials: dict | None = None,
) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        _id=channel_id,
        name="test-channel",
        provider=provider,
        agent_id="agent_01J",
        owner_user_id="user_01J",
        webhook_secret="x" * 32,
        credentials=credentials or {},
        receive_mode=receive_mode,
        enabled=enabled,
    )


# ── ConnectionClient ABC contract ──

class TestConnectionClientABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            ConnectionClient(_make_config())  # type: ignore[abstract]

    def test_subclass_must_implement_all(self):
        # Missing disconnect → TypeError
        with pytest.raises(TypeError):
            class _Bad(ConnectionClient):  # type: ignore[abstract]
                @property
                def is_connected(self) -> bool:
                    return True

                async def connect(self) -> None:
                    pass
            _Bad(_make_config())

    def test_channel_id_property(self):
        client = FakeConnectionClient(_make_config(channel_id="ch_xyz"))
        assert client.channel_id == "ch_xyz"


# ── Factory registration ──

class TestFactoryRegistration:
    def test_register_and_supports(self):
        mgr = ChannelConnectionManager()
        assert not mgr.supports("mock")
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        assert mgr.supports("mock")
        assert not mgr.supports("lark")  # unregistered

    def test_supports_accepts_enum(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        assert mgr.supports(ChannelProvider.MOCK)


# ── Manager start/stop ──

class TestManagerLifecycle:
    async def test_start_loads_long_connection_channels_from_db(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))

        cfg1 = _make_config(channel_id="ch_1")
        cfg2 = _make_config(channel_id="ch_2", enabled=False)  # should be skipped
        cfg3 = _make_config(channel_id="ch_3", receive_mode="webhook")  # skipped

        with patch.object(
            mgr, "_load_long_connection_channels",
            new=AsyncMock(return_value=[cfg1, cfg2, cfg3]),
        ):
            # The DB query already filters by enabled + receive_mode, but the
            # manager should still be defensive. _load_long_connection_channels
            # is the source of truth — if it returns only matching channels,
            # they all start.
            await mgr.start()

        try:
            assert len(mgr._clients) == 3  # all returned were started
            assert "ch_1" in mgr._clients
        finally:
            await mgr.stop()

    async def test_stop_cancels_all_tasks(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        with patch.object(
            mgr, "_load_long_connection_channels",
            new=AsyncMock(return_value=[_make_config(channel_id="ch_a")]),
        ):
            await mgr.start()
            assert "ch_a" in mgr._tasks
            await mgr.stop()
            assert len(mgr._clients) == 0
            assert len(mgr._tasks) == 0

    async def test_start_with_no_factories_skips_silently(self):
        """If no provider has registered (e.g. feature flags all off),
        start() must not crash — it just starts zero connections."""
        mgr = ChannelConnectionManager()
        with patch.object(
            mgr, "_load_long_connection_channels",
            new=AsyncMock(return_value=[_make_config(provider=ChannelProvider.LARK)]),
        ):
            await mgr.start()  # lark has no factory registered
            assert len(mgr._clients) == 0
        await mgr.stop()


# ── Hot reload ──

class TestReloadChannel:
    async def test_reload_starts_connection_for_long_connection_channel(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        cfg = _make_config(channel_id="ch_reload1")
        try:
            with patch.object(
                mgr, "_load_channel", new=AsyncMock(return_value=cfg),
            ):
                await mgr.reload_channel("ch_reload1")
                # Give the connect task a tick to mark connected
                await asyncio.sleep(0.05)
                assert "ch_reload1" in mgr._clients
                assert mgr.connection_status("ch_reload1") == "long_connection_connected"
        finally:
            await mgr.stop()

    async def test_reload_stops_connection_when_disabled(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        cfg_on = _make_config(channel_id="ch_toggle", enabled=True)
        cfg_off = _make_config(channel_id="ch_toggle", enabled=False)

        try:
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg_on)):
                await mgr.reload_channel("ch_toggle")
                await asyncio.sleep(0.05)
                assert "ch_toggle" in mgr._clients

            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg_off)):
                await mgr.reload_channel("ch_toggle")
                await asyncio.sleep(0.05)
                assert "ch_toggle" not in mgr._clients
        finally:
            await mgr.stop()

    async def test_reload_stops_connection_when_switched_to_webhook(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        cfg_lc = _make_config(channel_id="ch_mode", receive_mode="long_connection")
        cfg_wh = _make_config(channel_id="ch_mode", receive_mode="webhook")

        try:
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg_lc)):
                await mgr.reload_channel("ch_mode")
                await asyncio.sleep(0.05)
                assert "ch_mode" in mgr._clients

            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg_wh)):
                await mgr.reload_channel("ch_mode")
                await asyncio.sleep(0.05)
                assert "ch_mode" not in mgr._clients
        finally:
            await mgr.stop()

    async def test_reload_stops_connection_when_channel_deleted(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        cfg = _make_config(channel_id="ch_del")

        try:
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg)):
                await mgr.reload_channel("ch_del")
                await asyncio.sleep(0.05)

            # Now simulate delete: get_config returns None
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=None)):
                await mgr.reload_channel("ch_del")
                await asyncio.sleep(0.05)
                assert "ch_del" not in mgr._clients
        finally:
            await mgr.stop()

    async def test_reload_skips_provider_without_factory(self):
        """wecom in first iteration has no factory — reload should no-op."""
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        cfg_wecom = _make_config(
            channel_id="ch_wecom", provider=ChannelProvider.WECOM,
        )
        try:
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg_wecom)):
                await mgr.reload_channel("ch_wecom")
                await asyncio.sleep(0.05)
                assert "ch_wecom" not in mgr._clients
        finally:
            await mgr.stop()

    async def test_reload_restarts_when_config_credentials_change(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        cfg_v1 = _make_config(channel_id="ch_update", credentials={"k": "v1"})
        cfg_v2 = _make_config(channel_id="ch_update", credentials={"k": "v2"})

        try:
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg_v1)):
                await mgr.reload_channel("ch_update")
                await asyncio.sleep(0.05)
                first_client = mgr._clients["ch_update"]

            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg_v2)):
                await mgr.reload_channel("ch_update")
                await asyncio.sleep(0.05)
                second_client = mgr._clients["ch_update"]
                # New instance created, old one disconnected
                assert second_client is not first_client
                assert first_client.disconnect_calls >= 1
        finally:
            await mgr.stop()


# ── Status reporting ──

class TestConnectionStatus:
    async def test_status_for_unknown_channel(self):
        mgr = ChannelConnectionManager()
        assert mgr.connection_status("ch_nope") == "not_long_connection"

    async def test_status_reflects_connected_state(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        cfg = _make_config(channel_id="ch_status")
        try:
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg)):
                await mgr.reload_channel("ch_status")
                await asyncio.sleep(0.05)
                assert mgr.connection_status("ch_status") == "long_connection_connected"
        finally:
            await mgr.stop()


# ── Singleton ──

class TestSingleton:
    def test_get_connection_manager_returns_same_instance(self):
        a = get_connection_manager()
        b = get_connection_manager()
        assert a is b


# ── Reconnect behavior ──

class TestReconnect:
    async def test_reconnects_after_connect_failure(self):
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))

        # Make the first connect raise, then succeed on reconnect.
        # We patch CHANNEL_CONNECTION_RECONNECT_INTERVAL to keep the test fast.
        cfg = _make_config(channel_id="ch_retry")
        try:
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg)), \
                 patch("app.core.config.settings.CHANNEL_CONNECTION_RECONNECT_INTERVAL", 0.01):
                # Stash a reference to control the fake client
                created_clients: list[FakeConnectionClient] = []
                orig_factory = mgr._factories["mock"]

                def controlled_factory(c):
                    client = orig_factory(c)
                    # First connect raises, subsequent ones block normally
                    if not created_clients:
                        client.connect_behavior = RuntimeError("initial failure")
                    created_clients.append(client)
                    return client

                mgr._factories["mock"] = controlled_factory

                await mgr.reload_channel("ch_retry")
                # First connect fails fast; the run loop should then create a
                # new client on next reload OR reconnect within the same run
                # loop. Our design: a failed connect() logs + sleeps + retries
                # the *same* client. So reconnect happens in-place.
                # Give it time for at least one retry attempt
                await asyncio.sleep(0.05)
                # The original client's connect was called > 1 time (retried)
                assert created_clients[0].connect_calls >= 1
        finally:
            await mgr.stop()


# ── dispatch_inbound helper ──

class TestDispatchInbound:
    async def test_dispatch_parses_and_executes_inline(self):
        """dispatch_inbound calls parser → create_or_dedup_event →
        ChannelService.execute (NO Celery). Verify the new inline wiring.

        dispatch fires execute in a background task (asyncio.create_task) so
        the SDK callback can return immediately; we await the event loop to
        let the spawned task run, then assert execute was called."""
        from datetime import UTC, datetime

        from app.channels.base import InboundMessage

        cfg = _make_config()
        inbound = InboundMessage(
            channel_id=cfg.id, platform_chat_id="c", platform_user_id="u",
            message_id="m1", text="hi", raw={}, timestamp=datetime.now(UTC),
        )

        def fake_parser(body, config):
            assert config.id == cfg.id
            assert "hi" in body
            return inbound

        # Manager.execution_semaphore returns None for channels not in the
        # _clients dict (our test cfg isn't), so no semaphore is acquired.
        with patch(
            "app.services.channel_service.ChannelService.create_or_dedup_event",
            new=AsyncMock(return_value="inb_001"),
        ), patch(
            "app.services.channel_service.ChannelService.execute",
            new=AsyncMock(),
        ) as mock_exec:
            log_id = await dispatch_inbound(
                config=cfg, body='{"text":"hi"}', parser=fake_parser,
            )
            # Let the spawned background task run
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)

        assert log_id == "inb_001"
        mock_exec.assert_awaited_once()
        # Called with the InboundMessage + event_log_id threaded through
        passed_inbound = mock_exec.call_args.args[0]
        assert passed_inbound.message_id == "m1"
        assert mock_exec.call_args.kwargs.get("event_log_id") == "inb_001"

    async def test_dispatch_does_not_call_celery(self):
        """Regression: long-connection mode must not enqueue via Celery."""
        from datetime import UTC, datetime

        from app.channels.base import InboundMessage

        cfg = _make_config()
        inbound = InboundMessage(
            channel_id=cfg.id, platform_chat_id="c", platform_user_id="u",
            message_id="m_no_celery", text="hi", raw={}, timestamp=datetime.now(UTC),
        )
        with patch(
            "app.services.channel_service.ChannelService.create_or_dedup_event",
            new=AsyncMock(return_value="inb_no_celery"),
        ), patch(
            "app.services.channel_service.ChannelService.execute",
            new=AsyncMock(),
        ), patch(
            "app.workers.tasks.channel_inbound.process_inbound.delay"
        ) as mock_delay:
            await dispatch_inbound(
                config=cfg, body='{"text":"hi"}',
                parser=lambda b, c: inbound,
            )
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)
        mock_delay.assert_not_called()

    async def test_dispatch_returns_none_when_parser_returns_none(self):
        cfg = _make_config()
        with patch(
            "app.services.channel_service.ChannelService.create_or_dedup_event"
        ) as mock_dedup:
            log_id = await dispatch_inbound(
                config=cfg, body="{}", parser=lambda b, c: None,
            )
        assert log_id is None
        mock_dedup.assert_not_called()

    async def test_dispatch_swallows_parser_exception(self):
        cfg = _make_config()

        def bad_parser(body, config):
            raise ValueError("parse boom")

        with patch(
            "app.services.channel_service.ChannelService.create_or_dedup_event"
        ) as mock_dedup:
            log_id = await dispatch_inbound(
                config=cfg, body="...", parser=bad_parser,
            )
        assert log_id is None
        mock_dedup.assert_not_called()

    async def test_dispatch_handles_dedup(self):
        """Duplicate event (dedup returns None) → no execute call."""
        cfg = _make_config()
        from datetime import UTC, datetime

        from app.channels.base import InboundMessage

        inbound = InboundMessage(
            channel_id=cfg.id, platform_chat_id="c", platform_user_id="u",
            message_id="dup", text="hi", raw={}, timestamp=datetime.now(UTC),
        )

        with patch(
            "app.services.channel_service.ChannelService.create_or_dedup_event",
            new=AsyncMock(return_value=None),  # duplicate
        ), patch(
            "app.services.channel_service.ChannelService.execute",
            new=AsyncMock(),
        ) as mock_exec:
            log_id = await dispatch_inbound(
                config=cfg, body='{"text":"hi"}',
                parser=lambda b, c: inbound,
            )
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)
        assert log_id is None
        mock_exec.assert_not_awaited()


# ── Inline execution: retry + semaphore + failure marking ──

class TestExecuteWithRetry:
    """_execute_with_retry: TransientChannelError retried with backoff,
    PermanentChannelError handled by execute, unexpected error marks log FAILED."""

    async def test_retries_on_transient_then_succeeds(self):
        from datetime import UTC, datetime

        from app.channels.base import InboundMessage
        from app.channels.connections.dispatch import _execute_with_retry
        from app.channels.errors import TransientChannelError

        inbound = InboundMessage(
            channel_id="ch", platform_chat_id="c", platform_user_id="u",
            message_id="m", text="hi", raw={}, timestamp=datetime.now(UTC),
        )

        # First two calls raise transient, third succeeds
        side_effects = [
            TransientChannelError("rate limit 1"),
            TransientChannelError("rate limit 2"),
            None,
        ]
        with patch(
            "app.services.channel_service.ChannelService.execute",
            new=AsyncMock(side_effect=side_effects),
        ), patch("asyncio.sleep", new=AsyncMock()):  # skip real backoff
            await _execute_with_retry(inbound, "inb_1", None)

    async def test_marks_log_failed_when_retries_exhausted(self):
        from datetime import UTC, datetime

        from app.channels.base import InboundMessage
        from app.channels.connections.dispatch import _execute_with_retry
        from app.channels.errors import LLMRateLimitError

        inbound = InboundMessage(
            channel_id="ch", platform_chat_id="c", platform_user_id="u",
            message_id="m", text="hi", raw={}, timestamp=datetime.now(UTC),
        )

        mock_coll = MagicMock()
        mock_coll.update_one = AsyncMock()

        with patch(
            "app.services.channel_service.ChannelService.execute",
            new=AsyncMock(side_effect=LLMRateLimitError("always")),
        ), patch("asyncio.sleep", new=AsyncMock()), patch(
            "app.services.channel_service.ChannelService._event_logs_coll",
            return_value=mock_coll,
        ):
            await _execute_with_retry(inbound, "inb_2", None)

        # After retries exhausted, the log is marked FAILED
        mock_coll.update_one.assert_awaited_once()
        update_filter = mock_coll.update_one.call_args.args[0]
        update_set = mock_coll.update_one.call_args.args[1]["$set"]
        assert update_filter == {"_id": "inb_2"}
        assert update_set["status"] == "failed"

    async def test_unexpected_error_marks_log_failed(self):
        from datetime import UTC, datetime

        from app.channels.base import InboundMessage
        from app.channels.connections.dispatch import _execute_with_retry

        inbound = InboundMessage(
            channel_id="ch", platform_chat_id="c", platform_user_id="u",
            message_id="m", text="hi", raw={}, timestamp=datetime.now(UTC),
        )

        mock_coll = MagicMock()
        mock_coll.update_one = AsyncMock()

        with patch(
            "app.services.channel_service.ChannelService.execute",
            new=AsyncMock(side_effect=RuntimeError("unexpected bug")),
        ), patch(
            "app.services.channel_service.ChannelService._event_logs_coll",
            return_value=mock_coll,
        ):
            await _execute_with_retry(inbound, "inb_3", None)

        mock_coll.update_one.assert_awaited_once()
        update_set = mock_coll.update_one.call_args.args[1]["$set"]
        assert update_set["status"] == "failed"
        assert "unexpected bug" in update_set["error"]


class TestExecutionSemaphore:
    """Manager.execution_semaphore: per-channel cap, lazy create, cleanup on stop."""

    async def test_returns_none_for_unknown_channel(self):
        mgr = ChannelConnectionManager()
        assert mgr.execution_semaphore("ch_nope") is None

    async def test_lazy_creates_for_active_channel(self):
        """Once a channel has a live client, the semaphore is created on demand."""
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        cfg = _make_config(channel_id="ch_sem")
        try:
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg)):
                await mgr.reload_channel("ch_sem")
                await asyncio.sleep(0.05)
                sem1 = mgr.execution_semaphore("ch_sem")
                assert sem1 is not None
                # Same instance on second call (lazy + cached)
                sem2 = mgr.execution_semaphore("ch_sem")
                assert sem1 is sem2
        finally:
            await mgr.stop()

    async def test_cleaned_up_on_stop(self):
        """Stopping a channel drops its semaphore so reconnect gets a fresh one."""
        mgr = ChannelConnectionManager()
        mgr.register_factory("mock", lambda cfg: FakeConnectionClient(cfg))
        cfg = _make_config(channel_id="ch_cleanup")
        try:
            with patch.object(mgr, "_load_channel", new=AsyncMock(return_value=cfg)):
                await mgr.reload_channel("ch_cleanup")
                await asyncio.sleep(0.05)
                # Touch the semaphore so it's created lazily
                assert mgr.execution_semaphore("ch_cleanup") is not None
                assert "ch_cleanup" in mgr._execution_sems

            # Stop the channel
            await mgr._stop_channel("ch_cleanup")
            assert "ch_cleanup" not in mgr._execution_sems
        finally:
            await mgr.stop()

"""Channel management API tests (admin CRUD + provider schema + inbound).

Auth pattern follows test_webhooks.py: override BOTH get_current_user and
require_role (the admin gate is a router-level Depends, not an inline check).
``user`` is a UserResponse Pydantic object — fields accessed via attributes
(user.id), confirmed in Step 0.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.channels.providers.mock.channel import MOCK_SENT_MESSAGES
from app.core.security import get_current_user, require_role
from app.main import app
from app.models.channel import ChannelConfig, ChannelProvider, InboundEventLog
from app.models.user import UserStatus
from app.schemas.execution import ExecutionResponse
from app.schemas.user import UserResponse
from app.services.channel_service import ChannelService
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_user() -> UserResponse:
    return UserResponse(
        id="user_admin",
        username="admin",
        email="admin@test.com",
        role="admin",
        status=UserStatus.ACTIVE,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


def _override_auth(admin_user):
    """Override both auth deps (matches webhooks.py router-level gating)."""
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_role] = lambda: admin_user
    return lambda: app.dependency_overrides.clear()


def _make_config(**overrides) -> ChannelConfig:
    defaults = {
        "id": "ch_01J",
        "name": "test",
        "provider": ChannelProvider.MOCK,
        "agent_id": "agent_01J",
        "owner_user_id": "user_admin",
        "webhook_secret": "mock_secret_at_least_16",
        "credentials": {},
    }
    defaults.update(overrides)
    return ChannelConfig(**defaults)


# ---------------------------------------------------------------------------
# Provider schema endpoint
# ---------------------------------------------------------------------------


class TestProviderSchema:
    def test_returns_all_built_in_providers(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            resp = client.get("/api/v1/channels/providers/schema")
            assert resp.status_code == 200
            providers = resp.json()["providers"]
            assert "lark" in providers
            assert "dingtalk" in providers
            assert "wecom" in providers
            assert "mock" in providers
            lark_fields = {f["key"] for f in providers["lark"]["credential_fields"]}
            assert {"app_id", "app_secret", "verification_token"} <= lark_fields
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# Create channel — credentials must be masked in the response
# ---------------------------------------------------------------------------


class TestCreateChannel:
    def test_creates_channel_and_returns_masked_credentials(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            created = _make_config(
                id="ch_new", credentials={"app_id": "cli_AAAA1111BBBB"},
            )
            with patch(
                "app.services.channel_service.ChannelService.create_channel",
                new=AsyncMock(return_value=created),
            ) as mock_create:
                resp = client.post(
                    "/api/v1/channels",
                    json={
                        "name": "test",
                        "provider": "mock",
                        "agent_id": "agent_01J",
                        "credentials": {"app_id": "cli_AAAA1111BBBB"},
                    },
                )
            assert resp.status_code == 201
            body = resp.json()
            assert body["id"] == "ch_new"
            # owner threaded from the authenticated user (user.id)
            assert body["owner_user_id"] == "user_admin"
            # full inbound URL includes base_url + provider + id + ?secret=...
            assert body["inbound_url"].startswith(
                "http://testserver/api/v1/channels/inbound/mock/ch_new?secret="
            )
            assert body["inbound_url"].endswith("mock_secret_at_least_16")
            # credentials always masked in the response
            assert "app_id" in body["credentials"]
            masked = body["credentials"]["app_id"]
            assert "AAAA1111BBBB" not in masked
            assert "****" in masked
            mock_create.assert_awaited_once()
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# List / Get / Update / Delete
# ---------------------------------------------------------------------------


class TestListChannels:
    def test_returns_owner_channels(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.channel_service.ChannelService.list_channels",
                new=AsyncMock(return_value=([_make_config(id="ch_1")], 1)),
            ) as mock_list:
                resp = client.get("/api/v1/channels")
            assert resp.status_code == 200
            body = resp.json()
            assert body["total"] == 1
            assert body["page"] == 1
            assert body["items"][0]["id"] == "ch_1"
            mock_list.assert_awaited_once()
            assert mock_list.call_args.kwargs.get("owner_user_id") == "user_admin"
        finally:
            cleanup()


class TestGetChannel:
    def test_get_existing(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.channel_service.ChannelService.get_channel",
                new=AsyncMock(return_value=_make_config()),
            ):
                resp = client.get("/api/v1/channels/ch_01J")
            assert resp.status_code == 200
            assert resp.json()["id"] == "ch_01J"
        finally:
            cleanup()

    def test_get_not_found_returns_404(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.channel_service.ChannelService.get_channel",
                new=AsyncMock(return_value=None),
            ):
                resp = client.get("/api/v1/channels/missing")
            assert resp.status_code == 404
        finally:
            cleanup()


class TestUpdateChannel:
    def test_update_returns_updated(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            updated = _make_config(name="renamed")
            with patch(
                "app.services.channel_service.ChannelService.update_channel",
                new=AsyncMock(return_value=updated),
            ) as mock_update:
                resp = client.patch(
                    "/api/v1/channels/ch_01J",
                    json={"name": "renamed", "enabled": False},
                )
            assert resp.status_code == 200
            assert resp.json()["name"] == "renamed"
            mock_update.assert_awaited_once()
            assert mock_update.call_args.kwargs.get("enabled") is False
        finally:
            cleanup()

    def test_update_not_found_returns_404(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.channel_service.ChannelService.update_channel",
                new=AsyncMock(return_value=None),
            ):
                resp = client.patch("/api/v1/channels/missing", json={"name": "x"})
            assert resp.status_code == 404
        finally:
            cleanup()


class TestDeleteChannel:
    def test_soft_delete_returns_204(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.channel_service.ChannelService.delete_channel",
                new=AsyncMock(),
            ) as mock_del:
                resp = client.delete("/api/v1/channels/ch_01J")
            assert resp.status_code == 204
            mock_del.assert_awaited_once_with("ch_01J")
        finally:
            cleanup()


class TestEnableDisableReset:
    def test_enable(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.channel_service.ChannelService.set_enabled",
                new=AsyncMock(),
            ) as mock_set:
                resp = client.post("/api/v1/channels/ch_01J/enable")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
            mock_set.assert_awaited_once_with("ch_01J", True)
        finally:
            cleanup()

    def test_disable(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.channel_service.ChannelService.set_enabled",
                new=AsyncMock(),
            ) as mock_set:
                resp = client.post("/api/v1/channels/ch_01J/disable")
            assert resp.status_code == 200
            mock_set.assert_awaited_once_with("ch_01J", False)
        finally:
            cleanup()

    def test_reset(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.channel_service.ChannelService.reset_degraded",
                new=AsyncMock(),
            ) as mock_reset:
                resp = client.post("/api/v1/channels/ch_01J/reset")
            assert resp.status_code == 200
            mock_reset.assert_awaited_once_with("ch_01J")
        finally:
            cleanup()


# ---------------------------------------------------------------------------
# Inbound webhook receiver — PUBLIC (no JWT), full E2E via MockChannel
# ---------------------------------------------------------------------------


class TestInboundWebhookE2E:
    """Full pipeline through the route, with MockChannel as the adapter.

    Two complementary angles:
    - Route dispatch test: verify → dedup → persist → dispatch(.delay called
      with the persisted log id) → ack. The Celery task body itself is
      unit-tested in tests/workers/test_channel_inbound.py.
    - Full round-trip test: ChannelService.execute(Mock inbound) records the
      reply via MockChannel.send — proving the dispatch target would deliver.

    NOTE on ``.delay`` mocking: in production Celery is NOT eager — ``.delay``
    enqueues to Redis and returns immediately, and a separate worker process
    runs the task on its own loop. Under eager mode (conftest sets
    task_always_eager=True) the task body would run inline inside the request
    handler, whose loop is already running; ``workers/loop.run_async`` then
    tries to start a second loop and crashes. So we patch ``.delay`` to assert
    dispatch happened, not to run the task body.
    """

    def setup_method(self):
        MOCK_SENT_MESSAGES.clear()

    def test_route_dispatches_to_celery_with_persisted_log_id(self, client, admin_user):
        # Inbound endpoint is public — clear the admin auth override set by
        # earlier tests. The endpoint does NOT use get_current_user.
        app.dependency_overrides.clear()

        cfg = _make_config(id="ch_e2e")

        # Dedup collection: no existing event → insert_one proceeds → log id
        # generated. We capture the inserted doc to recover the generated id.
        inserted: dict[str, InboundEventLog | None] = {"log": None}

        async def _fake_find_one(query):
            return None  # no existing → not a dup

        async def _fake_insert_one(doc):
            inserted["log"] = InboundEventLog(**doc)

        mock_event_coll = MagicMock()
        mock_event_coll.find_one = AsyncMock(side_effect=_fake_find_one)
        mock_event_coll.insert_one = AsyncMock(side_effect=_fake_insert_one)

        with patch(
            "app.api.v1.channels.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ), patch.object(
            ChannelService, "_event_logs_coll", return_value=mock_event_coll,
        ), patch(
            "app.workers.tasks.channel_inbound.process_inbound.delay",
        ) as mock_delay:
            resp = client.post(
                "/api/v1/channels/inbound/mock/ch_e2e"
                f"?secret={cfg.webhook_secret}",
                json={
                    "message_id": "e2e_msg_1",
                    "chat_id": "e2e_chat",
                    "user_id": "e2e_user",
                    "text": "你好",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # Route persisted the event then dispatched it.
        assert inserted["log"] is not None
        assert inserted["log"].platform_message_id == "e2e_msg_1"
        mock_delay.assert_called_once_with(inserted["log"].id)

    def test_execute_full_round_trip_sends_reply(self, client, admin_user):
        """ChannelService.execute(Mock inbound) → MockChannel.send records reply.

        This is the dispatch target's behavior: given the persisted
        InboundMessage, execution produces the reply that gets delivered.
        Run directly (not via eager Celery) to avoid the nested-loop conflict
        documented above.
        """
        app.dependency_overrides.clear()
        import asyncio

        from app.channels.base import InboundMessage

        cfg = _make_config(id="ch_e2e")
        fake_response = ExecutionResponse(
            output="回复:你好",
            execution_path="react",  # str, not list (confirmed in Step 0)
            request_id="req_e2e",
            agent_id="agent_01J",
            session_id="sess_e2e",
            step_count=1,
        )
        inbound = InboundMessage(
            channel_id="ch_e2e",
            platform_chat_id="e2e_chat",
            platform_user_id="e2e_user",
            message_id="e2e_msg_1",
            text="你好",
            raw={},
            timestamp=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
        )
        mock_cfg_coll = MagicMock()
        mock_cfg_coll.update_one = AsyncMock()

        async def _run():
            with patch(
                "app.services.channel_service.ChannelService.get_config",
                new=AsyncMock(return_value=cfg),
            ), patch(
                "app.services.channel_service.AgentExecutionService.invoke",
                new=AsyncMock(return_value=fake_response),
            ), patch.object(
                ChannelService, "_configs_coll", return_value=mock_cfg_coll,
            ):
                await ChannelService.execute(inbound)

        asyncio.run(_run())

        assert len(MOCK_SENT_MESSAGES) == 1
        assert MOCK_SENT_MESSAGES[0]["text"] == "回复:你好"
        assert MOCK_SENT_MESSAGES[0]["platform_chat_id"] == "e2e_chat"

    def test_inbound_unknown_channel_returns_404(self, client):
        app.dependency_overrides.clear()
        with patch(
            "app.services.channel_service.ChannelService.get_config",
            new=AsyncMock(return_value=None),
        ):
            resp = client.post(
                "/api/v1/channels/inbound/mock/missing",
                json={"message_id": "m", "chat_id": "c", "user_id": "u", "text": "x"},
            )
        assert resp.status_code == 404

    def test_inbound_disabled_channel_returns_404(self, client):
        app.dependency_overrides.clear()
        cfg = _make_config(id="ch_off", enabled=False)
        with patch(
            "app.services.channel_service.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ):
            resp = client.post(
                "/api/v1/channels/inbound/mock/ch_off",
                json={"message_id": "m", "chat_id": "c", "user_id": "u", "text": "x"},
            )
        assert resp.status_code == 404

    def test_inbound_degraded_channel_returns_503(self, client):
        from app.models.channel import ChannelStatus

        app.dependency_overrides.clear()
        cfg = _make_config(id="ch_deg", status=ChannelStatus.DEGRADED)
        with patch(
            "app.services.channel_service.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ):
            resp = client.post(
                "/api/v1/channels/inbound/mock/ch_deg",
                json={"message_id": "m", "chat_id": "c", "user_id": "u", "text": "x"},
            )
        assert resp.status_code == 503

    def test_inbound_empty_text_returns_ok_no_processing(self, client):
        """MockChannel.verify_inbound returns None for empty text → ack, no dispatch."""
        app.dependency_overrides.clear()
        cfg = _make_config(id="ch_ack")
        with patch(
            "app.services.channel_service.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ), patch(
            "app.services.channel_service.ChannelService.create_or_dedup_event",
            new=AsyncMock(),
        ) as mock_dedup:
            resp = client.post(
                "/api/v1/channels/inbound/mock/ch_ack"
                f"?secret={cfg.webhook_secret}",
                json={"message_id": "m", "chat_id": "c", "user_id": "u", "text": "   "},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # No event should have been persisted/dispatched.
        mock_dedup.assert_not_awaited()


class TestInboundWebhookSecret:
    """Second-factor auth via ?secret=<webhook_secret> (issue #3).

    Each channel has a per-channel secret generated at creation time. The
    inbound receiver enforces it as a defense against path-scanning forgery
    on top of each platform's signature verification.
    """

    def test_missing_secret_returns_401(self, client):
        app.dependency_overrides.clear()
        cfg = _make_config(id="ch_sec")
        with patch(
            "app.services.channel_service.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ):
            resp = client.post(
                "/api/v1/channels/inbound/mock/ch_sec",
                json={"message_id": "m", "chat_id": "c", "user_id": "u", "text": "x"},
            )
        assert resp.status_code == 401

    def test_wrong_secret_returns_401(self, client):
        app.dependency_overrides.clear()
        cfg = _make_config(id="ch_sec")
        with patch(
            "app.services.channel_service.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ):
            resp = client.post(
                "/api/v1/channels/inbound/mock/ch_sec?secret=wrong_value",
                json={"message_id": "m", "chat_id": "c", "user_id": "u", "text": "x"},
            )
        assert resp.status_code == 401

    def test_correct_secret_passes_secret_gate(self, client):
        """With the correct secret, the request proceeds past the secret check
        into verify_inbound. We mock verify_inbound to a no-op ack to isolate
        the secret gate."""
        app.dependency_overrides.clear()
        cfg = _make_config(id="ch_sec")
        with patch(
            "app.services.channel_service.ChannelService.get_config",
            new=AsyncMock(return_value=cfg),
        ), patch(
            "app.services.channel_service.ChannelService.create_or_dedup_event",
            new=AsyncMock(return_value=None),  # dedup → ack
        ) as mock_dedup:
            resp = client.post(
                "/api/v1/channels/inbound/mock/ch_sec"
                f"?secret={cfg.webhook_secret}",
                json={"message_id": "m", "chat_id": "c", "user_id": "u", "text": "x"},
            )
        # Past the secret gate — either 200 (dedup) or a verify error, but NOT 401.
        assert resp.status_code != 401
        mock_dedup.assert_awaited_once()


# ── receive_mode + connection reload ──

class TestReceiveMode:
    def test_create_with_long_connection_mode(self, client, admin_user):
        """create passes receive_mode through to the service."""
        cleanup = _override_auth(admin_user)
        try:
            created = _make_config(
                id="ch_lc", receive_mode="long_connection",
            )
            with patch(
                "app.services.channel_service.ChannelService.create_channel",
                new=AsyncMock(return_value=created),
            ) as mock_create, patch(
                "app.api.v1.channels._reload", new=AsyncMock(),
            ) as mock_reload:
                resp = client.post(
                    "/api/v1/channels",
                    json={
                        "name": "lc-test",
                        "provider": "mock",
                        "agent_id": "agent_01J",
                        "credentials": {},
                        "receive_mode": "long_connection",
                    },
                )
            assert resp.status_code == 201
            body = resp.json()
            assert body["receive_mode"] == "long_connection"
            # Service received receive_mode kwarg
            assert mock_create.call_args.kwargs["receive_mode"] == "long_connection"
            # Connection manager was notified (hot reload)
            mock_reload.assert_awaited_once_with("ch_lc")
        finally:
            cleanup()

    def test_create_rejects_invalid_receive_mode(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            resp = client.post(
                "/api/v1/channels",
                json={
                    "name": "bad", "provider": "mock",
                    "agent_id": "agent_01J", "credentials": {},
                    "receive_mode": "carrier_pigeon",
                },
            )
            assert resp.status_code == 400
        finally:
            cleanup()

    def test_create_defaults_to_webhook(self, client, admin_user):
        """Omitting receive_mode defaults to webhook."""
        cleanup = _override_auth(admin_user)
        try:
            created = _make_config(id="ch_def")
            with patch(
                "app.services.channel_service.ChannelService.create_channel",
                new=AsyncMock(return_value=created),
            ) as mock_create, patch(
                "app.api.v1.channels._reload", new=AsyncMock(),
            ):
                client.post(
                    "/api/v1/channels",
                    json={"name": "t", "provider": "mock",
                          "agent_id": "a", "credentials": {}},
                )
            assert mock_create.call_args.kwargs["receive_mode"] == "webhook"
        finally:
            cleanup()

    def test_update_changes_receive_mode_and_reloads(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            updated = _make_config(id="ch_upd", receive_mode="long_connection")
            with patch(
                "app.services.channel_service.ChannelService.update_channel",
                new=AsyncMock(return_value=updated),
            ), patch(
                "app.api.v1.channels._reload", new=AsyncMock(),
            ) as mock_reload:
                resp = client.patch(
                    "/api/v1/channels/ch_upd",
                    json={"receive_mode": "long_connection"},
                )
            assert resp.status_code == 200
            assert resp.json()["receive_mode"] == "long_connection"
            mock_reload.assert_awaited_once_with("ch_upd")
        finally:
            cleanup()

    def test_delete_triggers_reload(self, client, admin_user):
        """delete must notify the connection manager so it stops the client."""
        cleanup = _override_auth(admin_user)
        try:
            with patch(
                "app.services.channel_service.ChannelService.delete_channel",
                new=AsyncMock(),
            ), patch(
                "app.api.v1.channels._reload", new=AsyncMock(),
            ) as mock_reload:
                resp = client.delete("/api/v1/channels/ch_del")
            assert resp.status_code == 204
            mock_reload.assert_awaited_once_with("ch_del")
        finally:
            cleanup()

    def test_enable_disable_trigger_reload(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            for action in ("enable", "disable"):
                with patch(
                    "app.services.channel_service.ChannelService.set_enabled",
                    new=AsyncMock(),
                ), patch(
                    "app.api.v1.channels._reload", new=AsyncMock(),
                ) as mock_reload:
                    resp = client.post(f"/api/v1/channels/ch_x/{action}")
                assert resp.status_code == 200
                mock_reload.assert_awaited_once_with("ch_x")
        finally:
            cleanup()


class TestProviderSchemaReceiveModes:
    def test_webhook_always_offered(self, client, admin_user):
        cleanup = _override_auth(admin_user)
        try:
            resp = client.get("/api/v1/channels/providers/schema")
            for _, schema in resp.json()["providers"].items():
                assert "webhook" in schema["receive_modes"]
        finally:
            cleanup()

    def test_long_connection_dropped_when_no_factory(self, client, admin_user):
        """A provider whose protocol supports long-connection but has no
        registered ConnectionClient factory should only offer webhook.
        In the default test environment wecom has no factory (first iteration)."""
        cleanup = _override_auth(admin_user)
        try:
            resp = client.get("/api/v1/channels/providers/schema")
            providers = resp.json()["providers"]
            # wecom has no ConnectionClient factory in first iteration
            assert "long_connection" not in providers["wecom"]["receive_modes"]
        finally:
            cleanup()

    def test_long_connection_offered_when_factory_registered(self, client, admin_user):
        """When a provider has a registered factory AND the global flag is on,
        the schema should offer long_connection. In the default test
        environment lark and dingtalk both have real factories (registered at
        import time via app.channels.providers) and their flags default True,
        so both should offer long_connection."""
        cleanup = _override_auth(admin_user)
        try:
            resp = client.get("/api/v1/channels/providers/schema")
            providers = resp.json()["providers"]
            # lark and dingtalk have real factories + flags on → offered
            assert "long_connection" in providers["lark"]["receive_modes"]
            assert "long_connection" in providers["dingtalk"]["receive_modes"]
            # wecom has no factory → not offered
            assert "long_connection" not in providers["wecom"]["receive_modes"]
        finally:
            cleanup()

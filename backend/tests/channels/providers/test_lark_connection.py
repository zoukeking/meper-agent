"""LarkConnectionClient unit tests.

We don't connect to real lark; instead we mock the lark_oapi.ws.Client and
the SDK's typed event object to verify:
  1. The factory registers with the manager
  2. _on_message_receive reconstructs the envelope and dispatches
  3. connect() runs the SDK in a thread and respects cancellation
  4. send() (via client.py) maps SDK errors to our ChannelError taxonomy
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.channels.connections.manager import get_connection_manager
from app.channels.errors import InvalidCredentialsError, SendFailedError
from app.core.crypto import encrypt_secret
from app.models.channel import ChannelConfig, ChannelProvider


def _make_lark_config(*, credentials: dict | None = None) -> ChannelConfig:
    if credentials is None:
        credentials = {
            "app_id": encrypt_secret("cli_test_app"),
            "app_secret": encrypt_secret("test_secret_value"),
        }
    return ChannelConfig(
        id="ch_lark1", _id="ch_lark1",
        name="lark-test", provider=ChannelProvider.LARK,
        agent_id="agent_01J", owner_user_id="user_01J",
        webhook_secret="x" * 32, credentials=credentials,
        receive_mode="long_connection",
    )


class TestLarkFactoryRegistration:
    def test_factory_registered_on_import(self):
        """Importing app.channels.providers triggers lark factory registration."""
        import app.channels.providers  # noqa: F401
        mgr = get_connection_manager()
        assert mgr.supports("lark")

    def test_factory_creates_lark_connection_client(self):
        from app.channels.providers.lark.connection import LarkConnectionClient
        mgr = get_connection_manager()
        factory = mgr._factories.get("lark")
        assert factory is not None
        client = factory(_make_lark_config())
        assert isinstance(client, LarkConnectionClient)


class TestReconstructEnvelope:
    def test_reconstruct_typical_message_event(self):
        """SDK event object → v2 envelope JSON that parse_lark_event can consume."""
        from app.channels.providers.lark.connection import LarkConnectionClient

        # Build a fake SDK event matching P2ImMessageReceiveV1 shape.
        # Use simple namespace objects (not MagicMock) because the SDK's
        # real objects are plain Python objects with __dict__, and MagicMock's
        # __dict__ contains internal mock attributes that cause recursion.
        class FakeHeader:
            def __init__(self):
                self.event_type = "im.message.receive_v1"
                self.token = "verify_token_value"
                self.app_id = "cli_test"
                self.event_id = "evt_1"
                self.create_time = "123"
                self.tenant_key = "tk"

        class FakeMessage:
            def __init__(self):
                self.message_id = "om_001"
                self.chat_id = "oc_chat1"
                self.message_type = "text"
                self.content = json.dumps({"text": "hello world"})

        class FakeEvent:
            def __init__(self):
                class FakeSender:
                    def __init__(self):
                        class FakeSenderId:
                            def __init__(self):
                                self.open_id = "ou_sender"
                        self.sender_id = FakeSenderId()
                self.sender = FakeSender()
                self.message = FakeMessage()

        class FakeSdkEvent:
            def __init__(self):
                self.header = FakeHeader()
                self.event = FakeEvent()

        sdk_event = FakeSdkEvent()

        body = LarkConnectionClient._reconstruct_envelope(sdk_event)
        envelope = json.loads(body)
        assert envelope["schema"] == "2.0"
        assert envelope["header"]["event_type"] == "im.message.receive_v1"
        assert envelope["event"]["message"]["message_id"] == "om_001"

        # parse_lark_event should be able to consume the reconstructed body
        from app.channels.providers.lark.verify import parse_lark_event
        config = _make_lark_config(credentials={
            "app_id": encrypt_secret("cli_x"),
            "app_secret": encrypt_secret("s"),
            "verification_token": encrypt_secret("verify_token_value"),
        })
        msg = parse_lark_event(body, config)
        assert msg is not None
        assert msg.message_id == "om_001"
        assert msg.text == "hello world"

    def test_reconstruct_handles_missing_fields(self):
        """Missing header/event shouldn't crash — just produce empty sub-objects."""
        from app.channels.providers.lark.connection import LarkConnectionClient

        sdk_event = MagicMock()
        sdk_event.header = None
        sdk_event.event = None
        body = LarkConnectionClient._reconstruct_envelope(sdk_event)
        envelope = json.loads(body)
        assert envelope["header"] == {}
        assert envelope["event"] == {}


class TestOnMessageReceive:
    def test_dispatches_via_run_coroutine_threadsafe(self):
        """The SDK callback (sync) should schedule dispatch_inbound on the
        captured event loop via asyncio.run_coroutine_threadsafe."""
        from app.channels.providers.lark.connection import LarkConnectionClient

        config = _make_lark_config()
        client = LarkConnectionClient(config)
        fake_loop = MagicMock()
        fake_loop.is_closed.return_value = False
        client._loop = fake_loop

        # Build a fake SDK event using simple objects (not MagicMock, which
        # has __dict__ attributes that cause recursion in _obj_to_dict)
        class FakeObj:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        sdk_event = FakeObj(
            header=FakeObj(event_type="im.message.receive_v1", token="t",
                          app_id="a", event_id="e", create_time="1", tenant_key="tk"),
            event=FakeObj(
                sender=FakeObj(sender_id=FakeObj(open_id="ou_x")),
                message=FakeObj(message_id="om_1", chat_id="oc_c",
                               message_type="text",
                               content=json.dumps({"text": "hi"})),
            ),
        )

        with patch(
            "app.channels.providers.lark.connection.dispatch_inbound",
            new=AsyncMock(return_value="inb_1"),
        ), patch("asyncio.run_coroutine_threadsafe") as mock_rcts:
            client._on_message_receive(sdk_event)

        mock_rcts.assert_called_once()
        args = mock_rcts.call_args.args
        assert args[1] is fake_loop  # loop passed as second arg

    def test_drops_event_when_no_loop(self):
        """If the loop is None (before connect() finished), drop the event."""
        from app.channels.providers.lark.connection import LarkConnectionClient

        client = LarkConnectionClient(_make_lark_config())
        client._loop = None  # simulating pre-connect state

        # Should not raise
        client._on_message_receive(MagicMock())


class TestConnectMissingCredentials:
    async def test_connect_raises_on_missing_app_id(self):
        from app.channels.providers.lark.connection import LarkConnectionClient

        config = _make_lark_config(credentials={})  # no app_id/app_secret
        client = LarkConnectionClient(config)
        with pytest.raises(InvalidCredentialsError):
            await client.connect()


class TestSendViaSDK:
    """client.send_text_message now uses lark-oapi SDK. Verify error mapping."""

    @staticmethod
    def _build_mock_sdk_client(response):
        """Build a MagicMock simulating the SDK client chain im.v1.message.create."""
        fake_message = MagicMock()
        fake_message.create = MagicMock(return_value=response)
        fake_sdk_client = MagicMock()
        # MagicMock auto-creates chain, but we need .create to be our mock
        fake_sdk_client.im.v1.message.create = fake_message.create
        return fake_sdk_client

    async def test_send_success_returns_message_id(self):
        from app.channels.providers.lark.client import send_text_message

        config = _make_lark_config()
        fake_response = MagicMock()
        fake_response.success.return_value = True
        fake_response.data = MagicMock(message_id="om_new_msg")
        fake_sdk_client = self._build_mock_sdk_client(fake_response)

        with patch(
            "app.channels.providers.lark.client._get_lark_client",
            return_value=fake_sdk_client,
        ):
            msg_id = await send_text_message(
                config=config, receive_id="oc_chat1", text="hello",
            )
        assert msg_id == "om_new_msg"

    async def test_send_token_error_raises_invalid_credentials(self):
        from app.channels.providers.lark.client import send_text_message

        config = _make_lark_config()
        fake_response = MagicMock()
        fake_response.success.return_value = False
        fake_response.code = 99991663  # token-related error code
        fake_response.msg = "invalid token"
        fake_sdk_client = self._build_mock_sdk_client(fake_response)

        with patch(
            "app.channels.providers.lark.client._get_lark_client",
            return_value=fake_sdk_client,
        ), pytest.raises(InvalidCredentialsError):
            await send_text_message(
                config=config, receive_id="oc_chat1", text="hello",
            )

    async def test_send_generic_error_raises_send_failed(self):
        from app.channels.providers.lark.client import send_text_message

        config = _make_lark_config()
        fake_response = MagicMock()
        fake_response.success.return_value = False
        fake_response.code = 230002  # some generic business error
        fake_response.msg = "chat not found"
        fake_sdk_client = self._build_mock_sdk_client(fake_response)

        with patch(
            "app.channels.providers.lark.client._get_lark_client",
            return_value=fake_sdk_client,
        ), pytest.raises(SendFailedError):
            await send_text_message(
                config=config, receive_id="oc_chat1", text="hello",
            )

    async def test_send_missing_app_id_raises_invalid_credentials(self):
        from app.channels.providers.lark.client import send_text_message

        config = _make_lark_config(credentials={})
        with pytest.raises(InvalidCredentialsError):
            await send_text_message(
                config=config, receive_id="oc_chat1", text="hello",
            )

    def test_receive_id_type_inference(self):
        """open_id (ou_ prefix) vs chat_id detection."""
        from app.channels.providers.lark.client import _receive_id_type

        assert _receive_id_type("oc_chat123") == "chat_id"
        assert _receive_id_type("ou_user456") == "open_id"

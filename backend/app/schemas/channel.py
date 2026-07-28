"""Channel API request/response schemas + provider credential schema."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.channel import ChannelProvider

# Receive-mode constants. ``receive_mode`` is a free-form str on the model so
# adding values doesn't require a migration, but the API only accepts these.
RECEIVE_MODE_WEBHOOK = "webhook"
RECEIVE_MODE_LONG_CONNECTION = "long_connection"
RECEIVE_MODES = (RECEIVE_MODE_WEBHOOK, RECEIVE_MODE_LONG_CONNECTION)


class CredentialField(BaseModel):
    key: str
    label: str
    type: str = "text"        # "text" | "secret"
    required: bool = True


class ProviderSchema(BaseModel):
    label: str
    credential_fields: list[CredentialField]
    # Modes the provider supports. Drives the frontend dropdown.
    # webhooks always works; long_connection depends on a ConnectionClient
    # being registered for this provider (and the global enable flag).
    receive_modes: list[str] = [RECEIVE_MODE_WEBHOOK]


class ChannelCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    provider: ChannelProvider
    agent_id: str
    credentials: dict = Field(default_factory=dict)
    receive_mode: str = RECEIVE_MODE_WEBHOOK


class ChannelUpdateRequest(BaseModel):
    name: str | None = None
    agent_id: str | None = None
    credentials: dict | None = None
    enabled: bool | None = None
    receive_mode: str | None = None


class ChannelResponse(BaseModel):
    id: str
    name: str
    provider: ChannelProvider
    agent_id: str
    owner_user_id: str
    enabled: bool
    status: str
    receive_mode: str
    credentials: dict   # always masked
    inbound_url: str
    # Live state of the long-connection client, if this channel is in
    # long_connection mode. One of:
    #   "long_connection_connected" / "long_connection_disconnected"
    #   "not_long_connection" (webhook mode or no live client)
    connection_status: str = "not_long_connection"
    created_at: str
    updated_at: str


class ChannelListResponse(BaseModel):
    items: list[ChannelResponse]
    total: int
    page: int
    page_size: int


class ProviderSchemaResponse(BaseModel):
    providers: dict[str, ProviderSchema]


def _provider_receive_modes(provider: ChannelProvider) -> list[str]:
    """Return the modes the platform *protocol-wise* supports.

    Long-connection availability also depends on a ConnectionClient factory
    being registered at runtime (lark/dingtalk in first iteration, wecom later)
    and the per-provider enable flag in settings. The frontend merges this
    list with runtime availability to decide what to offer.
    """
    if provider in (ChannelProvider.LARK, ChannelProvider.DINGTALK, ChannelProvider.WECOM):
        return [RECEIVE_MODE_WEBHOOK, RECEIVE_MODE_LONG_CONNECTION]
    return [RECEIVE_MODE_WEBHOOK]


PROVIDER_SCHEMAS: dict[str, ProviderSchema] = {
    ChannelProvider.LARK: ProviderSchema(
        label="飞书",
        credential_fields=[
            CredentialField(key="app_id", label="App ID"),
            CredentialField(key="app_secret", label="App Secret", type="secret"),
            # verification_token + encrypt_key only needed in webhook mode.
            # The frontend can hide them when receive_mode=long_connection,
            # but they remain optional and the backend tolerates their absence.
            CredentialField(key="verification_token", label="Verification Token", type="secret", required=False),
            CredentialField(key="encrypt_key", label="Encrypt Key", type="secret", required=False),
        ],
        receive_modes=_provider_receive_modes(ChannelProvider.LARK),
    ),
    ChannelProvider.DINGTALK: ProviderSchema(
        label="钉钉",
        credential_fields=[
            CredentialField(key="app_key", label="App Key"),
            CredentialField(key="app_secret", label="App Secret", type="secret"),
            # robot outgoing webhook URL — only needed in webhook mode for replies
            CredentialField(key="webhook_url", label="Group Robot Webhook URL (仅 webhook 模式)", type="secret", required=False),
        ],
        receive_modes=_provider_receive_modes(ChannelProvider.DINGTALK),
    ),
    ChannelProvider.WECOM: ProviderSchema(
        label="企业微信",
        credential_fields=[
            CredentialField(key="corp_id", label="Corp ID"),
            CredentialField(key="agent_id", label="Agent ID"),
            CredentialField(key="secret", label="Secret", type="secret"),
            CredentialField(key="token", label="Token (仅 webhook 模式)", type="secret", required=False),
            CredentialField(key="encoding_aes_key", label="EncodingAESKey (仅 webhook 模式)", type="secret", required=False),
        ],
        receive_modes=_provider_receive_modes(ChannelProvider.WECOM),
    ),
    ChannelProvider.MOCK: ProviderSchema(
        label="Mock (测试)",
        credential_fields=[],
        receive_modes=[RECEIVE_MODE_WEBHOOK],
    ),
}

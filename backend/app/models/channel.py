"""Channel configuration + inbound event log models.

Pattern: plain pydantic.BaseModel + generate_id(prefix) for ULID string IDs.
No Document base class, no PyObjectId — see backend/app/models/webhook.py.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic.config import ConfigDict

from app.models.base import generate_id, utc_now


class ChannelProvider(StrEnum):
    LARK = "lark"
    DINGTALK = "dingtalk"
    WECOM = "wecom"
    MOCK = "mock"


class ChannelStatus(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class InboundEventLogStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class ChannelConfig(BaseModel):
    """A channel = a set of platform credentials + agent binding.

    Supports 1 agent : N channels (each channel is an independent config).
    """
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("ch"), alias="_id")
    name: str = Field(..., min_length=1, max_length=200)
    provider: ChannelProvider

    # Encrypted at rest via core/crypto.encrypt_secret (per-field on write)
    credentials: dict = Field(default_factory=dict)

    agent_id: str
    owner_user_id: str

    receive_mode: str = "webhook"
    # "webhook": HTTP callback (requires public URL exposed to the platform)
    # "long_connection": WebSocket/Stream client (no public URL needed;
    #                     provider must support it — lark/dingtalk/wecom)

    enabled: bool = True
    webhook_secret: str = Field(..., min_length=16)  # secondary inbound verification
    status: ChannelStatus = ChannelStatus.ACTIVE
    consecutive_failures: int = 0

    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())


class InboundEventLog(BaseModel):
    """Idempotency key + work queue entry.

    Platform may resend the same event on timeout; dedupe by platform_message_id.
    InboundMessage is persisted here BEFORE acking the platform, so a crash
    between ack and worker pickup doesn't lose the message.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("inb"), alias="_id")
    channel_id: str
    platform_message_id: str
    payload: dict                  # full InboundMessage.model_dump()
    status: InboundEventLogStatus = InboundEventLogStatus.PENDING
    processed_at: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())

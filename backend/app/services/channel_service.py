"""ChannelService — orchestration layer between IM channels and agent execution.

Analogue of AgentExecutionService for inbound IM messages. Responsibilities:
  1. Idempotency: dedup by platform_message_id before processing.
  2. Session resolution: encode (channel_id, platform_chat_id) into user_id,
     reusing the existing session mechanism with zero changes.
  3. Execution: delegate to AgentExecutionService.invoke (reuses all existing
     prompt rendering / tool assembly / token budget / persistence).
  4. Outbound: translate reply → OutboundEnvelope → adapter.send(), with
     bounded internal retries for transient send failures.
  5. Error handling: fallback reply + event-log update + degraded-state
     bookkeeping for credential failures.

Adapters themselves never call AgentExecutionService — they go through this
service so cross-cutting logic stays in one place. This is the only call
site for AgentExecutionService outside the HTTP API layer.
"""
from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime

from pymongo.errors import DuplicateKeyError

from app.channels.base import InboundMessage, OutboundEnvelope
from app.channels.errors import (
    AgentRuntimeError,
    PermanentChannelError,
    SendFailedError,
    TransientChannelError,
)
from app.channels.registry import ChannelRegistry
from app.core.config import settings
from app.db.mongodb import get_database
from app.models.channel import (
    ChannelConfig,
    ChannelProvider,
    ChannelStatus,
    InboundEventLog,
    InboundEventLogStatus,
)
from app.schemas.execution import ExecutionRequest
from app.services.agent_execution_service import AgentExecutionService

logger = logging.getLogger(__name__)


class ChannelService:
    # ── DB access ──

    @staticmethod
    def _configs_coll():
        return get_database().channel_configs

    @staticmethod
    def _event_logs_coll():
        return get_database().inbound_event_logs

    @staticmethod
    async def get_config(channel_id: str) -> ChannelConfig | None:
        doc = await ChannelService._configs_coll().find_one({"_id": channel_id})
        return ChannelConfig(**doc) if doc else None

    @staticmethod
    async def get_event_log(log_id: str) -> InboundEventLog | None:
        doc = await ChannelService._event_logs_coll().find_one({"_id": log_id})
        return InboundEventLog(**doc) if doc else None

    # ── Idempotency ──

    @staticmethod
    async def create_or_dedup_event(inbound: InboundMessage) -> str | None:
        """Insert a pending event log entry, dedup by platform_message_id.

        Returns the new log id, or None if the event was already processed
        (duplicate). Caller should ack the platform and skip processing on None.
        """
        coll = ChannelService._event_logs_coll()
        existing = await coll.find_one({
            "channel_id": inbound.channel_id,
            "platform_message_id": inbound.message_id,
        })
        if existing:
            return None
        log = InboundEventLog(
            channel_id=inbound.channel_id,
            platform_message_id=inbound.message_id,
            payload=inbound.model_dump(mode="json"),
        )
        try:
            await coll.insert_one(log.model_dump(by_alias=True))
        except DuplicateKeyError:
            # Concurrent race: another request inserted the same
            # (channel_id, platform_message_id) between our find_one and
            # insert_one. Treat as a duplicate and ack the platform.
            return None
        return log.id

    # ── Orchestration ──

    @staticmethod
    async def execute(
        inbound: InboundMessage, event_log_id: str | None = None
    ) -> None:
        """Resolve session, invoke agent, send reply.

        TransientChannelError propagates (the Celery task retries the whole
        message). PermanentChannelError → handle_error (fallback reply).

        Args:
            inbound: The normalized inbound message to process.
            event_log_id: Optional id of the persisted InboundEventLog. When
                provided, handle_error is handed the *real* persisted log so it
                can update its status to FAILED. When None (e.g. synchronous
                test callers without a persisted log), an in-memory log is
                reconstructed — handle_error's status update is then a no-op,
                which is acceptable for those callers.
        """
        config = await ChannelService.get_config(inbound.channel_id)
        if config is None or not config.enabled:
            logger.warning("channel %s missing or disabled", inbound.channel_id)
            return

        try:
            reply_text = await ChannelService._invoke_agent(inbound, config)
            await ChannelService._send_reply(inbound, config, reply_text)
            await ChannelService._reset_failure_counter(config.id)
        except PermanentChannelError as e:
            logger.warning("permanent channel error: %s", e)
            if event_log_id is not None:
                # Fetch the real persisted log so handle_error can update its
                # status from PENDING → FAILED on the actual document.
                real_log = await ChannelService.get_event_log(event_log_id)
                if real_log is None:
                    # Log was TTL'd or missing; reconstruct in-memory using the
                    # provided id so any (no-op) update still targets that id.
                    real_log = InboundEventLog(
                        _id=event_log_id,
                        channel_id=inbound.channel_id,
                        platform_message_id=inbound.message_id,
                        payload=inbound.model_dump(mode="json"),
                    )
            else:
                # Synchronous caller (e.g. tests) without a persisted log —
                # reconstruct a fresh in-memory log (new id).
                real_log = InboundEventLog(
                    channel_id=inbound.channel_id,
                    platform_message_id=inbound.message_id,
                    payload=inbound.model_dump(mode="json"),
                )
            await ChannelService.handle_error(real_log, config, e)
        # TransientChannelError intentionally propagates to the Celery task.

    @staticmethod
    async def _invoke_agent(inbound: InboundMessage, config: ChannelConfig) -> str:
        """Encode identity into user_id, call AgentExecutionService.invoke."""
        user_id = f"channel:{config.id}:{inbound.platform_chat_id}"
        body = ExecutionRequest(input=inbound.text)
        response = await AgentExecutionService.invoke(
            agent_id=config.agent_id,
            body=body,
            user_id=user_id,
        )
        return response.output

    @staticmethod
    async def _send_reply(
        inbound: InboundMessage, config: ChannelConfig, text: str
    ) -> None:
        """Translate reply → envelope → adapter.send(), with bounded retries.

        PermanentChannelError is re-raised immediately (no retry). Transient
        failures are retried up to CHANNEL_SEND_MAX_RETRIES times; if all
        attempts fail, the call is converted to SendFailedError.
        """
        envelope = OutboundEnvelope(
            channel_id=config.id,
            platform_chat_id=inbound.platform_chat_id,
            text=text,
            reply_to_message_id=inbound.message_id,
            # Pass selected inbound-derived fields so platform adapters that
            # need them (e.g. DingTalk's session_webhook) can reply.
            context=_extract_send_context(inbound),
        )
        adapter = ChannelRegistry.get(config.provider)
        last_err: Exception | None = None
        for attempt in range(1, settings.CHANNEL_SEND_MAX_RETRIES + 1):
            try:
                return await _call_send(adapter, envelope, config)
            except PermanentChannelError:
                raise  # don't retry permanent errors
            except TransientChannelError as e:
                last_err = e
                logger.info("send attempt %d failed (transient): %s", attempt, e)
            except Exception as e:
                last_err = e
                logger.error("send attempt %d failed: %s", attempt, e)
        raise SendFailedError(
            f"send failed after {settings.CHANNEL_SEND_MAX_RETRIES} attempts: {last_err}"
        )

    # ── Error handling ──

    @staticmethod
    async def handle_error(
        event_log: InboundEventLog,
        config: ChannelConfig,
        error: PermanentChannelError,
    ) -> None:
        """Send fallback user_message + mark event log failed + bookkeeping."""
        # 1. Reply user-facing message (best-effort — don't shadow the real error)
        inbound = InboundMessage(**event_log.payload)
        envelope = OutboundEnvelope(
            channel_id=config.id,
            platform_chat_id=inbound.platform_chat_id,
            text=error.user_message,
            reply_to_message_id=inbound.message_id,
            context=_extract_send_context(inbound),
        )
        adapter = ChannelRegistry.get(config.provider)
        try:
            await _call_send(adapter, envelope, config)
        except Exception as send_err:
            logger.error("fallback reply also failed: %s", send_err)

        # 2. Update event log status
        await ChannelService._event_logs_coll().update_one(
            {"_id": event_log.id},
            {"$set": {
                "status": InboundEventLogStatus.FAILED,
                "processed_at": datetime.now(UTC).isoformat(),
                "error": f"{type(error).__name__}: {error}",
            }},
        )

        # 3. Per spec §5.2.4, every PermanentChannelError counts toward
        #    consecutive_failures except AgentRuntimeError (which is a code
        #    bug in the agent itself, not the channel — degrading would punish
        #    every other user on this channel for a bug they didn't cause).
        #    InvalidCredentialsError AND SendFailedError (e.g. platform API
        #    outage) both bump; otherwise a persistently failing channel
        #    would silently lose every message and stay ACTIVE forever.
        if not isinstance(error, AgentRuntimeError):
            await ChannelService._bump_failure_counter(config.id)

    @staticmethod
    async def _bump_failure_counter(channel_id: str) -> None:
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$inc": {"consecutive_failures": 1}},
        )
        await ChannelService._maybe_degrade(channel_id)

    @staticmethod
    async def _reset_failure_counter(channel_id: str) -> None:
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$set": {
                "consecutive_failures": 0,
                "status": ChannelStatus.ACTIVE,
            }},
        )

    @staticmethod
    async def _maybe_degrade(channel_id: str) -> None:
        cfg = await ChannelService._configs_coll().find_one({"_id": channel_id})
        if cfg and cfg.get("consecutive_failures", 0) >= settings.CHANNEL_DEGRADED_ON_CONSECUTIVE_FAILURES:
            await ChannelService._configs_coll().update_one(
                {"_id": channel_id},
                {"$set": {"status": ChannelStatus.DEGRADED}},
            )
            logger.warning(
                "channel %s auto-degraded after %d failures",
                channel_id, cfg["consecutive_failures"],
            )

    # ── CRUD (called by management API) ──

    @staticmethod
    async def create_channel(
        *, name: str, provider: ChannelProvider, agent_id: str,
        credentials: dict, owner_user_id: str,
        receive_mode: str = "webhook",
    ) -> ChannelConfig:
        import secrets

        from app.core.crypto import encrypt_secret

        encrypted_creds = {
            k: encrypt_secret(str(v)) for k, v in credentials.items() if v
        }
        cfg = ChannelConfig(
            name=name, provider=provider, agent_id=agent_id,
            owner_user_id=owner_user_id,
            credentials=encrypted_creds,
            webhook_secret=secrets.token_urlsafe(32),
            receive_mode=receive_mode,
        )
        await ChannelService._configs_coll().insert_one(cfg.model_dump(by_alias=True))
        return cfg

    @staticmethod
    async def list_channels(
        *, owner_user_id: str, page: int = 1, page_size: int = 20,
    ) -> tuple[list[ChannelConfig], int]:
        skip = (page - 1) * page_size
        coll = ChannelService._configs_coll()
        total = await coll.count_documents({"owner_user_id": owner_user_id})
        cursor = coll.find({"owner_user_id": owner_user_id}).skip(skip).limit(page_size)
        docs = await cursor.to_list(length=page_size)
        return [ChannelConfig(**d) for d in docs], total

    @staticmethod
    async def get_channel(channel_id: str, owner_user_id: str) -> ChannelConfig | None:
        doc = await ChannelService._configs_coll().find_one({
            "_id": channel_id, "owner_user_id": owner_user_id,
        })
        return ChannelConfig(**doc) if doc else None

    @staticmethod
    async def update_channel(
        channel_id: str, owner_user_id: str, *, name=None, agent_id=None,
        credentials: dict | None = None, enabled=None,
        receive_mode: str | None = None,
    ) -> ChannelConfig | None:
        from app.core.crypto import encrypt_secret

        update: dict = {"updated_at": datetime.now(UTC).isoformat()}
        if name is not None:
            update["name"] = name
        if agent_id is not None:
            update["agent_id"] = agent_id
        if enabled is not None:
            update["enabled"] = enabled
        if receive_mode is not None:
            update["receive_mode"] = receive_mode
        if credentials:
            # Merge: only overwrite keys the caller explicitly provided.
            # A partial update (e.g. only verification_token) must NOT wipe
            # existing app_id/app_secret — that was a real bug where editing
            # one credential field in the UI cleared the others.
            existing_doc = await ChannelService._configs_coll().find_one(
                {"_id": channel_id, "owner_user_id": owner_user_id},
                projection={"credentials": 1},
            )
            merged: dict = dict(existing_doc.get("credentials", {})) if existing_doc else {}
            for k, v in credentials.items():
                if v:  # skip empty values (UI "leave unchanged")
                    merged[k] = encrypt_secret(str(v))
            update["credentials"] = merged
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id, "owner_user_id": owner_user_id},
            {"$set": update},
        )
        return await ChannelService.get_channel(channel_id, owner_user_id)

    @staticmethod
    async def delete_channel(channel_id: str) -> None:
        """Soft delete: disable + mark DISABLED. Keeps row for audit/event logs."""
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$set": {
                "enabled": False,
                "status": ChannelStatus.DISABLED,
                "updated_at": datetime.now(UTC).isoformat(),
            }},
        )

    @staticmethod
    async def set_enabled(channel_id: str, enabled: bool) -> None:
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$set": {
                "enabled": enabled,
                "status": ChannelStatus.ACTIVE if enabled else ChannelStatus.DISABLED,
                "updated_at": datetime.now(UTC).isoformat(),
            }},
        )

    @staticmethod
    async def reset_degraded(channel_id: str) -> None:
        """Manually clear DEGRADED state + reset failure counter."""
        await ChannelService._configs_coll().update_one(
            {"_id": channel_id},
            {"$set": {
                "consecutive_failures": 0,
                "status": ChannelStatus.ACTIVE,
                "updated_at": datetime.now(UTC).isoformat(),
            }},
        )

    @staticmethod
    def mask_credentials(credentials: dict) -> dict:
        from app.core.crypto import mask_secret

        return {k: mask_secret(str(v)) for k, v in credentials.items()}


# Adapter.send() may be sync (MockChannel) or async (Lark/DingTalk/WeCom).
# Detect and await if needed.
async def _call_send(
    adapter, envelope: OutboundEnvelope, config: ChannelConfig
) -> str:
    result = adapter.send(envelope, config)
    if inspect.isawaitable(result):
        return await result
    return result


def _extract_send_context(inbound: InboundMessage) -> dict:
    """Pull platform-specific reply state from an inbound message's raw payload.

    Most platforms need nothing (they send via a stable OpenAPI + token). The
    notable exception is DingTalk, whose bot reply address (session_webhook)
    is embedded in each inbound event and expires after ~2h. We surface known
    platform-specific keys here so adapters can pick them up from the
    OutboundEnvelope without each adapter re-parsing InboundMessage.raw.
    """
    raw = inbound.raw or {}
    context: dict = {}
    # DingTalk stream / webhook both carry session_webhook on inbound events.
    sw = raw.get("session_webhook") or raw.get("sessionWebhook")
    if sw:
        context["session_webhook"] = sw
    return context

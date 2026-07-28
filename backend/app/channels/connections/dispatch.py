"""Shared inbound dispatcher for long-connection events.

Long-connection clients receive raw platform events. Each client converts its
event into the same JSON body the HTTP webhook would receive, then calls
``dispatch_inbound(config, body)``. The dispatcher parses, dedups, persists,
and **executes directly in the FastAPI process** — no Celery round-trip.

Why not Celery for long-connection mode?
---------------------------------------
Webhook mode MUST ack within seconds (platform timeout), so it hands the work
to Celery. Long-connection mode has no ack pressure — the WebSocket is
persistent — so executing inline yields lower latency, removes the worker
deployment dependency, and lets failures surface immediately to the
connection manager. Transient errors are retried in-process with exponential
backoff; a per-channel semaphore caps concurrency so one busy chat can't
starve the LLM quota.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from app.models.channel import ChannelConfig

logger = logging.getLogger(__name__)

# Parser type: (body, config) -> InboundMessage | None
# Each provider wires its own ``parse_xxx_event`` here.
EventParser = Callable[[str | bytes | dict, ChannelConfig], "object | None"]


async def dispatch_inbound(
    *, config: ChannelConfig, body: str | bytes | dict, parser: EventParser
) -> str | None:
    """Parse a long-connection event and execute it inline.

    Args:
        config: The channel that received the event.
        body: Raw platform payload, already verified by the SDK transport.
              For lark/dingtalk this is the same JSON the webhook would
              receive; pass it through unchanged.
        parser: Provider-specific parser (e.g. ``parse_lark_event``).

    Returns:
        The created InboundEventLog id, or None if dedup / parse skipped it.
    """
    # Late imports to avoid circulars at module import time.
    from app.channels.connections.manager import get_connection_manager
    from app.services.channel_service import ChannelService

    if isinstance(body, (bytes, bytearray)):
        body_str = body.decode("utf-8", errors="replace")
    elif isinstance(body, dict):
        body_str = json.dumps(body, ensure_ascii=False)
    else:
        body_str = body

    try:
        inbound = parser(body_str, config)
    except Exception as exc:
        logger.warning(
            "long_connection_parse_failed channel=%s err=%s", config.id, exc
        )
        return None

    if inbound is None:
        # Non-text message, URL verification marker, or empty content — skip.
        return None

    log_id = await ChannelService.create_or_dedup_event(inbound)
    if log_id is None:
        logger.debug(
            "long_connection_dedup channel=%s msg=%s",
            config.id, getattr(inbound, "message_id", "?"),
        )
        return None

    # Execute inline (no Celery). The per-channel semaphore caps concurrency;
    # we get it from the connection manager so the cap is shared across all
    # events on this channel. Run as a fire-and-forget task so the SDK callback
    # returns immediately — the platform doesn't wait for our agent to finish.
    sem = get_connection_manager().execution_semaphore(config.id)
    asyncio.create_task(
        _execute_with_retry(inbound, log_id, sem)
    )
    return log_id


async def _execute_with_retry(
    inbound, log_id: str, sem: asyncio.Semaphore | None,
) -> None:
    """Run ChannelService.execute with bounded concurrency + retry on transient errors.

    Mirrors the Celery task's retry semantics (max_retries + exponential
    backoff) but in-process. PermanentChannelError is handled inside execute
    itself (it calls handle_error), so we only catch transient here.
    """
    from app.channels.errors import TransientChannelError
    from app.core.config import settings
    from app.services.channel_service import ChannelService

    max_retries = settings.CHANNEL_EXECUTION_MAX_RETRIES

    async def _run_once() -> None:
        await ChannelService.execute(inbound, event_log_id=log_id)

    for attempt in range(1, max_retries + 2):  # 1..max_retries+1 tries
        try:
            if sem is not None:
                async with sem:
                    await _run_once()
            else:
                await _run_once()
            return
        except TransientChannelError as exc:
            if attempt > max_retries:
                logger.error(
                    "long_connection_execute_retries_exhausted "
                    "channel=%s log=%s attempts=%d err=%s",
                    inbound.channel_id, log_id, attempt, exc,
                )
                # ChannelService.execute already routes PermanentChannelError
                # to handle_error; an exhausted-transient here leaves the log
                # in PENDING. Mark it FAILED so it's not retried forever.
                await _mark_log_failed(log_id, exc)
                return
            backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s, ...
            logger.warning(
                "long_connection_transient_retry channel=%s log=%s "
                "attempt=%d/%d backoff=%ds err=%s",
                inbound.channel_id, log_id, attempt, max_retries, backoff, exc,
            )
            await asyncio.sleep(backoff)
        except Exception as exc:
            # Non-transient unexpected error — log and stop. execute() handles
            # PermanentChannelError internally; this catches anything else
            # escaping (programming bug, network blip in send).
            logger.exception(
                "long_connection_execute_unexpected channel=%s log=%s err=%s",
                inbound.channel_id, log_id, exc,
            )
            await _mark_log_failed(log_id, exc)
            return


async def _mark_log_failed(log_id: str, exc: Exception) -> None:
    """Mark an event log FAILED after retries are exhausted or on unexpected error."""
    from datetime import UTC, datetime

    from app.models.channel import InboundEventLogStatus
    from app.services.channel_service import ChannelService

    try:
        await ChannelService._event_logs_coll().update_one(
            {"_id": log_id},
            {"$set": {
                "status": InboundEventLogStatus.FAILED,
                "processed_at": datetime.now(UTC).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }},
        )
    except Exception:
        logger.exception("long_connection_mark_failed_failed log=%s", log_id)

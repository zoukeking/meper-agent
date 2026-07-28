"""Celery task: process one inbound IM message.

Dispatched by the inbound route (Task 10) via ``process_inbound.delay(log_id)``
right after the event log has been durably persisted. This task is the async
processing tail of the inbound pipeline.

Flow::

    event_log_id → ChannelService.execute
      ├─ TransientChannelError → self.retry (30s backoff, max 3 attempts)
      │     └─ retries exhausted → fall back to handle_error (permanent)
      └─ PermanentChannelError → ChannelService.handle_error (fallback reply)

The task body runs its async work on the shared worker loop via ``run_async`` —
do NOT use ``asyncio.run()`` here: it would create a fresh loop and break the
motor singleton pinned to the worker's persistent loop (see workers/loop.py).
"""
from __future__ import annotations

import logging

from app.channels.errors import PermanentChannelError, TransientChannelError
from app.services.channel_service import ChannelService
from app.workers.celery_app import celery_app
from app.workers.loop import run_async

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.tasks.channel_inbound.process_inbound",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def process_inbound(self, event_log_id: str) -> None:
    """Process one inbound message. Executes synchronously on the worker."""
    run_async(_run(self, event_log_id))


async def _run(task, event_log_id: str) -> None:
    """Async body of process_inbound.

    Kept as a module-level coroutine (mirroring the scheduled_workflow /
    workflow_execution task layout) so it can be unit-tested in isolation if
    needed. ``task`` is the bound Celery task instance (for retry control).
    """
    event_log = await ChannelService.get_event_log(event_log_id)
    if event_log is None:
        logger.warning("event_log_not_found event_log_id=%s", event_log_id)
        return

    # Reconstruct the original InboundMessage from the persisted payload.
    from app.channels.base import InboundMessage

    inbound = InboundMessage(**event_log.payload)

    try:
        # Thread the real event_log_id through so execute's PermanentChannelError
        # branch updates the *actual* persisted log instead of a dummy.
        await ChannelService.execute(inbound, event_log_id=event_log_id)
    except TransientChannelError as e:
        # Retries exhausted → convert to a permanent failure so the user gets a
        # fallback reply. Guarding retries >= max_retries avoids Celery's
        # MaxRetriesExceededError bubbling up unhandled.
        if task.request.retries >= task.max_retries:
            logger.warning(
                "inbound_retries_exhausted event_log_id=%s error=%s",
                event_log_id, e,
            )
            config = await ChannelService.get_config(event_log.channel_id)
            if config is not None:
                await ChannelService.handle_error(
                    event_log, config,
                    PermanentChannelError("重试耗尽: " + str(e)),
                )
        else:
            # ``task.retry()`` raises Celery's Retry control-flow signal (not a
            # chained copy of ``e``), so B904's "raise ... from err" doesn't
            # apply here — this is the documented Celery idiom.
            raise task.retry(exc=e)  # noqa: B904
    except PermanentChannelError as e:
        config = await ChannelService.get_config(event_log.channel_id)
        if config is not None:
            await ChannelService.handle_error(event_log, config, e)

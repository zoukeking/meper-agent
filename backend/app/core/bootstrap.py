"""Application bootstrap — extracted from main.py lifespan.

Each function corresponds to a startup concern, allowing:
- Parallel execution (``asyncio.gather``) of independent steps.
- Background execution (``asyncio.create_task``) of non-critical steps.
- Independent testing of each initialization phase.

Startup phases:
1. **Critical path** (must complete before first request):
   - checkpointer (agent execution depends on it)
   - notification service (WebSocket push depends on it)
   - event bridge (Celery→FastAPI event delivery)
2. **Background boot** (deferred via ``create_task``):
   - indexes (4 collections, parallelized)
   - schedulers (task + trigger)
   - task recovery (orphan + waiting_human)
   - channel long connections
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:

    from app.services.task_scheduler_service import TaskSchedulerService
    from app.services.trigger_repo import TriggerRepository
    from app.services.trigger_scheduler_service import TriggerSchedulerService


# ---------------------------------------------------------------------------
# Critical path — must be ready before serving requests
# ---------------------------------------------------------------------------


async def init_critical_path() -> None:
    """Run the minimum startup steps that block correct request handling."""
    # Checkpointer — agent interrupt/resume depends on it.
    from app.core.checkpointer import configure_mongo_checkpointer

    configure_mongo_checkpointer(log_on_error=False)

    # Notification service — bridges EventBus → WebSocket + MongoDB.
    # Must register before any task events arrive.
    from app.services.notification_service import NotificationService

    NotificationService().register()

    # Event bridge — Redis pub/sub listener for Celery worker events.
    from app.services.event_bridge import start_event_bridge_listener

    await start_event_bridge_listener()


# ---------------------------------------------------------------------------
# Background boot — deferred, non-blocking
# ---------------------------------------------------------------------------


async def ensure_all_indexes() -> TriggerRepository:
    """Create indexes for all collections (parallelized, idempotent)."""
    from app.db.mongodb import get_database
    from app.services.api_key_service import (
        ApiKeyService,  # noqa: F401 (avoid eager import cycle)
    )
    from app.services.execution_log_service import ExecutionLogService
    from app.services.role_service import RoleService  # noqa: F401
    from app.services.trigger_repo import TriggerRepository

    trigger_repo = TriggerRepository(get_database())

    await asyncio.gather(
        trigger_repo.ensure_indexes(),
        RoleService.ensure_indexes(),
        ApiKeyService.ensure_indexes(),
        ExecutionLogService.ensure_indexes(),
        return_exceptions=True,
    )

    # init_system_roles depends on RoleService.ensure_indexes completing.
    await RoleService.init_system_roles()
    return trigger_repo


async def start_schedulers(trigger_repo: object) -> tuple[TaskSchedulerService, TriggerSchedulerService]:
    """Start the task + trigger schedulers (fire-and-forget poll loops)."""
    from app.services.task_scheduler_service import get_scheduler
    from app.services.trigger_scheduler_service import get_trigger_scheduler

    scheduler = get_scheduler()
    await scheduler.start()

    trigger_scheduler = get_trigger_scheduler()
    trigger_scheduler.set_repo(trigger_repo)  # type: ignore[arg-type]
    await trigger_scheduler.start()
    return scheduler, trigger_scheduler


async def recover_tasks() -> None:
    """Recover tasks orphaned by the previous process crash/restart."""
    from app.services.task_recovery import (
        recover_orphan_running_tasks,
        recover_waiting_human_tasks,
    )

    await recover_waiting_human_tasks()
    await recover_orphan_running_tasks()


async def start_channel_connections() -> None:
    """Start long-connection clients for IM channels.

    Failures are non-fatal — webhook-mode channels are unaffected, and
    long-connection channels simply won't receive events.
    """
    try:
        from app.channels import providers  # noqa: F401 (triggers factory registration)
        from app.channels.connections import get_connection_manager

        await get_connection_manager().start()
    except Exception as exc:  # pragma: no cover — never block startup
        logger.error("connection_manager_startup_failed err={}", exc)


async def background_boot() -> tuple[TaskSchedulerService, TriggerSchedulerService]:
    """Background initialization: indexes → schedulers + recovery + channels.

    Runs after the critical path, deferred via ``create_task`` so the first
    request isn't blocked by index creation or channel connection setup.
    """
    # Phase 1: indexes (parallel, must complete before schedulers that read them)
    trigger_repo = await ensure_all_indexes()

    # Phase 2: independent startup tasks (parallel).
    # gather returns a list of results in order; start_schedulers is first.
    results = await asyncio.gather(
        start_schedulers(trigger_repo),
        recover_tasks(),
        start_channel_connections(),
    )
    schedulers = results[0]  # (scheduler, trigger_scheduler)
    return schedulers


async def shutdown(
    scheduler: TaskSchedulerService | None,
    trigger_scheduler: TriggerSchedulerService | None,
) -> None:
    """Graceful shutdown — stop connections, schedulers, then DB clients."""
    from app.channels.connections import get_connection_manager
    from app.db.mongodb import close_mongodb_client
    from app.db.redis import close_redis_client
    from app.services.event_bridge import stop_event_bridge_listener

    await get_connection_manager().stop()
    await stop_event_bridge_listener()
    if trigger_scheduler is not None:
        await trigger_scheduler.stop()
    if scheduler is not None:
        await scheduler.stop()
    await close_mongodb_client()
    await close_redis_client()

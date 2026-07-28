"""Celery application instance and configuration."""
from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]
from celery.signals import (  # type: ignore[import-untyped]
    task_prerun,
    worker_process_init,
)
from loguru import logger

from app.core.config import settings

celery_app = Celery(
    "agent_flow",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.tasks.maintenance",
        "app.workers.tasks.webhook_delivery",
        "app.workers.tasks.scheduled_workflow",
        "app.workers.tasks.workflow_execution",
        "app.workers.tasks.channel_inbound",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # 兜底超时：防卡死的工作流永久占用 worker（30 分钟软超时）。
    # 与取消功能正交，但提升健壮性。
    task_soft_time_limit=1800,
    task_time_limit=1860,
    # Redis visibility_timeout: if a worker takes a message but doesn't ack
    # within this window, Redis restores it to the ready queue for re-delivery.
    # The project no longer uses Celery eta (scheduling is polling-based via
    # TriggerSchedulerService), so the default 1h is sufficient — kept at 3h
    # for safety margin. The zombie guard in run_and_persist handles any
    # re-delivered cancelled tasks.
    broker_transport_options={"visibility_timeout": 3600 * 3},  # 3 hours
    # Periodic tasks (Celery Beat)
    beat_schedule={
        "cleanup-expired-workspaces": {
            "task": "app.workers.tasks.maintenance.cleanup_expired_workspaces",
            # Run daily at 03:00 UTC
            "schedule": crontab(hour=3, minute=0),
        },
    },
)


@worker_process_init.connect
def _init_worker(**_kwargs: object) -> None:
    """每个 Celery worker 子进程启动时的初始化。

    1. **日志初始化** — Celery worker 是独立进程，不走 FastAPI 的 lifespan，
       ``app.main`` 里的 ``setup_logging()`` 不会执行。必须在此显式调用，
       否则 worker 日志走 loguru 默认 sink（stderr 彩色输出），不写文件、
       不受 ``LOG_LEVEL`` / ``LOG_JSON_FORMAT`` 控制、格式与主进程不一致。

    2. **MongoDB checkpointer** — 同上，harness 默认使用 ``InMemorySaver``，
       agent 节点的 checkpoint 只存在于内存中，进程重启后丢失，前端无法
       按 task+node 反查执行详情。
    """
    # 1. 日志初始化 — 必须在打任何日志之前完成
    from app.core.logging import setup_logging
    setup_logging()

    # 2. Checkpointer 配置 — shared with FastAPI lifespan via app.core.checkpointer
    from app.core.checkpointer import configure_mongo_checkpointer

    configure_mongo_checkpointer(log_on_error=True)


@task_prerun.connect
def _inject_request_id(task_id=None, **_kwargs: object) -> None:
    """在 Celery task 执行前注入 ``request_id`` 到 contextvar。

    这样 worker 里所有 ``logger.xxx()`` 都能带上 task 关联标识，
    便于在日志中按 task 反查执行链路。使用 task_id 的前 8 位作为
    request_id（与 HTTP 请求的 8 位 hex 格式保持一致）。
    """
    from app.api.middleware.request_id import request_id_var

    rid = (task_id or "-")[:8]
    request_id_var.set(rid)
    logger.contextualize(request_id=rid).__enter__()


__all__ = ["celery_app"]

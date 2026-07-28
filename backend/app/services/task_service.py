"""Task business logic — CRUD, state machine with optimistic locking, timeline, audit."""
from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

from loguru import logger
from pymongo import ReturnDocument

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.mongodb import get_database
from app.engine.events import TaskEvent, get_event_bus
from app.models.audit_log import AuditLog
from app.models.task import (
    TERMINAL_STATUSES,
    Task,
    TaskError,
    TaskStatus,
    TimelineEvent,
    is_valid_transition,
    utc_now,
)


class TaskService:
    """Service layer for Task operations."""

    COLLECTION = "tasks"
    AUDIT_COLLECTION = "audit_logs"

    @staticmethod
    def _collection():
        return get_database()[TaskService.COLLECTION]

    @staticmethod
    def _audit_collection():
        return get_database()[TaskService.AUDIT_COLLECTION]

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def create_task(
        workflow_id: str,
        input_data: dict[str, Any],
        created_by: str = "",
        created_by_type: str = "user",
        parent_task_id: str | None = None,
        call_chain: list[str] | None = None,
        scheduled_at: datetime | None = None,
        ext_metadata: dict[str, Any] | None = None,
        skip_execution: bool = False,
        source: str = "manual",
        trigger_id: str | None = None,
    ) -> dict:
        """Create a new Task in pending status.

        Args:
            workflow_id: Target workflow template ID.
            input_data: Input parameters conforming to workflow's input_schema.
            created_by: User or Agent ID who created this Task.
            created_by_type: "user", "agent", or "system".
            parent_task_id: Parent Task ID (for subflow scenarios).
            call_chain: Call chain for nested execution tracking.
            scheduled_at: Optional scheduled execution time.

        Returns:
            Created Task MongoDB document.

        Raises:
            ValidationError: If workflow_id is empty.
        """
        logger.debug(
            "task_create_start",
            workflow_id=workflow_id,
            created_by=created_by,
            created_by_type=created_by_type,
            source=source,
            skip_execution=skip_execution,
            parent_task_id=parent_task_id,
            input_keys=list(input_data.keys()) if input_data else [],
        )
        if not workflow_id:
            raise ValidationError(
                code="TASK_MISSING_WORKFLOW_ID",
                message="workflow_id 不能为空",
            )

        # 校验 workflow 模板存在，避免创建指向已删除/不存在模板的僵尸 task
        # （模板不存在时 engine.run_and_persist 会加载失败，但 task 已写入
        # pending 且不会被清理，造成永久卡死）。
        db = get_database()
        workflow_exists = await db["workflows"].find_one(
            {"_id": workflow_id}, {"_id": 1}
        )
        if not workflow_exists:
            raise ValidationError(
                code="WORKFLOW_NOT_FOUND",
                message=f"工作流模板 {workflow_id} 不存在，无法创建任务",
                details={"workflow_id": workflow_id},
            )

        # Build initial timeline
        now = utc_now()
        initial_timeline = [
            TimelineEvent(
                timestamp=now,
                event_type="created",
                data={"workflow_id": workflow_id, "created_by": created_by},
                actor=created_by or "system",
            )
        ]

        task = Task(
            workflow_id=workflow_id,
            input=input_data,
            created_by=created_by,
            created_by_type=created_by_type,
            parent_task_id=parent_task_id,
            call_chain=call_chain or [],
            scheduled_at=scheduled_at,
            source=source,
            trigger_id=trigger_id,
            timeline=[e.model_dump() for e in initial_timeline],
            created_at=now,
            updated_at=now,
        )

        doc = task.model_dump(by_alias=True)
        if ext_metadata:
            doc.update(ext_metadata)
        result = await TaskService._collection().insert_one(doc)

        # Write audit log
        await TaskService._write_audit_log(
            task_id=task.id,
            event_type="state_change",
            from_status=None,
            to_status=TaskStatus.PENDING.value,
            triggered_by=created_by,
            triggered_by_type=created_by_type,
            version=1,
            details={"workflow_id": workflow_id},
        )

        logger.info("task_created", task_id=task.id, workflow_id=workflow_id)

        created_doc = await TaskService._collection().find_one({"_id": result.inserted_id})

        # Publish event
        event_bus = get_event_bus()
        await event_bus.publish(TaskEvent(
            event_type="task.created",
            task_id=task.id,
            to_status=TaskStatus.PENDING.value,
            data={"workflow_id": workflow_id, "created_by": created_by},
        ))

        # Also publish to Redis so Celery worker events reach FastAPI via pub/sub
        from app.services.event_bridge import publish_task_event_to_redis

        await publish_task_event_to_redis(
            event_type="task.created",
            task_id=task.id,
            from_status=None,
            to_status=TaskStatus.PENDING.value,
            data={"workflow_id": workflow_id, "created_by": created_by},
        )

        # Trigger workflow engine execution in background (unless caller handles it)
        if not skip_execution:
            TaskService._start_workflow_execution(task.id)

        return created_doc

    @staticmethod
    def _start_workflow_execution(task_id: str) -> None:
        """Fire-and-forget: dispatch workflow execution to a Celery worker.

        The engine handles state transitions (PENDING→RUNNING→...) internally.
        This replaces the previous in-process ``asyncio.create_task`` approach,
        moving heavy workflow execution out of the FastAPI event loop and into
        a dedicated Celery worker for process isolation and crash recovery.
        """
        from app.workers.tasks.workflow_execution import run_workflow_task

        result = run_workflow_task.delay(task_id)
        # Persist the Celery task id for later cancellation (revoke).
        TaskService._store_celery_task_id(task_id, result.id)

    @staticmethod
    def resume_task_execution(task_id: str) -> None:
        """Fire-and-forget: resume a paused Task from checkpoint.

        Symmetric with ``_start_workflow_execution`` — used after Human node
        intervention (approve/skip) or after cancelling + resuming to continue
        workflow execution. Dispatched to a Celery worker; the engine detects
        the saved checkpoint and resumes from there.
        """
        from app.workers.tasks.workflow_execution import run_workflow_task

        result = run_workflow_task.delay(task_id)
        TaskService._store_celery_task_id(task_id, result.id)

    @staticmethod
    def _store_celery_task_id(task_id: str, celery_task_id: object) -> None:
        """Persist the Celery AsyncResult id onto the Task document.

        Called after every ``run_workflow_task.delay()`` so that
        :meth:`cancel_running_task` can later ``revoke`` the worker.
        Fire-and-forget on the running event loop (safe because these dispatch
        methods are always called from async API handlers).
        """
        # Guard: delay() returns an AsyncResult; only persist a real string id.
        if not isinstance(celery_task_id, str) or not celery_task_id:
            return

        import asyncio

        from app.db.mongodb import get_database

        async def _update() -> None:
            await get_database()["tasks"].update_one(
                {"_id": task_id},
                {"$set": {"celery_task_id": celery_task_id}},
            )

        with contextlib.suppress(RuntimeError):
            # No running loop — best-effort: skip (celery_task_id is a backup field)
            asyncio.ensure_future(_update())

    @staticmethod
    async def add_total_tokens(task_id: str, delta: int) -> None:
        """Increment the Task's cumulative token usage by *delta*.

        Called by WorkflowEngine as each agent node completes, so the Flow
        cost is visible incrementally and survives pause/resume (each Celery
        run starts a fresh engine instance, so we persist rather than tally).
        """
        from app.db.mongodb import get_database

        await get_database()["tasks"].update_one(
            {"_id": task_id},
            {"$inc": {"total_tokens": int(delta or 0)}, "$set": {"updated_at": utc_now()}},
        )

    @staticmethod
    async def cancel_running_task(task_id: str) -> None:
        """Revoke the Celery worker running *task_id* (best-effort).

        Called from the cancel API after the DB status is flipped to
        ``cancelled``. The running engine cooperatively checks the flag at
        node boundaries and inside the agent REACT loop (via
        ``cancel_checker``), so revocation is a **backup** for the case where
        the agent is deep inside a long LLM HTTP call.
        """
        doc = await TaskService.get_task(task_id)
        if doc is None:
            return
        celery_task_id = doc.get("celery_task_id", "")
        if not celery_task_id:
            return
        try:
            from app.workers.celery_app import celery_app

            celery_app.control.revoke(celery_task_id, terminate=True)
            logger.info("celery_task_revoked", task_id=task_id, celery_task_id=celery_task_id)
        except Exception as exc:
            # Revoke failure is non-fatal — the DB flag + cooperative check
            # are the primary cancellation mechanism.
            logger.warning(
                "celery_task_revoke_failed",
                task_id=task_id,
                celery_task_id=celery_task_id,
                error=str(exc),
            )

    @staticmethod
    async def get_task(task_id: str) -> dict | None:
        """Get a single Task by ID.

        Returns:
            Task document or None if not found.
        """
        return await TaskService._collection().find_one({"_id": task_id})

    @staticmethod
    async def get_task_or_404(task_id: str) -> dict:
        """Get a Task by ID or raise NotFoundError."""
        doc = await TaskService.get_task(task_id)
        if doc is None:
            raise NotFoundError(
                code="TASK_NOT_FOUND",
                message=f"Task {task_id} 不存在",
                details={"task_id": task_id},
            )
        return doc

    @staticmethod
    async def list_tasks(
        page: int = 1,
        page_size: int = 20,
        status: TaskStatus | None = None,
        created_by: str | None = None,
        workflow_id: str | None = None,
        trigger_id: str | None = None,
        source: str | None = None,
    ) -> tuple[list[dict], int]:
        """List Tasks with optional filters.

        Returns:
            Tuple of (task_docs, total_count).
        """
        query: dict[str, Any] = {}
        if status is not None:
            query["status"] = status.value
        if created_by:
            query["created_by"] = created_by
        if workflow_id:
            query["workflow_id"] = workflow_id
        if trigger_id:
            query["trigger_id"] = trigger_id
        if source:
            query["source"] = source

        cursor = (
            TaskService._collection()
            .find(query)
            .sort("created_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        items = await cursor.to_list(length=page_size)
        total = await TaskService._collection().count_documents(query)
        return items, total

    @staticmethod
    async def _cleanup_task_artifacts(task_id: str, *, delete_workspace: bool = True) -> None:
        """清理 task 关联的所有附属数据。

        清理范围：
        - FileRef + FileUsage（origin_id=task_id 的文件引用和使用记录）
        - 本地 task workspace 文件（input/output/tmp 目录）
        - LangGraph checkpointer 数据（thread_id 以 {task_id}_ 开头）

        Args:
            task_id: Task ID.
            delete_workspace: 是否删除本地 workspace 目录（删除 task 时 True，
                重试时也 True——engine 会按需重建）。
        """
        db = get_database()

        # 1. 删除 FileRef + FileUsage
        try:
            file_refs = db.file_refs
            file_usages = db.file_usages
            # 先查出关联的 file_id，再删 FileRef 和 FileUsage
            cursor = file_refs.find(
                {"origin_id": task_id}, {"_id": 1},
            )
            file_ids = [doc["_id"] async for doc in cursor]
            if file_ids:
                await file_usages.delete_many(
                    {"file_id": {"$in": file_ids}},
                )
                await file_refs.delete_many(
                    {"_id": {"$in": file_ids}},
                )
                logger.info("task_files_cleaned", task_id=task_id, count=len(file_ids))
        except Exception as exc:
            logger.warning("task_files_cleanup_failed", task_id=task_id, error=str(exc))

        # 2. 删除本地 task workspace
        if delete_workspace:
            try:
                # 需要 user_id 来定位 workspace 路径
                task_doc = await db["tasks"].find_one({"_id": task_id}, {"created_by": 1})
                user_id = (task_doc or {}).get("created_by", "")
                if user_id:
                    from app.engine.tool.workspace import WorkspaceManager

                    WorkspaceManager.delete_task_workspace(user_id, task_id)
            except Exception as exc:
                logger.warning("task_workspace_cleanup_failed", task_id=task_id, error=str(exc))

        # 3. 清理 LangGraph checkpointer（pymongo 同步 Collection）
        try:
            from app.engine.harness_integration import get_checkpointer

            checkpointer = get_checkpointer()
            cp_col = getattr(checkpointer, "checkpoint_collection", None)
            writes_col = getattr(checkpointer, "writes_collection", None)
            thread_prefix = f"{task_id}_"
            if cp_col is not None:
                cp_col.delete_many({"thread_id": {"$regex": f"^{thread_prefix}"}})
            if writes_col is not None:
                writes_col.delete_many({"thread_id": {"$regex": f"^{thread_prefix}"}})
        except Exception as exc:
            logger.warning("task_checkpointer_cleanup_failed", task_id=task_id, error=str(exc))

    @staticmethod
    async def _clear_checkpointer_threads(task_id: str, node_ids: set[str]) -> None:
        """Clear LangGraph checkpointer threads for the given node IDs.

        Scoped counterpart of the checkpointer cleanup in
        :meth:`_cleanup_task_artifacts`: instead of deleting every thread under
        ``{task_id}_`` (which would wipe upstream nodes' history too), this
        deletes only the threads ``{task_id}_{node_id}`` for the supplied
        nodes. Used by :meth:`rewind_task` to clear the trimmed (target +
        downstream) nodes' threads so that re-execution starts a fresh agent
        conversation instead of appending to stale history (which would cause
        "Received multiple non-consecutive system messages").

        Non-agent nodes have no checkpointer thread, so deleting their
        (absent) thread is a harmless no-op.

        Args:
            task_id: Task ID.
            node_ids: Node IDs whose threads to delete (typically the rewind
                trim set).
        """
        if not node_ids:
            return
        try:
            from app.engine.harness_integration import get_checkpointer

            checkpointer = get_checkpointer()
            cp_col = getattr(checkpointer, "checkpoint_collection", None)
            writes_col = getattr(checkpointer, "writes_collection", None)
            thread_ids = [f"{task_id}_{nid}" for nid in node_ids]
            thread_filter = {"thread_id": {"$in": thread_ids}}
            if cp_col is not None:
                cp_col.delete_many(thread_filter)
            if writes_col is not None:
                writes_col.delete_many(thread_filter)
            logger.info(
                "rewind_checkpointer_cleared",
                task_id=task_id,
                node_count=len(node_ids),
                thread_ids=thread_ids,
            )
        except Exception as exc:
            # Cleanup is best-effort: if the checkpointer is unavailable, the
            # rewind still proceeds (resume may fail with the system-message
            # error, which is a clearer signal than blocking the rewind here).
            logger.warning("rewind_checkpointer_clear_failed", task_id=task_id, error=str(exc))

    @staticmethod
    async def delete_task(task_id: str) -> bool:
        """Delete a terminal Task.

        Only terminal-state Tasks (completed/failed/cancelled) can be deleted.

        Returns:
            True if deleted, False if not found.

        Raises:
            ConflictError: If Task is not in a terminal state.
        """
        doc = await TaskService.get_task_or_404(task_id)
        status = TaskStatus(doc["status"])
        if status not in TERMINAL_STATUSES:
            raise ConflictError(
                code="TASK_NOT_TERMINAL",
                message=f"Task {task_id} 状态为 {status.value}，仅允许删除终态 Task",
                details={"task_id": task_id, "status": status.value},
            )

        # 先清理附属数据（文件、workspace、checkpointer），再删 task 文档
        await TaskService._cleanup_task_artifacts(task_id, delete_workspace=True)

        result = await TaskService._collection().delete_one({"_id": task_id})
        return result.deleted_count > 0

    # ------------------------------------------------------------------
    # Concurrency control
    # ------------------------------------------------------------------

    @staticmethod
    async def _check_concurrency_limits(created_by: str, source: str = "manual") -> None:
        """Check global and per-user concurrency limits.

        Trigger-sourced tasks skip the per-user limit (they run at a
        scheduled time and must not be blocked by manual tasks).

        Raises:
            ConflictError: If either limit is exceeded.
        """
        db = get_database()
        col = db[TaskService.COLLECTION]

        # Global limit
        global_running = await col.count_documents(
            {"status": TaskStatus.RUNNING.value}
        )
        if global_running >= settings.TASK_GLOBAL_MAX_RUNNING:
            raise ConflictError(
                code="TASK_GLOBAL_CONCURRENCY_LIMIT",
                message=(
                    f"全局并发达到上限 ({settings.TASK_GLOBAL_MAX_RUNNING})，"
                    f"当前运行中: {global_running}"
                ),
                details={
                    "limit": settings.TASK_GLOBAL_MAX_RUNNING,
                    "current": global_running,
                    "type": "global",
                },
            )

        # Per-user limit — skip for trigger-sourced execution snapshots
        if source != "trigger_scheduled" and created_by and created_by not in ("system", "agent"):
            user_running = await col.count_documents(
                {"status": TaskStatus.RUNNING.value, "created_by": created_by}
            )
            if user_running >= settings.TASK_USER_MAX_RUNNING:
                raise ConflictError(
                    code="TASK_USER_CONCURRENCY_LIMIT",
                    message=(
                        f"用户 {created_by} 并发达到上限 ({settings.TASK_USER_MAX_RUNNING})，"
                        f"当前运行中: {user_running}"
                    ),
                    details={
                        "limit": settings.TASK_USER_MAX_RUNNING,
                        "current": user_running,
                        "user_id": created_by,
                        "type": "user",
                    },
                )

    @staticmethod
    async def _schedule_next_pending() -> dict | None:
        """Find and auto-start the oldest pending Task (FIFO scheduling).

        Called after a running Task transitions to a terminal state.
        Uses ``findOneAndUpdate`` with a status filter to avoid races.

        Returns:
            The started Task document, or ``None`` if no pending Task.
        """
        col = TaskService._collection()

        # Find the oldest pending manual Task and atomically claim it.
        # Trigger-sourced execution snapshots are excluded — they are started
        # directly by Celery (execute_scheduled_workflow), not by this FIFO.
        now = utc_now()
        pending = await col.find_one_and_update(
            {"status": TaskStatus.PENDING.value, "source": {"$ne": "trigger_scheduled"}},
            {
                "$set": {
                    "status": TaskStatus.RUNNING.value,
                    "updated_at": now,
                    "version": 1,  # fresh task, version starts at 1
                },
                "$push": {
                    "timeline": TimelineEvent(
                        timestamp=now,
                        event_type="auto_scheduled",
                        data={"reason": "FIFO schedule — previous task completed"},
                        actor="system",
                    ).model_dump()
                },
            },
            sort=[("created_at", 1)],  # oldest first
            return_document=ReturnDocument.AFTER,
        )

        if pending is not None:
            logger.info(
                "task_auto_scheduled",
                task_id=pending["_id"],
                created_by=pending.get("created_by", ""),
            )

            # Write audit log
            await TaskService._write_audit_log(
                task_id=pending["_id"],
                event_type="state_change",
                from_status=TaskStatus.PENDING.value,
                to_status=TaskStatus.RUNNING.value,
                triggered_by="system",
                triggered_by_type="system",
                version=1,
                details={"reason": "FIFO auto-schedule"},
            )

        return pending

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    @staticmethod
    async def transition_task(
        task_id: str,
        to_status: TaskStatus,
        triggered_by: str = "system",
        triggered_by_type: str = "system",
        timeline_event_type: str | None = None,
        timeline_data: dict[str, Any] | None = None,
        error_info: dict[str, Any] | None = None,
    ) -> dict:
        """Transition a Task to a new status with optimistic locking.

        Concurrency control:
        - ``pending → running``: Checks global + per-user limits before
          allowing the transition.  Raises ``ConflictError`` if exceeded.
        - ``running → completed|failed|cancelled``: Triggers FIFO scheduling
          that auto-starts the oldest pending Task.

        Uses MongoDB ``findOneAndUpdate`` with version check for atomicity.

        Args:
            task_id: Task ID.
            to_status: Target status.
            triggered_by: Who/what triggered this transition.
            triggered_by_type: "user", "agent", or "system".
            timeline_event_type: Optional timeline event type (defaults to status value).
            timeline_data: Optional data to attach to timeline event.
            error_info: Optional error info (for failed transitions).

        Returns:
            Updated Task MongoDB document.

        Raises:
            NotFoundError: If Task not found.
            ConflictError: If transition is invalid, version conflict, or
                concurrency limit exceeded.
        """
        # Fetch current state
        doc = await TaskService.get_task_or_404(task_id)
        from_status = TaskStatus(doc["status"])
        current_version = doc.get("version", 1)

        logger.debug(
            "task_transition_start",
            task_id=task_id,
            from_status=from_status.value,
            to_status=to_status.value,
            current_version=current_version,
            triggered_by=triggered_by,
        )

        # Validate transition
        if not is_valid_transition(from_status, to_status):
            raise ConflictError(
                code="TASK_INVALID_TRANSITION",
                message=f"Task {task_id} 不允许从 {from_status.value} 转换到 {to_status.value}",
                details={
                    "task_id": task_id,
                    "from_status": from_status.value,
                    "to_status": to_status.value,
                },
            )

        # Concurrency guard: entering RUNNING state consumes a concurrency slot.
        # Covers initial start (PENDING→RUNNING), retry (FAILED→RUNNING),
        # and resume (CANCELLED→RUNNING).
        if to_status == TaskStatus.RUNNING and from_status in (
            TaskStatus.PENDING,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        ):
            created_by = doc.get("created_by", "")
            task_source = doc.get("source", "manual")
            await TaskService._check_concurrency_limits(created_by, task_source)

        # Build update
        now = utc_now()
        event_type = timeline_event_type or to_status.value

        timeline_entry = TimelineEvent(
            timestamp=now,
            event_type=event_type,
            data=timeline_data or {},
            actor=triggered_by,
        )

        update: dict[str, Any] = {
            "$set": {
                "status": to_status.value,
                "updated_at": now,
                "version": current_version + 1,
            },
            "$push": {"timeline": timeline_entry.model_dump()},
        }

        if error_info:
            task_error = TaskError(**error_info)
            update["$set"]["error"] = task_error.model_dump()

        # Atomic update with optimistic locking
        updated = await TaskService._collection().find_one_and_update(
            {"_id": task_id, "version": current_version},
            update,
            return_document=ReturnDocument.AFTER,
        )

        if updated is None:
            raise ConflictError(
                code="TASK_VERSION_CONFLICT",
                message="Task 状态已变更，请重新获取最新状态后重试",
                details={"task_id": task_id},
            )

        # Write audit log
        await TaskService._write_audit_log(
            task_id=task_id,
            event_type="state_change",
            from_status=from_status.value,
            to_status=to_status.value,
            triggered_by=triggered_by,
            triggered_by_type=triggered_by_type,
            version=current_version + 1,
            details=timeline_data or {},
        )

        logger.info(
            "task_transition",
            task_id=task_id,
            from_status=from_status.value,
            to_status=to_status.value,
            version=current_version + 1,
        )

        # FIFO scheduling: running → terminal
        if from_status == TaskStatus.RUNNING and to_status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            scheduled = await TaskService._schedule_next_pending()
            if scheduled:
                logger.info(
                    "task_fifo_scheduled",
                    new_task_id=scheduled["_id"],
                    triggered_by=task_id,
                )

        # Publish transition event
        event_bus = get_event_bus()
        await event_bus.publish(TaskEvent(
            event_type=f"task.{to_status.value}",
            task_id=task_id,
            from_status=from_status.value,
            to_status=to_status.value,
            data=timeline_data or {},
        ))

        # Also publish to Redis so Celery worker events reach FastAPI via pub/sub
        from app.services.event_bridge import publish_task_event_to_redis

        await publish_task_event_to_redis(
            event_type=f"task.{to_status.value}",
            task_id=task_id,
            from_status=from_status.value,
            to_status=to_status.value,
            data=timeline_data or {},
        )

        # Fire webhook events for terminal/waiting_human states
        if to_status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.WAITING_HUMAN,
        ):
            _fire_task_webhook(task_id, to_status, updated)

        return updated

    # ------------------------------------------------------------------
    # Timeline & variables
    # ------------------------------------------------------------------

    @staticmethod
    async def append_timeline_event(
        task_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> dict:
        """Append a timeline event to a Task."""
        entry = TimelineEvent(
            event_type=event_type,
            data=data or {},
            actor=actor,
        )

        updated = await TaskService._collection().find_one_and_update(
            {"_id": task_id},
            {
                "$push": {"timeline": entry.model_dump()},
                "$set": {"updated_at": utc_now()},
            },
            return_document=ReturnDocument.AFTER,
        )

        if updated is None:
            raise NotFoundError(
                code="TASK_NOT_FOUND",
                message=f"Task {task_id} 不存在",
                details={"task_id": task_id},
            )
        return updated

    @staticmethod
    async def update_variables(
        task_id: str,
        variables: dict[str, Any],
        version: int,
        reason: str | None = None,
        triggered_by: str = "system",
    ) -> dict:
        """Update Task variables with optimistic locking.

        Args:
            task_id: Task ID.
            variables: Variables to merge into existing variable pool.
            version: Expected current version for optimistic locking.
            reason: Optional reason for the variable update.
            triggered_by: Who triggered this update.

        Returns:
            Updated Task MongoDB document.

        Raises:
            ConflictError: If version mismatch.
        """
        now = utc_now()
        new_version = version + 1

        # Build a snapshot of the change
        snapshot = {
            "timestamp": now,
            "variables": variables,
            "reason": reason or "",
            "triggered_by": triggered_by,
        }

        update: dict[str, Any] = {
            "$set": {
                "updated_at": now,
                "version": new_version,
            },
            "$push": {"variable_snapshots": snapshot},
        }

        # Merge variables using $each for top-level keys or $set individual fields
        for key, value in variables.items():
            update["$set"][f"variables.{key}"] = value

        updated = await TaskService._collection().find_one_and_update(
            {"_id": task_id, "version": version},
            update,
            return_document=ReturnDocument.AFTER,
        )

        if updated is None:
            raise ConflictError(
                code="TASK_VERSION_CONFLICT",
                message="Task 状态已变更，请重新获取最新状态后重试",
                details={"task_id": task_id},
            )

        # Sync checkpoint.variable_snapshot so resume_from_checkpoint
        # picks up the latest variable values (e.g. human decision data).
        # This avoids the bug where resume loads the stale snapshot saved
        # when the human node first paused.
        updated_vars = updated.get("variables") or {}
        checkpoint_sync: dict[str, Any] = {}
        for _key in variables:
            if _key in updated_vars:
                checkpoint_sync[f"checkpoint.variable_snapshot.{_key}"] = updated_vars[_key]
        if checkpoint_sync:
            await TaskService._collection().update_one(
                {"_id": task_id},
                {"$set": checkpoint_sync},
            )

        return updated

    @staticmethod
    def _compute_downstream_nodes(workflow_doc: dict, target_node_id: str) -> set[str]:
        """Return all downstream node IDs of ``target_node_id`` (excluding target itself).

        Traverses ``node.config.next_nodes`` via DFS (traversal order is
        irrelevant for transitive closure).

        LIMITATION: only ``config.next_nodes`` is traversed. Gateway and
        parallel nodes store their routing targets elsewhere
        (``config.conditions[].target``/``config.default_branch`` for gateway,
        ``config.branches[].start_node`` for parallel) and
        ``_migrate_edges_to_next_nodes`` (engine.py) never back-fills
        ``next_nodes`` for those node types. As a result the downstream of a
        gateway/parallel node — or of any node whose path crosses one — is
        under-computed: those branches are NOT trimmed from
        ``completed_nodes`` and on resume are silently skipped rather than
        re-executed. This is an accepted V1 non-goal (rewind spec §1.2).
        Rewind across gateway/parallel should therefore be used with care.

        Defends against cycles (though the workflow validator forbids them)
        via a ``visited`` set, and skips next-targets that are not defined as
        nodes in the workflow.

        Args:
            workflow_doc: Raw workflow MongoDB document (must contain ``nodes``).
            target_node_id: Node to compute downstream of.

        Returns:
            Set of node IDs reachable from ``target_node_id`` via
            ``config.next_nodes`` (never includes ``target_node_id`` itself).
        """
        node_map = {n["node_id"]: n for n in workflow_doc.get("nodes", [])}
        visited: set[str] = set()
        stack: list[str] = [target_node_id]
        while stack:
            cur = stack.pop()
            node = node_map.get(cur)
            if not node:
                continue
            for nxt in (node.get("config") or {}).get("next_nodes") or []:
                target = nxt.get("target") if isinstance(nxt, dict) else None
                if (
                    target
                    and target not in visited
                    and target in node_map
                ):
                    visited.add(target)
                    stack.append(target)
        return visited - {target_node_id}

    @staticmethod
    async def rewind_task(
        task_id: str,
        target_node_id: str,
        variables: dict[str, Any] | None,
        comment: str | dict[str, Any] | None,
        triggered_by: str,
        version: int,
    ) -> dict:
        """Rewind a WAITING_HUMAN task back to ``target_node_id`` and resume.

        Trims ``target_node_id`` and ALL its downstream nodes from the
        checkpoint's ``completed_nodes`` and ``variable_snapshot``, optionally
        merges ``variables`` into the pool, then atomically transitions
        WAITING_HUMAN → RUNNING (optimistic lock on ``version``) and triggers
        ``resume_task_execution``. The engine then re-executes the target node
        and its whole downstream subgraph (untrimmed nodes are skipped).

        See ``docs/superpowers/specs/2026-07-17-human-node-rewind-design.md`` §6.

        Args:
            task_id: Task ID.
            target_node_id: Node to rewind to (must be in completed_nodes).
            variables: Optional dict to merge into the variable pool.
            comment: Optional audit comment (str or structured dict).
            triggered_by: User/system ID triggering the rewind.
            version: Expected current version for optimistic locking.

        Returns:
            Updated Task MongoDB document.

        Raises:
            ConflictError: Task not WAITING_HUMAN, no checkpoint, or version
                conflict (status changed concurrently).
            ValidationError: target_node_id not provided, not in
                completed_nodes, or equals current paused_at_node.
        """
        db = get_database()

        # ── 1. Load task ──
        task_doc = await db["tasks"].find_one({"_id": task_id})
        if task_doc is None:
            raise NotFoundError(
                code="TASK_NOT_FOUND",
                message=f"任务 {task_id} 不存在",
                details={"task_id": task_id},
            )

        from_status = task_doc.get("status")

        # ── 2. Validate status ──
        if from_status != TaskStatus.WAITING_HUMAN.value:
            raise ConflictError(
                code="TASK_NOT_WAITING_HUMAN",
                message=f"任务当前状态为 {from_status},无法执行 rewind（仅 waiting_human 可退回）",
                details={"task_id": task_id, "status": from_status},
            )

        # ── 3. Validate checkpoint ──
        checkpoint = task_doc.get("checkpoint")
        if not checkpoint:
            raise ConflictError(
                code="TASK_NO_CHECKPOINT",
                message="任务无可回退的执行上下文（checkpoint 不存在）",
                details={"task_id": task_id},
            )

        completed_nodes: list[str] = list(checkpoint.get("completed_nodes", []))
        paused_at_node = checkpoint.get("paused_at_node", "")
        variable_snapshot: dict[str, Any] = dict(checkpoint.get("variable_snapshot", {}))

        # ── 4. Validate target_node_id ──
        if not target_node_id:
            raise ValidationError(
                code="REWIND_NO_TARGET",
                message="rewind 操作必须指定 target_node_id",
                details={"task_id": task_id},
            )
        # Check paused_at_node FIRST to surface the more precise "当前暂停"
        # error when the user targets the paused node itself. (The paused human
        # node IS in completed_nodes — engine.py:767 adds it before the
        # checkpoint is saved at engine.py:806 — so reversing these two checks
        # wouldn't misfire, but reporting "未执行过" for the current pause node
        # would be a confusing message.)
        if target_node_id == paused_at_node:
            raise ValidationError(
                code="REWIND_TARGET_IS_CURRENT",
                message="不能退回到当前暂停的节点",
                details={"task_id": task_id, "target_node_id": target_node_id},
            )
        if target_node_id not in completed_nodes:
            raise ValidationError(
                code="REWIND_TARGET_NOT_EXECUTED",
                message=f"目标节点 {target_node_id} 未执行过，无法回退",
                details={"task_id": task_id, "target_node_id": target_node_id},
            )

        # ── 5. Compute trim set R = {target} ∪ downstream ──
        workflow_doc = await db["workflows"].find_one({"_id": task_doc.get("workflow_id", "")})
        if workflow_doc is None:
            raise NotFoundError(
                code="WORKFLOW_NOT_FOUND",
                message=f"工作流 {task_doc.get('workflow_id', '')} 不存在",
                details={"task_id": task_id},
            )
        downstream = TaskService._compute_downstream_nodes(workflow_doc, target_node_id)
        trim_set = {target_node_id} | downstream

        # ── 6. Compute trimmed state in memory ──
        new_completed = [n for n in completed_nodes if n not in trim_set]
        new_snapshot = {k: v for k, v in variable_snapshot.items() if k not in trim_set}

        now = utc_now()
        new_version = version + 1

        # ── 7. Build single atomic update ──
        set_ops: dict[str, Any] = {
            "status": TaskStatus.RUNNING.value,
            "updated_at": now,
            "version": new_version,
            "checkpoint.paused_at_node": target_node_id,
            "checkpoint.completed_nodes": new_completed,
            "checkpoint.variable_snapshot": new_snapshot,
            "checkpoint.human_context": {},
            # agent_thread_id must already be empty for a human-pause checkpoint;
            # clear defensively in case of legacy data.
            "checkpoint.agent_thread_id": "",
        }

        timeline_entry = TimelineEvent(
            timestamp=now,
            event_type="rewoun",
            data={
                "node_id": target_node_id,
                "rewound_nodes": sorted(trim_set),
                "comment": comment,
                "triggered_by": triggered_by,
            },
            actor=triggered_by,
        ).model_dump(mode="json")

        push_ops: dict[str, Any] = {
            "timeline": timeline_entry,
        }

        # Merge optional variables. IMPORTANT: also merge into the checkpoint
        # variable_snapshot, because resume_from_checkpoint builds its
        # VariablePool exclusively from checkpoint.variable_snapshot
        # (engine.py:458-463) and never reads the top-level task.variables.
        # Without this, overriding an upstream (non-trimmed) node's value
        # would be lost on resume. (Mirrors the sync done in update_variables.)
        if variables:
            overridden_keys: list[str] = []
            for key, value in variables.items():
                set_ops[f"variables.{key}"] = value
                new_snapshot[key] = value
                overridden_keys.append(key)
            set_ops["checkpoint.variable_snapshot"] = new_snapshot  # keep in sync
            timeline_entry["data"]["variables_overridden"] = overridden_keys
            push_ops["variable_snapshots"] = {
                "timestamp": now,
                "variables": variables,
                "reason": f"rewind to {target_node_id}",
                "triggered_by": triggered_by,
            }

        update = {"$set": set_ops, "$push": push_ops}

        # ── 8. Atomic write with optimistic lock ──
        if not is_valid_transition(TaskStatus(from_status), TaskStatus.RUNNING):
            raise ConflictError(
                code="TASK_INVALID_TRANSITION",
                message=f"任务 {task_id} 不允许从 {from_status} 转换到 running",
                details={"task_id": task_id, "from_status": from_status, "to_status": "running"},
            )

        updated = await db["tasks"].find_one_and_update(
            {"_id": task_id, "version": version},
            update,
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            raise ConflictError(
                code="TASK_VERSION_CONFLICT",
                message="任务状态已变更，请重新获取最新状态后重试",
                details={"task_id": task_id},
            )

        # ── 9. Audit log ──
        await TaskService._write_audit_log(
            task_id=task_id,
            event_type="rewind",
            from_status=from_status,
            to_status=TaskStatus.RUNNING.value,
            action="rewind",
            triggered_by=triggered_by,
            triggered_by_type="user",
            version=new_version,
            details={
                "target_node_id": target_node_id,
                "rewound_nodes": sorted(trim_set),
                "variables_provided": bool(variables),
            },
        )

        logger.info(
            "task_rewind",
            task_id=task_id,
            target_node=target_node_id,
            rewound_count=len(trim_set),
            version=new_version,
        )

        # ── 10. Clear LangGraph checkpointer threads for trimmed nodes ──
        # Without this, re-executing a trimmed agent node reuses its stale
        # thread ({task_id}_{node_id}) and the add_messages reducer appends a
        # fresh SystemMessage after the old history, producing non-consecutive
        # system messages → "Received multiple non-consecutive system messages".
        # (retry avoids this via _cleanup_task_artifacts; rewind must do the
        # same, scoped to the trimmed nodes so upstream threads are preserved.)
        await TaskService._clear_checkpointer_threads(task_id, trim_set)

        # ── 11. Resume (fire-and-forget) ──
        TaskService.resume_task_execution(task_id)

        return updated

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    @staticmethod
    async def _write_audit_log(
        task_id: str,
        event_type: str,
        from_status: str | None = None,
        to_status: str | None = None,
        action: str | None = None,
        triggered_by: str = "system",
        triggered_by_type: str = "system",
        version: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append an immutable audit log entry."""
        entry = AuditLog(
            task_id=task_id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            action=action,
            triggered_by=triggered_by,
            triggered_by_type=triggered_by_type,
            version=version,
            details=details or {},
        )
        await TaskService._audit_collection().insert_one(entry.model_dump(by_alias=True))

    @staticmethod
    async def list_audit_logs(
        task_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """List audit log entries for a Task, newest first."""
        cursor = (
            TaskService._audit_collection()
            .find({"task_id": task_id})
            .sort("timestamp", -1)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @staticmethod
    async def get_stats() -> dict[str, Any]:
        """Get concurrency and Task statistics."""
        db = get_database()
        running_count = await db[TaskService.COLLECTION].count_documents(
            {"status": TaskStatus.RUNNING.value}
        )
        pending_count = await db[TaskService.COLLECTION].count_documents(
            {"status": TaskStatus.PENDING.value}
        )

        # Per-user running counts (top 10)
        pipeline = [
            {"$match": {"status": TaskStatus.RUNNING.value}},
            {"$group": {"_id": "$created_by", "running": {"$sum": 1}}},
            {"$sort": {"running": -1}},
            {"$limit": 10},
        ]
        cursor = db[TaskService.COLLECTION].aggregate(pipeline)
        user_stats = await cursor.to_list(length=10)

        return {
            "global_running": running_count,
            "global_pending": pending_count,
            "global_max": settings.TASK_GLOBAL_MAX_RUNNING,
            "user_limit": settings.TASK_USER_MAX_RUNNING,
            "user_stats": [
                {"user_id": u["_id"], "running": u["running"]} for u in user_stats
            ],
        }


# ---------------------------------------------------------------------------
# Webhook event helper
# ---------------------------------------------------------------------------

_TASK_STATUS_TO_WEBHOOK_EVENT = {
    TaskStatus.COMPLETED: "task.completed",
    TaskStatus.FAILED: "task.failed",
    TaskStatus.WAITING_HUMAN: "task.waiting_human",
}


def _fire_task_webhook(task_id: str, to_status: TaskStatus, task_doc: dict) -> None:
    """Fire-and-forget: dispatch webhook event for a task state change."""
    webhook_event = _TASK_STATUS_TO_WEBHOOK_EVENT.get(to_status)
    if not webhook_event:
        return

    payload = {
        "event": webhook_event,
        "task_id": task_id,
        "workflow_id": task_doc.get("workflow_id", ""),
        "status": to_status.value,
        "output": task_doc.get("output"),
        "error": task_doc.get("error"),
        "timestamp": task_doc.get("updated_at", ""),
        "api_key_id": task_doc.get("ext_api_key_id"),
        "callback_url": task_doc.get("ext_callback_url"),
    }

    import asyncio

    async def _dispatch():
        try:
            from app.services.webhook_service import WebhookService

            await WebhookService.dispatch_event(webhook_event, payload)
        except Exception as exc:
            logger.warning("webhook_dispatch_error", task_id=task_id, error=str(exc))

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_dispatch())
    except RuntimeError:
        pass

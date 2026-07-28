# 人工审核节点「退回重跑」(rewind) 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `WAITING_HUMAN` 状态下，支持审核人退回到任意已执行节点 + 可选修改输入值 + 立即重跑整个下游，一次 API 调用完成。

**Architecture:** 新增正交动作 `rewind`：service 层新增 `_compute_downstream_nodes`（DAG 下游 BFS）+ `rewind_task`（编排：校验 → 算下游集 → 裁剪 checkpoint → 单次原子写状态转换+裁剪+变量+rewoun 事件 → resume）。引擎完全不改——清空 `human_context` 后落到 `resume_from_checkpoint` 的「节点边界取消」分支，天然重跑 target 及下游。

**Tech Stack:** Python 3 / FastAPI / Pydantic / MongoDB (motor) / pytest。复用现有 `Checkpoint` / `transition_task` 的乐观锁模式 / `update_variables` 的 merge+快照模式。

**对应 Spec:** `docs/superpowers/specs/2026-07-17-human-node-rewind-design.md`

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `backend/app/services/task_service.py` | 新增 `_compute_downstream_nodes` + `rewind_task` | 修改 |
| `backend/app/schemas/task.py` | `TaskIntervene` 加 `target_node_id`/`variables` 字段 + action 正则加 `rewind` | 修改 |
| `backend/app/api/v1/tasks.py` | intervene 端点加 `rewind` 分支 | 修改 |
| `backend/tests/services/test_task_service_rewind.py` | service 层单测（下游计算 + rewind 编排） | 新建 |
| `backend/tests/api/test_task_intervention.py` | API 层 rewind 用例（追加到现有文件） | 修改 |

引擎层 (`backend/app/engine/workflow/engine.py`) **不改**。

---

## Task 1: 下游节点计算 `_compute_downstream_nodes`

纯函数、无 IO，最先实现。它依赖 `Workflow` 模型，因此先确认其结构。

**Files:**
- Modify: `backend/app/services/task_service.py`（在 `TaskService` 类内新增静态方法）
- Test: `backend/tests/services/test_task_service_rewind.py`（新建）

- [ ] **Step 1: 写失败测试（新建测试文件）**

创建 `backend/tests/services/test_task_service_rewind.py`：

```python
"""Tests for TaskService.rewind_task and _compute_downstream_nodes (human-node-rewind)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.services.task_service import TaskService


def _wf(*nodes):
    """Build a minimal workflow doc with the given nodes (node_id → list of next targets)."""
    return {
        "nodes": [
            {
                "node_id": nid,
                "type": "agent",
                "config": {"next_nodes": [{"target": t} for t in targets]},
            }
            for nid, targets in nodes
        ],
    }


class TestComputeDownstreamNodes:
    def test_linear_chain_returns_all_downstream(self):
        wf = _wf(("start", ["a"]), ("a", ["b"]), ("b", ["human"]), ("human", []))
        result = TaskService._compute_downstream_nodes(wf, "a")
        assert result == {"b", "human"}

    def test_includes_target_neighbors_but_not_target_itself(self):
        """Per spec: returns downstream *excluding* target itself; caller unions target in."""
        wf = _wf(("start", ["a"]), ("a", ["b"]), ("b", []))
        result = TaskService._compute_downstream_nodes(wf, "a")
        assert "a" not in result
        assert result == {"b"}

    def test_parallel_branches_all_collected(self):
        wf = _wf(
            ("a", ["p"]),
            ("p", ["b1", "b2"]),
            ("b1", ["join"]),
            ("b2", ["join"]),
            ("join", ["human"]),
            ("human", []),
        )
        result = TaskService._compute_downstream_nodes(wf, "p")
        assert result == {"b1", "b2", "join", "human"}

    def test_diamond_merges_back(self):
        wf = _wf(
            ("a", ["b", "c"]),
            ("b", ["d"]),
            ("c", ["d"]),
            ("d", ["human"]),
            ("human", []),
        )
        result = TaskService._compute_downstream_nodes(wf, "a")
        assert result == {"b", "c", "d", "human"}

    def test_cycle_does_not_loop_forever(self):
        """Validator forbids cycles, but the function must be defensive."""
        wf = _wf(("a", ["b"]), ("b", ["a"]), ("a2", []))
        result = TaskService._compute_downstream_nodes(wf, "a")
        # Even with a cycle, must terminate; both a's neighbours visited once.
        assert "b" in result

    def test_unknown_next_target_skipped_safely(self):
        wf = _wf(("a", ["ghost"]), ("a2", []))  # 'ghost' node not defined
        result = TaskService._compute_downstream_nodes(wf, "a")
        assert result == set()

    def test_target_with_no_downstream_returns_empty(self):
        wf = _wf(("a", ["b"]), ("b", []))
        result = TaskService._compute_downstream_nodes(wf, "b")
        assert result == set()

    def test_target_not_in_workflow_returns_empty(self):
        wf = _wf(("a", ["b"]), ("b", []))
        result = TaskService._compute_downstream_nodes(wf, "nonexistent")
        assert result == set()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/services/test_task_service_rewind.py::TestComputeDownstreamNodes -v`
Expected: FAIL — `AttributeError: type object 'TaskService' has no attribute '_compute_downstream_nodes'`

- [ ] **Step 3: 实现 `_compute_downstream_nodes`**

在 `backend/app/services/task_service.py` 的 `TaskService` 类内（建议放在 `update_variables` 方法之后、`_write_audit_log` 之前），新增静态方法：

```python
    @staticmethod
    def _compute_downstream_nodes(workflow_doc: dict, target_node_id: str) -> set[str]:
        """Return all downstream node IDs of ``target_node_id`` (excluding target itself).

        Traverses ``node.config.next_nodes`` via BFS. Does NOT distinguish
        parallel/gateway branches — per the rewind spec, *all* downstream nodes
        (including parallel siblings) are returned so the caller can trim them
        from ``completed_nodes`` and re-execute the whole downstream subgraph.

        Defends against cycles (though the workflow validator forbids them)
        via a ``visited`` set, and skips next-targets that are not defined as
        nodes in the workflow.

        Args:
            workflow_doc: Raw workflow MongoDB document (must contain ``nodes``).
            target_node_id: Node to compute downstream of.

        Returns:
            Set of node IDs reachable from ``target_node_id`` (never includes
            ``target_node_id`` itself).
        """
        node_map = {n["node_id"]: n for n in workflow_doc.get("nodes", [])}
        visited: set[str] = set()
        queue: list[str] = [target_node_id]
        while queue:
            cur = queue.pop()
            node = node_map.get(cur)
            if not node:
                continue
            for nxt in (node.get("config") or {}).get("next_nodes") or []:
                target = nxt.get("target") if isinstance(nxt, dict) else None
                if target and target not in visited:
                    visited.add(target)
                    queue.append(target)
        return visited
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/services/test_task_service_rewind.py::TestComputeDownstreamNodes -v`
Expected: PASS (8 tests)

- [ ] **Step 5: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/services/task_service.py backend/tests/services/test_task_service_rewind.py
git commit -m "feat(rewind): _compute_downstream_nodes DAG 下游 BFS 计算"
```

---

## Task 2: `rewind_task` 编排方法（成功路径 + 变量覆盖）

实现核心编排。这一步只覆盖成功路径和 variables 覆盖，校验失败的用例放 Task 3。

**Files:**
- Modify: `backend/app/services/task_service.py`（紧接 `_compute_downstream_nodes` 之后新增 `rewind_task`）
- Test: `backend/tests/services/test_task_service_rewind.py`（追加 `TestRewindTask` 类）

- [ ] **Step 1: 写失败测试（成功路径 + variables 覆盖）**

在 `backend/tests/services/test_task_service_rewind.py` 末尾追加：

```python
class TestRewindTask:
    """Tests for TaskService.rewind_task orchestration (success + variables)."""

    @pytest.fixture
    def linear_wf_doc(self):
        return {
            "_id": "wf_test",
            "nodes": [
                {"node_id": "start", "type": "start", "config": {"next_nodes": [{"target": "a"}]}},
                {"node_id": "a", "type": "agent", "config": {"next_nodes": [{"target": "human"}]}},
                {"node_id": "human", "type": "human", "config": {"next_nodes": []}},
            ],
        }

    @pytest.fixture
    def waiting_task_doc(self, linear_wf_doc):
        return {
            "_id": "task_1",
            "workflow_id": "wf_test",
            "status": "waiting_human",
            "version": 5,
            "variables": {"start": {"x": 1}, "a": {"out": "v1"}},
            "checkpoint": {
                "paused_at_node": "human",
                "completed_nodes": ["start", "a"],
                "variable_snapshot": {"start": {"x": 1}, "a": {"out": "v1"}},
                "human_context": {"node_id": "human", "title": "审"},
                "agent_thread_id": "",
            },
            "timeline": [],
            "variable_snapshots": [],
        }

    def test_rewind_trims_target_and_downstream_and_reruns(self, linear_wf_doc, waiting_task_doc):
        """Rewind to 'a': completed_nodes loses 'a' (human not in completed), paused→a, human_context cleared."""
        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        find_one_and_update = AsyncMock(return_value={
            **waiting_task_doc,
            "status": "running",
            "version": 6,
            "checkpoint": {
                "paused_at_node": "a",
                "completed_nodes": ["start"],
                "variable_snapshot": {"start": {"x": 1}},
                "human_context": {},
            },
        })
        with (
            patch("app.services.task_service.get_database", AsyncMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, find_one_and_update),
            ))),
            patch.object(TaskService, "_write_audit_log", AsyncMock()),
            patch.object(TaskService, "resume_task_execution") as resume_mock,
        ):
            import asyncio
            result = asyncio.get_event_loop().run_until_complete(
                TaskService.rewind_task(
                    task_id="task_1",
                    target_node_id="a",
                    variables=None,
                    comment="回退重跑",
                    triggered_by="user_1",
                    version=5,
                )
            )

        # checkpoint trimmed: paused_at_node='a', 'a' removed from completed_nodes
        update_call = find_one_and_update.await_args
        set_ops = update_call.kwargs.get("update", update_call.args[-1] if update_call.args else {})["$set"]
        assert set_ops["status"] == "running"
        assert set_ops["version"] == 6
        assert set_ops["checkpoint.paused_at_node"] == "a"
        assert set_ops["checkpoint.completed_nodes"] == ["start"]
        assert set_ops["checkpoint.human_context"] == {}
        # 'a' output removed from variable_snapshot
        assert "a" not in set_ops["checkpoint.variable_snapshot"]
        # rewoun timeline event pushed
        push_ops = update_call.kwargs.get("update", update_call.args[-1] if update_call.args else {})["$push"]
        tl_entry = push_ops["timeline"]
        assert tl_entry["event_type"] == "rewoun"
        assert tl_entry["node_id"] == "a"
        assert "a" in tl_entry["data"]["rewound_nodes"]
        # resume triggered
        assert resume_mock.call_count == 1

    def test_rewind_with_variables_merges_and_snapshots(self, linear_wf_doc, waiting_task_doc):
        """Providing variables merges into pool and pushes a variable_snapshots entry."""
        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        find_one_and_update = AsyncMock(return_value={**waiting_task_doc, "status": "running", "version": 6})
        with (
            patch("app.services.task_service.get_database", AsyncMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, find_one_and_update),
            ))),
            patch.object(TaskService, "_write_audit_log", AsyncMock()),
            patch.object(TaskService, "resume_task_execution"),
        ):
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                TaskService.rewind_task(
                    task_id="task_1",
                    target_node_id="a",
                    variables={"start": {"x": 999}},
                    comment=None,
                    triggered_by="user_1",
                    version=5,
                )
            )

        update_call = find_one_and_update.await_args
        set_ops = update_call.kwargs.get("update", update_call.args[-1] if update_call.args else {})["$set"]
        # variables.start.x overridden to 999
        assert set_ops["variables.start"] == {"x": 999}
        # variable_snapshots pushed
        push_ops = update_call.kwargs.get("update", update_call.args[-1] if update_call.args else {})["$push"]
        snap = push_ops["variable_snapshots"]
        assert snap["reason"] == "rewind to a"
        assert snap["triggered_by"] == "user_1"
        assert snap["variables"] == {"start": {"x": 999}}
        # timeline records overridden keys
        tl_entry = push_ops["timeline"]
        assert "start" in tl_entry["data"]["variables_overridden"]


class _FakeColl:
    """Minimal fake MongoDB collection capturing find_one / find_one_and_update."""
    def __init__(self, find_one, find_one_and_update):
        self.find_one = find_one
        self.find_one_and_update = find_one_and_update
```

> 说明：`_FakeColl` 放在文件底部，供两个测试类共享。`side_effect=[task, workflow]` 对应 `rewind_task` 内两次 `find_one`（先取 task，再取 workflow）。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/services/test_task_service_rewind.py::TestRewindTask -v`
Expected: FAIL — `AttributeError: ... has no attribute 'rewind_task'`

- [ ] **Step 3: 实现 `rewind_task`**

在 `backend/app/services/task_service.py` 的 `TaskService` 类内，紧接 `_compute_downstream_nodes` 之后，新增：

```python
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

        # ── 1. Load task + workflow ──
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
        if target_node_id not in completed_nodes:
            raise ValidationError(
                code="REWIND_TARGET_NOT_EXECUTED",
                message=f"目标节点 {target_node_id} 未执行过，无法回退",
                details={"task_id": task_id, "target_node_id": target_node_id},
            )
        if target_node_id == paused_at_node:
            raise ValidationError(
                code="REWIND_TARGET_IS_CURRENT",
                message="不能退回到当前暂停的节点",
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

        push_ops: dict[str, Any] = {
            "timeline": TimelineEvent(
                timestamp=now,
                event_type="rewoun",
                data={
                    "node_id": target_node_id,
                    "rewound_nodes": sorted(trim_set),
                    "variables_overridden": [],
                    "comment": comment,
                    "triggered_by": triggered_by,
                },
                actor=triggered_by,
            ).model_dump(mode="json"),
        }

        # Merge optional variables
        if variables:
            overridden_keys: list[str] = []
            for key, value in variables.items():
                set_ops[f"variables.{key}"] = value
                overridden_keys.append(key)
            # Patch the timeline event's variables_overridden (already in push_ops)
            push_ops["timeline"]["data"]["variables_overridden"] = overridden_keys
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

        # ── 10. Resume (fire-and-forget) ──
        TaskService.resume_task_execution(task_id)

        return updated
```

> 说明：`NotFoundError` 已在文件顶部导入（`from app.core.errors import ConflictError, NotFoundError, ValidationError`），无需新增 import。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/services/test_task_service_rewind.py -v`
Expected: PASS (10 tests: 8 from Task 1 + 2 from Task 2)

- [ ] **Step 5: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/services/task_service.py backend/tests/services/test_task_service_rewind.py
git commit -m "feat(rewind): rewind_task 编排 (裁剪+原子写+变量覆盖+resume)"
```

---

## Task 3: `rewind_task` 校验失败路径

覆盖 spec §4.1 与 §8 的所有错误分支。纯测试驱动，实现已在 Task 2 完成，这里只补断言。

**Files:**
- Test: `backend/tests/services/test_task_service_rewind.py`（在 `TestRewindTask` 类内追加方法）

- [ ] **Step 1: 写失败测试（追加到 TestRewindTask 类）**

在 `backend/tests/services/test_task_service_rewind.py` 的 `TestRewindTask` 类内追加（放在已有两个方法之后）：

```python
    def test_rewind_rejects_when_not_waiting_human(self, linear_wf_doc):
        import asyncio
        from app.core.errors import ConflictError

        task_doc = {
            "_id": "task_1", "workflow_id": "wf_test", "status": "running",
            "version": 5, "checkpoint": {"paused_at_node": "human", "completed_nodes": ["a"]},
        }
        find_one = AsyncMock(side_effect=[task_doc])
        with (
            patch("app.services.task_service.get_database", AsyncMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ConflictError, match="waiting_human"),
        ):
            asyncio.get_event_loop().run_until_complete(
                TaskService.rewind_task("task_1", "a", None, None, "u", 5)
            )

    def test_rewind_rejects_when_no_checkpoint(self, linear_wf_doc):
        import asyncio
        from app.core.errors import ConflictError

        task_doc = {
            "_id": "task_1", "workflow_id": "wf_test", "status": "waiting_human",
            "version": 5, "checkpoint": None,
        }
        find_one = AsyncMock(side_effect=[task_doc])
        with (
            patch("app.services.task_service.get_database", AsyncMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ConflictError, match="checkpoint"),
        ):
            asyncio.get_event_loop().run_until_complete(
                TaskService.rewind_task("task_1", "a", None, None, "u", 5)
            )

    def test_rewind_rejects_empty_target(self, waiting_task_doc, linear_wf_doc):
        import asyncio
        from app.core.errors import ValidationError

        find_one = AsyncMock(side_effect=[waiting_task_doc])
        with (
            patch("app.services.task_service.get_database", AsyncMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ValidationError, match="target_node_id"),
        ):
            asyncio.get_event_loop().run_until_complete(
                TaskService.rewind_task("task_1", "", None, None, "u", 5)
            )

    def test_rewind_rejects_target_not_executed(self, waiting_task_doc, linear_wf_doc):
        import asyncio
        from app.core.errors import ValidationError

        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        with (
            patch("app.services.task_service.get_database", AsyncMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ValidationError, match="未执行过"),
        ):
            asyncio.get_event_loop().run_until_complete(
                TaskService.rewind_task("task_1", "nonexistent_node", None, None, "u", 5)
            )

    def test_rewind_rejects_target_equals_paused(self, waiting_task_doc, linear_wf_doc):
        """target == paused_at_node (== 'human') → ValidationError."""
        import asyncio
        from app.core.errors import ValidationError

        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        with (
            patch("app.services.task_service.get_database", AsyncMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, AsyncMock()),
            ))),
            pytest.raises(ValidationError, match="当前暂停"),
        ):
            asyncio.get_event_loop().run_until_complete(
                TaskService.rewind_task("task_1", "human", None, None, "u", 5)
            )

    def test_rewind_rejects_on_version_conflict(self, waiting_task_doc, linear_wf_doc):
        """find_one_and_update returns None (version mismatch) → ConflictError."""
        import asyncio
        from app.core.errors import ConflictError

        find_one = AsyncMock(side_effect=[waiting_task_doc, linear_wf_doc])
        find_one_and_update = AsyncMock(return_value=None)  # version mismatch
        with (
            patch("app.services.task_service.get_database", AsyncMock(return_value=AsyncMock(
                __getitem__=lambda self, k: _FakeColl(find_one, find_one_and_update),
            ))),
            patch.object(TaskService, "_write_audit_log", AsyncMock()),
            pytest.raises(ConflictError, match="状态已变更"),
        ):
            asyncio.get_event_loop().run_until_complete(
                TaskService.rewind_task("task_1", "a", None, None, "u", 5)
            )
```

- [ ] **Step 2: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/services/test_task_service_rewind.py -v`
Expected: PASS (16 tests)。校验逻辑已在 Task 2 实现，这些测试应直接通过。

- [ ] **Step 3: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/tests/services/test_task_service_rewind.py
git commit -m "test(rewind): rewind_task 校验失败路径 (非等待/无 checkpoint/目标非法/version 冲突)"
```

---

## Task 4: 扩展 `TaskIntervene` schema

为 API 层做准备。schema 改动是其它 action 也可选地携带这两个字段，向后兼容。

**Files:**
- Modify: `backend/app/schemas/task.py:22-34`
- Test: `backend/tests/api/test_task_intervention.py`（追加 schema 校验测试）

- [ ] **Step 1: 写失败测试（追加到 test_task_intervention.py 末尾）**

在 `backend/tests/api/test_task_intervention.py` 末尾追加：

```python
def test_task_intervene_schema_accepts_rewind_action() -> None:
    """Schema must accept action='rewind' and optional target_node_id/variables."""
    from app.schemas.task import TaskIntervene

    body = TaskIntervene(
        action="rewind",
        version=3,
        target_node_id="node_a",
        variables={"input": {"q": "hi"}},
    )
    assert body.action == "rewind"
    assert body.target_node_id == "node_a"
    assert body.variables == {"input": {"q": "hi"}}


def test_task_intervene_schema_rejects_unknown_action() -> None:
    from pydantic import ValidationError as PydanticValidationError

    import pytest
    with pytest.raises(PydanticValidationError):
        TaskIntervene(action="bogus", version=1)  # noqa: F841


def test_task_intervene_schema_target_and_variables_optional() -> None:
    """For non-rewind actions, target_node_id/variables remain optional."""
    from app.schemas.task import TaskIntervene

    body = TaskIntervene(action="approve", version=1)
    assert body.target_node_id is None
    assert body.variables is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/api/test_task_intervention.py::test_task_intervene_schema_accepts_rewind_action tests/api/test_task_intervention.py::test_task_intervene_schema_target_and_variables_optional -v`
Expected: FAIL — `AttributeError: 'TaskIntervene' object has no attribute 'target_node_id'`（以及 action 正则不匹配 rewind）

- [ ] **Step 3: 修改 schema**

在 `backend/app/schemas/task.py` 的 `TaskIntervene` 类（第 22-34 行）追加两个字段，并更新 action 正则：

将：
```python
class TaskIntervene(BaseModel):
    """Request body for Task intervention."""

    action: str = Field(..., pattern=r"^(approve|reject|skip|retry|pause|resume|cancel|update_variables)$")
    # Deprecated: use comment; reason kept for backward compat
    reason: str | None = None
    # comment 支持三种形态（向后兼容）：
    # - str: 纯文本（老用法）
    # - {"type": "text", "value": "..."}: 文本
    # - {"type": "json", "value": {...}}: 结构化数据，value 原样存入 variables，
    #   下游可用 {{node.comment.field}} 钻取
    comment: str | dict[str, Any] | None = None
    version: int = Field(..., ge=1)
```

改为：
```python
class TaskIntervene(BaseModel):
    """Request body for Task intervention."""

    action: str = Field(
        ...,
        pattern=r"^(approve|reject|skip|retry|pause|resume|cancel|update_variables|rewind)$",
    )
    # Deprecated: use comment; reason kept for backward compat
    reason: str | None = None
    # comment 支持三种形态（向后兼容）：
    # - str: 纯文本（老用法）
    # - {"type": "text", "value": "..."}: 文本
    # - {"type": "json", "value": {...}}: 结构化数据，value 原样存入 variables，
    #   下游可用 {{node.comment.field}} 钻取
    comment: str | dict[str, Any] | None = None
    version: int = Field(..., ge=1)
    # rewind 专用：回退到的目标节点（必须是已执行过的节点）
    target_node_id: str | None = None
    # rewind 可选：覆盖变量池的输入值（merge 语义）
    variables: dict[str, Any] | None = None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/api/test_task_intervention.py -v -k "schema"`
Expected: PASS (3 tests)

- [ ] **Step 5: 回归现有 intervene 测试，确保 schema 改动没破坏 approve/reject/skip**

Run: `cd backend && python -m pytest tests/api/test_task_intervention.py -v`
Expected: PASS（原有所有测试 + 3 个新测试）

- [ ] **Step 6: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/schemas/task.py backend/tests/api/test_task_intervention.py
git commit -m "feat(rewind): TaskIntervene schema 加 rewind action + target_node_id/variables 字段"
```

---

## Task 5: intervene API 接入 rewind 分支

最后一步：在 API 端点把 `rewind` action 接到 `TaskService.rewind_task`。

**Files:**
- Modify: `backend/app/api/v1/tasks.py:263`（valid_actions）+ 在 retry 分支后新增 rewind 分支 + `action_messages`
- Test: `backend/tests/api/test_task_intervention.py`（追加 API 集成测试）

- [ ] **Step 1: 写失败测试（追加到 test_task_intervention.py 末尾）**

在 `backend/tests/api/test_task_intervention.py` 末尾追加：

```python
def test_intervene_rewind_calls_service_and_returns_running(
    auth_token: str, current_user: UserResponse,
) -> None:
    """POST intervene action=rewind delegates to TaskService.rewind_task, returns running."""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        rewound_doc = {**task_doc, "status": "running", "version": task_doc["version"] + 1}
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "rewind_task", AsyncMock(return_value=rewound_doc)) as rewind_mock,
        ):
            status_code, payload = _post_intervene(
                client,
                {
                    "action": "rewind",
                    "target_node_id": "node_a",
                    "variables": {"input": {"q": "new"}},
                    "comment": "回退修改",
                    "version": task_doc["version"],
                },
            )

        assert status_code == 200, payload
        assert payload["status"] == "running"
        assert payload["version"] == task_doc["version"] + 1
        # rewind_task called with right args
        assert rewind_mock.await_count == 1
        kwargs = rewind_mock.await_args.kwargs
        assert kwargs["task_id"] == TASK_ID
        assert kwargs["target_node_id"] == "node_a"
        assert kwargs["variables"] == {"input": {"q": "new"}}
        assert kwargs["triggered_by"] == USER_ID
        assert kwargs["version"] == task_doc["version"]
    finally:
        app.dependency_overrides.clear()


def test_intervene_rewind_propagates_validation_error(
    auth_token: str, current_user: UserResponse,
) -> None:
    """When rewind_task raises ValidationError (422), API returns 422."""
    from app.core.errors import ValidationError as AppValidationError

    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(
                tasks_module.TaskService,
                "rewind_task",
                AsyncMock(side_effect=AppValidationError(
                    code="REWIND_TARGET_NOT_EXECUTED",
                    message="目标节点 node_x 未执行过，无法回退",
                )),
            ),
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "rewind", "target_node_id": "node_x", "version": task_doc["version"]},
            )

        assert status_code == 422
        assert "未执行过" in str(payload)
    finally:
        app.dependency_overrides.clear()


def test_intervene_rewind_without_variables_works(
    auth_token: str, current_user: UserResponse,
) -> None:
    """rewind with no variables field — minimal closed loop (pure rerun)."""
    client, app, tasks_module = _build_client(current_user)
    try:
        task_doc = _make_task_doc()
        rewound_doc = {**task_doc, "status": "running", "version": task_doc["version"] + 1}
        with (
            patch.object(tasks_module.TaskService, "get_task_or_404", AsyncMock(return_value=task_doc)),
            patch.object(tasks_module.TaskService, "rewind_task", AsyncMock(return_value=rewound_doc)) as rewind_mock,
        ):
            status_code, payload = _post_intervene(
                client,
                {"action": "rewind", "target_node_id": "node_a", "version": task_doc["version"]},
            )

        assert status_code == 200, payload
        kwargs = rewind_mock.await_args.kwargs
        assert kwargs["variables"] is None
    finally:
        app.dependency_overrides.clear()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/api/test_task_intervention.py -v -k rewind`
Expected: FAIL — rewind 不在 `valid_actions`，返回 `TASK_INVALID_ACTION` 错误

- [ ] **Step 3: 修改 API 端点**

在 `backend/app/api/v1/tasks.py`：

**(a)** 第 263 行，把 `rewind` 加入 valid_actions：

将：
```python
    valid_actions = {"approve", "reject", "skip", "cancel", "resume", "retry"}
```
改为：
```python
    valid_actions = {"approve", "reject", "skip", "cancel", "resume", "retry", "rewind"}
```

**(b)** 在第 405 行（`elif body.action == "resume":` 分支之前）插入 rewind 分支。找到：

```python
    elif body.action == "resume":
        # Transition waiting_human → running  OR  cancelled → running
```

在其**之前**插入：

```python
    elif body.action == "rewind":
        # Rewind: trim target_node_id + downstream from checkpoint, optionally
        # merge variables, atomically transition waiting_human → running, then
        # resume (engine re-executes target + downstream; untrimmed skipped).
        # WAITING_HUMAN guard is enforced inside rewind_task.
        doc = await TaskService.rewind_task(
            task_id=task_id,
            target_node_id=body.target_node_id or "",
            variables=body.variables,
            comment=body.comment,
            triggered_by=current_user.id,
            version=body.version,
        )

```

**(c)** 第 443-450 行，`action_messages` 字典加 rewind：

将：
```python
    action_messages = {
        "approve": "审批通过",
        "reject": "已驳回",
        "skip": "已跳过",
        "cancel": "已取消",
        "resume": "已恢复",
        "retry": "重试中",
    }
```
改为：
```python
    action_messages = {
        "approve": "审批通过",
        "reject": "已驳回",
        "skip": "已跳过",
        "cancel": "已取消",
        "resume": "已恢复",
        "retry": "重试中",
        "rewind": "已退回重跑",
    }
```

- [ ] **Step 4: 运行新测试确认通过**

Run: `cd backend && python -m pytest tests/api/test_task_intervention.py -v -k rewind`
Expected: PASS (3 tests)

- [ ] **Step 5: 全量回归测试套件**

Run: `cd backend && python -m pytest tests/api/test_task_intervention.py tests/services/test_task_service_rewind.py -v`
Expected: PASS（全部，含原有 approve/reject/skip + 所有 rewind 用例）

- [ ] **Step 6: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add backend/app/api/v1/tasks.py backend/tests/api/test_task_intervention.py
git commit -m "feat(rewind): intervene API 接入 rewind 分支 (退回+改值+重跑)"
```

---

## 验收标准（Definition of Done）

实现完成后，以下全部成立：

1. `cd backend && python -m pytest tests/services/test_task_service_rewind.py tests/api/test_task_intervention.py -v` 全绿
2. `POST /api/v1/tasks/{id}/intervene` 携带 `{"action":"rewind","target_node_id":"...","version":N}` 在 WAITING_HUMAN 时返回 200 + `status=running`
3. `variables` 可选：不传也能工作（纯退回重跑），传了则 merge + 记录 `variable_snapshots`
4. 非 WAITING_HUMAN / 无 checkpoint → 409；target 非法 → 422；version 冲突 → 409
5. timeline 出现 `event_type=rewoun` 事件，payload 含 `rewound_nodes`
6. 引擎 `engine.py` 无任何改动（设计预期，可用 `git diff main -- backend/app/engine/` 确认为空）

## 未覆盖（V1 范围外，符合 spec §1.2）

- RUNNING/COMPLETED 状态下的 rewind
- 外部副作用回滚（重复发消息等）
- 工作流模板执行时快照锁定
- 前端 UI（后续单独迭代）

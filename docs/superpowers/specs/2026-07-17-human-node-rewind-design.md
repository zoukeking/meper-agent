# 人工审核节点「退回重跑」（rewind）设计文档

**日期**: 2026-07-17
**状态**: 设计完成，待实施

## 1. 概述

当前人工审核（`human` 节点）只有两种结局：`approve/skip` 继续向下游、`reject` 直接把整个 task 标记为 `FAILED` 并终止；另有粗粒度的 `retry` 会清空一切、整个 task 从 0 重跑。

本设计为人工审核新增一个正交动作 **`rewind`**：审核人在任务处于 `WAITING_HUMAN` 时，可指定一个**已执行节点**作为回退目标，并可选地覆盖部分变量值；系统裁剪该节点及其所有下游节点，然后立即从目标节点重新执行整个下游子图。

### 1.1 目标
- 在 `WAITING_HUMAN` 状态下，支持「退回到任意已执行节点 + 可选修改输入值 + 立即重跑」一次调用完成
- 复用现有 `Checkpoint` / `transition_task` / `resume_task_execution` / `update_variables` 底座，引擎侧改动最小
- 与现有 `approve`/`reject`/`skip`/`retry` 语义正交，互不冲突
- 完整审计：timeline + variable_snapshots + audit log

### 1.2 非目标（V1 不含）
- 不支持在 `RUNNING`（需先暂停）或 `COMPLETED` 时退回
- 不主动回滚外部副作用（已发出的消息、已写入的文件等保持不变）
- 不做 DAG 路径分析（即不区分「与目标节点在同一路径」的节点）——目标节点之后的**所有**已执行节点（含并行分支）一律重跑
- 不引入中间「待修改」状态，rewind 后立即重跑
- 不锁定执行时的工作流模板快照（沿用现有行为，执行与 rewind 均以当前 task 绑定的 workflow 为准）

## 2. 现状分析

| 主题 | 位置 |
|---|---|
| intervene API | `backend/app/api/v1/tasks.py:241` |
| TaskIntervene schema | `backend/app/schemas/task.py:22` |
| action 白名单 | `tasks.py:263`（内部）、schema 正则 `^(approve\|reject\|skip\|retry\|pause\|resume\|cancel\|update_variables)$` |
| approve/skip 流转 | `tasks.py:285-335`（WAITING_HUMAN→RUNNING，写 decision，resume） |
| reject 流转 | `tasks.py:337-376`（WAITING_HUMAN→FAILED，`error_code=HUMAN_REJECTED`） |
| retry 流转 | `tasks.py:407-441`（清空 output/variables/timeline/checkpoint 后从 0 重跑） |
| Checkpoint 模型 | `backend/app/models/task.py:66-84`（含 `paused_at_node`、`completed_nodes: list[str]`、`variable_snapshot`） |
| Task 状态机 | `backend/app/models/task.py:25`（`TRANSITION_MAP`，已含 `WAITING_HUMAN→RUNNING`） |
| transition_task（乐观锁） | `backend/app/services/task_service.py:549` |
| update_variables（变量覆盖 + 快照） | `backend/app/services/task_service.py:762`、`787-801`（`$push variable_snapshots`） |
| resume_task_execution（fire-and-forget） | `backend/app/services/task_service.py:195` |
| Engine resume_from_checkpoint | `backend/app/engine/workflow/engine.py:369`（按 `completed_nodes` 跳过已执行节点） |
| Engine 节点执行 + next_nodes 路由 | `backend/app/engine/workflow/engine.py:715`、`913-924` |
| Human 节点执行器 | `backend/app/engine/workflow/nodes/human.py:23` |
| Workflow 模板（next_nodes 写在 node.config） | `backend/app/models/workflow.py:28`（`WorkflowNode`）、`65`（`Workflow`） |
| Timeline 事件 | `backend/app/models/task.py:47-53`（`TimelineEvent`） |
| 审计日志 | `backend/app/services/task_service.py:842`（`_write_audit_log`） |

### 2.1 关键结论
- `reject` 固定流向 `FAILED`，没有「退回到指定节点」能力
- `retry` 粒度太粗（整 task 从零开始）
- `Checkpoint.completed_nodes` 是 `list[str]`，`resume_from_checkpoint` 本就按该集合跳过已执行节点；**只要裁剪该集合并把 `paused_at_node` 改为目标节点，引擎就会自动从目标节点开始重跑整个下游**——引擎核心逻辑几乎不动

## 3. 方案选型

| 方案 | 说明 | 取舍 |
|---|---|---|
| **A. 新增 `rewind` action（选定）** | 一次调用完成「退回 + 改值 + 重跑」 | ✅ 语义正交清晰；✅ 一次调用；✅ 引擎几乎不改；⚠️ service 层需做一次 DAG 下游遍历 |
| B. 扩展 `resume` 加 `target_node_id` | 不新增 action，两步：先 `update_variables` 再 `resume(target_node_id)` | ❌ 两步操作、中间状态不清晰；❌ resume 语义变重 |
| C. 让 `reject` 携带退回目标 | reject 时传 target 就退回而非 FAILED | ❌ reject 语义分裂（有时 FAILED 有时 rewind）；❌ approve 想改输入的场景用不上 |

**选定方案 A**。

## 4. API 契约

**端点**：复用 `POST /tasks/{task_id}/intervene`（`backend/app/api/v1/tasks.py:241`）

**请求体**（扩展 `TaskIntervene`，`backend/app/schemas/task.py:22`）：

```python
action: Literal[..., "rewind"]            # 在现有枚举后追加 rewind
target_node_id: str | None = None         # 【新增字段】rewind 时必填
variables: dict[str, Any] | None = None   # 【新增字段】rewind 时可选，覆盖变量池
comment: str | dict[str, Any] | None = None  # 已存在，复用
version: int = Field(..., ge=1)           # 已存在，乐观锁
```

> 说明：当前 `TaskIntervene` 仅有 `action`/`reason`/`comment`/`version` 四个字段（见 `schemas/task.py:22-34`），`target_node_id` 与 `variables` 均为**新增**字段。对其它 action 而言二者可选且被忽略，向后兼容。

### 4.1 rewind 专属校验规则
- `target_node_id` 必填
- task.status 必须 == `WAITING_HUMAN`（否则 409 "任务不在等待人工审核状态"）
- task 必须存在 `checkpoint`（否则 409 "无可回退的执行上下文"）
- `target_node_id` 必须 ∈ `checkpoint.completed_nodes`（否则 422 "目标节点未执行过，无法回退"）
- `target_node_id` 不能 == `checkpoint.paused_at_node`（否则 422 "不能退回到当前暂停的节点"）
- `variables` 若提供，走现有 `update_variables` 的 merge + 快照路径

### 4.2 响应
返回更新后的 task 对象（与现有 approve/skip 响应一致）。

## 5. 数据模型

### 5.1 Checkpoint（`backend/app/models/task.py:66-84`）
**不改字段**，复用：
- `paused_at_node`：rewind 时改写为 `target_node_id`
- `completed_nodes`：rewind 时裁剪，移除 target 及其下游
- `variable_snapshot`：rewind 时移除 target 及下游节点的输出 key

### 5.2 Task 状态机（`backend/app/models/task.py:25 TRANSITION_MAP`）
**不改**。`WAITING_HUMAN → RUNNING` 已存在（approve/skip 已用），rewind 复用同一转换。

### 5.3 Timeline 事件
新增一个事件类型 `rewoun`，沿用现有 `TimelineEvent` 结构（`backend/app/models/task.py:47-53`）：

```python
TimelineEvent(
    event_type="rewoun",
    node_id=target_node_id,
    payload={
        "rewound_nodes": [...],            # 被裁剪的节点 id 列表（target + 下游）
        "variables_overridden": [...],     # 被覆盖的变量 key 列表（仅 key，不存 value）
        "comment": comment,                # 原样透传
        "triggered_by": current_user,
    },
)
```

### 5.4 variable_snapshots
走现有 `update_variables` 的 `$push` 逻辑（`backend/app/services/task_service.py:787-801`），`reason=f"rewind to {target_node_id}"`，`triggered_by=current_user`。

## 6. 执行流程

`POST /tasks/{id}/intervene` 携带 `action=rewind` 时：

```
1. 加载 task（含 checkpoint）
2. 校验 status == WAITING_HUMAN（否则 409）
3. 校验 checkpoint 存在（否则 409）
4. 校验 target_node_id：
   - 必填
   - ∈ checkpoint.completed_nodes（否则 422）
   - ≠ checkpoint.paused_at_node（否则 422）
5. 计算待裁剪节点集 R：
   R = {target_node_id} ∪ _compute_downstream_nodes(workflow, target_node_id)
6. 在内存中计算裁剪结果（不落库）：
   - new_completed = [n for n in completed_nodes if n not in R]
   - new_paused_at = target_node_id
   - new_variable_snapshot = {k:v for k,v in variable_snapshot.items() if k not in R}
   - new_variables = merge(task.variables, variables)   # 若 variables 提供
7. 【原子写】一次性 find_one_and_update，version 校验：
   $set: {
     status: RUNNING,
     version: version + 1,
     "checkpoint.paused_at_node": target_node_id,
     "checkpoint.completed_nodes": new_completed,
     "checkpoint.variable_snapshot": new_variable_snapshot,
     "checkpoint.human_context": {},          # 清空旧 human 上下文
     variables: new_variables,                # 若提供
   },
   $push: {
     timeline: rewoun 事件 (payload: rewound_nodes=R, variables_overridden=keys,
                             comment, triggered_by),
     variable_snapshots: {timestamp, variables, reason=f"rewind to {target}",
                          triggered_by},   # 若 variables 提供
   }
   filter: {_id, version}
   → version 不匹配返回 None → 409 "任务状态已变更"
8. resume_task_execution(task_id)（fire-and-forget 触发 Celery）
   → engine.resume_from_checkpoint 按裁剪后的 completed_nodes 跳过未裁剪节点，
     从 paused_at_node=target 重跑整个下游
```

**关于原子性**：现有 `transition_task`（`task_service.py:549`）的 `$set`/`$push` 是为状态转换定制的固定结构，不会写 checkpoint 裁剪和 variables。因此 `rewind_task` **不复用** `transition_task`，而是自己执行一次带 version 过滤的 `find_one_and_update`，把「状态转换 + checkpoint 裁剪 + 变量覆盖 + rewoun 事件 + 快照」合并成**单次原子写**，避免中间态。但需复用 `transition_task` 里的两段逻辑：
- `is_valid_transition(WAITING_HUMAN, RUNNING)`（`task_service.py:600`）
- `_write_audit_log(...)`（`task_service.py:662`）

**关于并发限额**：`WAITING_HUMAN → RUNNING` 不属于 `(PENDING, CANCELLED, FAILED)` 三元组（`task_service.py:614-618`），不会触发 `_check_concurrency_limits`——task 本就占用着槽位（paused 期间未释放），符合预期，无需改动。

### 6.1 下游计算（新增的唯一新逻辑）
```python
def _compute_downstream_nodes(workflow: Workflow, target_node_id: str) -> set[str]:
    """返回 target 节点的所有下游节点 id（不含 target 本身）。

    基于 workflow 模板的 node.config.next_nodes 做 BFS，
    不区分 parallel/gateway 分支 —— 按需求"下游全部重跑"。
    """
    node_map = {n.node_id: n for n in workflow.nodes}
    visited: set[str] = set()
    queue = [target_node_id]
    while queue:
        cur = queue.pop()
        node = node_map.get(cur)
        if not node:
            continue
        for nxt in (node.config or {}).get("next_nodes", []) or []:
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)
    return visited
```
最终裁剪集 R = `{target_node_id} ∪ visited`。

### 6.2 引擎侧改动
**引擎完全不用改。** 详见 `engine.py:369 resume_from_checkpoint` 的三分支逻辑（`engine.py:429-451`）：

1. `agent_thread_id` 非空 → 中途取消，重跑 paused node
2. `is_human_pause`（`human_context` 非空）→ human 暂停，**标记 paused 为 complete**，恢复下游
3. 两者皆空 → 节点边界取消，**重跑** paused node 及其下游

rewind 在第 7 步清空了 `checkpoint.human_context={}`（同时 `agent_thread_id` 本就为空，因为 human 暂停时不会有 agent thread），因此 resume 必然落到**分支 3**：重跑 `paused_at_node=target`，然后 `_execute_node` 内部按 `next_nodes` 递归执行所有下游（`engine.py:913-924`，含条件路由与 parallel 分叉）。配合 `_execute_node` 第 720 行 `if node_id in self._completed_nodes: return None`，未裁剪的节点自动跳过。

结论：**rewind 只需裁剪 `completed_nodes` + 清空 `human_context` + 改 `paused_at_node=target`，引擎天然完成「重跑 target 及下游、跳过未裁剪节点」**。无需任何引擎代码改动。

## 7. 模块边界与改动清单

### 7.1 `backend/app/services/task_service.py`
- **新增** `_compute_downstream_nodes(workflow, target_node_id) -> set[str]`：见 6.1
- **新增** `rewind_task(task_id, target_node_id, variables, comment, current_user, version) -> dict`：编排第 6 节流程步骤 1-8
  - **不复用** `transition_task`（其 `$set`/`$push` 结构固定），而是自己执行一次带 version 过滤的 `find_one_and_update`，把状态转换 + checkpoint 裁剪 + 变量覆盖 + rewoun 事件 + 快照合并成单次原子写
  - 复用 `is_valid_transition` 做转换合法性校验，复用 `_write_audit_log` 写审计
  - 校验失败抛 `ConflictError`/`ValueError`（409/422），与现有 approve/reject 路径风格一致；API 层负责转 `HTTPException`
  - 返回更新后的 task 文档

### 7.2 `backend/app/api/v1/tasks.py:241`
- `action` 内部白名单（`tasks.py:263`）追加 `"rewind"`
- 新增 `elif action == "rewind":` 分支，调 `task_service.rewind_task(...)`
- 错误响应沿用现有 `HTTPException` 风格

### 7.3 `backend/app/schemas/task.py:22`
- `action` 正则追加 `rewind`：`^(approve|reject|skip|retry|pause|resume|cancel|update_variables|rewind)$`
- 若尚无 `target_node_id` 字段则新增（`str | None = None`）

### 7.4 引擎侧
- 基本不动（见 6.2），仅在实现时按测试结果决定是否做 1 行调整

## 8. 边界情况与错误处理

| 场景 | 处理 |
|---|---|
| target ∈ completed_nodes 但实际是 start 节点 | 允许（等价于"几乎全量重跑"，但保留未裁剪分支的变量池）。比 retry 更精细，语义自洽 |
| target 是 parallel 分支里的某个节点 | 按"下游全部重跑"，裁剪 target 之后所有节点（含其他分支）。符合已确认需求 |
| 目标节点之后再次遇到 human 节点 | 重跑到该 human 节点时自然再次 `WAITING_HUMAN`，审核人可再次干预 |
| variables 覆盖的 key 不属于被裁剪节点 | 仍允许 merge（与现有 `update_variables` 一致），timeline 如实记录 key |
| 并发：task 已被其他人 approve/skip | 乐观锁 version 不匹配 → `transition_task` 返回 None → API 返回 409 "任务状态已变更" |
| 并发：重复 rewind | 第一次成功后 task=RUNNING，第二次因 status≠WAITING_HUMAN 返回 409 |
| task 没有 checkpoint（异常态） | 返回 409 "无可回退的执行上下文" |
| 工作流模板执行期间被重新发布 | target 的 next_nodes 以**当前 task 绑定的 workflow**为准（执行与 rewind 均取同一份，保持一致） |
| `_compute_downstream_nodes` 遇到环（理论上 validator 已禁止） | `visited` 集合天然防环；若遇到未注册的 next_node 则跳过 |

## 9. 测试计划

沿用现有 pytest + fixture 风格（`backend/tests/` 镜像 `backend/app/`）。

### 9.1 `backend/tests/api/test_task_intervention.py`（追加）
- `test_intervene_rewind_to_node_reruns_downstream`：构造 start→A→human，走到 WAITING_HUMAN，rewind 到 A，断言 A 被重跑、变量被覆盖
- `test_intervene_rewind_with_variables_overrides_pool`：传 variables，断言变量池更新 + `variable_snapshots` 多一条
- `test_intervene_rewind_without_variables_just_reruns`：不传 variables，纯退回重跑（最小闭环）
- `test_intervene_rewind_rejects_when_task_not_waiting_human`：状态非 WAITING_HUMAN → 409
- `test_intervene_rewind_rejects_unknown_target_node`：target ∉ completed_nodes → 422
- `test_intervene_rewind_rejects_current_paused_node`：target == paused_at_node → 422
- `test_intervene_rewind_rejects_when_no_checkpoint`：task 无 checkpoint → 409
- `test_intervene_rewind_records_timeline_event`：断言 timeline 多一条 `event_type=rewoun`，payload 含 rewound_nodes

### 9.2 `backend/tests/engine/workflow/test_engine_rewind_resume.py`（新增）
- `test_resume_after_rewind_reruns_removed_nodes`：手工构造裁剪后的 checkpoint，断言引擎从 target 重跑下游、target 本身被重跑
- `test_resume_after_rewind_preserves_unremoved_outputs`：裁剪集合外的节点输出保留、被跳过不重跑

### 9.3 `backend/tests/services/test_task_service_rewind.py`（新增）
- `test_compute_downstream_nodes_traverses_next_nodes`：BFS 正确性，含 parallel 分叉与合流
- `test_compute_downstream_nodes_includes_target_itself`：R 集合包含 target 本身
- `test_compute_downstream_nodes_handles_cycle_safely`：环保护
- `test_rewind_task_trims_completed_nodes_and_pool`：裁剪 completed_nodes + 变量池 key

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 引擎 resume 对 paused_at_node 有特殊跳过逻辑，导致 target 不被重跑 | 实现时用单测锁定；必要时做 1 行调整 |
| 裁剪 completed_nodes 与持久化之间存在竞态 | 「状态转换 + checkpoint 裁剪 + 变量覆盖 + rewoun 事件 + 快照」合并成单次带 version 过滤的 `find_one_and_update` 原子落库 |
| 被裁剪节点的输出从变量池删除后，下游表达式 `{{node.field}}` 在重跑前读到空 | 不存在该问题：resume 立即触发，目标节点会先执行并重新填充其输出，下游表达式在重跑到对应节点时才求值 |
| 重跑产生重复外部副作用（如重复发消息） | V1 明确不处理外部副作用回滚；timeline 记录 rewind 事件供人工核对。后续可按节点提供 `on_rewind` 钩子 |

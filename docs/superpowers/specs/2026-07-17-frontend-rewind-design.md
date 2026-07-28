# 前端人工审核「退回重跑」(rewind) 对接设计文档

**日期**: 2026-07-17
**状态**: 设计完成，待实施

## 1. 概述

后端 rewind API 已就绪（`POST /api/v1/tasks/{id}/intervene` 携带 `action=rewind` + `target_node_id` + 可选 `variables`，见 `docs/superpowers/specs/2026-07-17-human-node-rewind-design.md`）。本设计为 `frontend/`（antd 主后台）对接该能力：在 task 详情面板为 `waiting_human` 状态新增「退回重跑」入口。

项目有两套前端（`frontend/` 用 antd、`frontend-studio/` 用自研 Tailwind 原语），**本次只对接 `frontend/`**，`frontend-studio/` 后续再说。

### 1.1 目标
- 在 `waiting_human` 状态的 task 详情面板提供「退回重跑」按钮
- 弹出 Modal：Select 选目标节点（候选来自 `checkpoint.completed_nodes`，label 来自工作流模板）+ 预览该节点当前输出 + 可折叠的 JSON 变量编辑区
- 一次提交完成「退回 + 可选改值 + 重跑」，复用现有 `intervene` mutation 和 WebSocket 自动刷新

### 1.2 非目标（V1 不含）
- 不改 `frontend-studio/`
- 不在看板卡片上加 rewind 入口（rewind 需选节点+改值，是相对重的操作，只在详情面板暴露）
- 不做基于节点 schema 的结构化变量表单（用 JSON textarea 即可）
- 不做 timeline 上「退回到此」的节点级按钮（用 Modal 内 Select）
- 不引入 i18n（沿用现有中文硬编码）

## 2. 现状分析（frontend/）

| 主题 | 位置 |
|---|---|
| task 详情面板主文件 | `frontend/src/pages/tasks-page.tsx`（~1200 行） |
| intervene mutation | `tasks-page.tsx:244-262`（`interveneMutation`，`mutationFn` 接 `{ taskId, action, version, comment }`） |
| intervene API 封装 | `frontend/src/services/tasks-api.ts:292-298` |
| `TaskIntervenePayload` 类型 | `tasks-api.ts:189-195`（当前仅 `action/reason/comment/version`，**缺 `target_node_id`/`variables`**） |
| waiting_human 操作区（按钮） | `tasks-page.tsx:1069-1109`（继续/批准/驳回） |
| 审批 Modal（text/json 双模 comment） | `tasks-page.tsx:1152-1213`（含 `Segmented` 切换 + JSON parse 校验 line 442-453） |
| 拉工作流模板的先例 | `tasks-page.tsx:287-289`（`useQuery` + `workflowsApi.get(editTask.workflow_id)`） |
| timeline 事件中文标签 map | `tasks-page.tsx:893-907`（`waiting_human/approve/reject/intervene_retry` 等） |
| Checkpoint 类型（含 completed_nodes） | `tasks-api.ts:108-123` |
| WS 自动刷新（invalidate detail） | `frontend/src/hooks/use-task-realtime.ts`（rewind 成功后无需手动 refetch） |
| workflowsApi.get | `frontend/src/services/workflows-api.ts` |

### 2.1 关键结论
- 所需数据前端都已可获取：`taskDetail.checkpoint.completed_nodes: string[]`（候选目标节点）、`taskDetail.variables[nodeId]`（节点输出预览）、`taskDetail.checkpoint.variable_snapshot`（变量编辑预填）。
- 所需 UI 原语都是 antd 现成：`Modal`、`Select`、`Segmented`、`Input.TextArea`、`Button`、`Spin`、`message`、`Empty`。
- 节点 label 需拉工作流模板（`completed_nodes` 只有 node_id），有 `workflowsApi.get` 先例可抄。
- comment 的 text/json 双模切换 + JSON parse 校验模式（line 442-453, 1199-1213）可直接用于 rewind 的 variables 编辑。

## 3. API 契约（对接后端）

后端 `TaskIntervene`（`backend/app/schemas/task.py:22-41`）已支持：
```python
action: "rewind"
target_node_id: str | None   # rewind 必填，必须 ∈ checkpoint.completed_nodes
variables: dict | None        # rewind 可选，merge 语义覆盖变量池
version: int                  # 乐观锁
```

前端需扩展 `TaskIntervenePayload`（`tasks-api.ts:189-195`）对齐：
```ts
export interface TaskIntervenePayload {
  action: string
  /** @deprecated use comment */
  reason?: string
  comment?: CommentValue
  version: number
  target_node_id?: string        // 【新增】rewind 用
  variables?: Record<string, unknown>  // 【新增】rewind 用
}
```

错误响应（后端经 ExceptionMiddleware 转为 JSON envelope）：
- 409 `TASK_NOT_WAITING_HUMAN` / `TASK_NO_CHECKPOINT` / `TASK_VERSION_CONFLICT`
- 422 `REWIND_NO_TARGET` / `REWIND_TARGET_NOT_EXECUTED` / `REWIND_TARGET_IS_CURRENT`

前端 `interveneMutation.onError`（line 256-260）已有 `message.error(msg)` 统一处理，无需特殊改。

## 4. UI 设计

### 4.1 入口按钮
在详情面板 waiting_human 操作区（`tasks-page.tsx:1069-1109`）的按钮组里，新增一枚「退回重跑」按钮：
- 与「继续/批准/驳回」并列，放在最后
- antd `Button`，`type="default"`（中性样式，避免抢批准的 `type="primary"` 紫色主按钮）
- 图标用 `RollbackOutlined`（antd 内置，语义贴合「回退」）
- `loading={interveneMutation.isPending}`
- 仅在 `taskDetail.status === 'waiting_human'` 且存在 `checkpoint` 时渲染

### 4.2 Rewind Modal 结构

```
┌─ 退回重跑 ────────────────────────────────┐
│                                            │
│  退回到节点 *                              │
│  ┌──────────────────────────────────────┐ │
│  │ Select ▾                              │ │  options = completed_nodes
│  │   节点A · start                       │ │  排除 paused_at_node
│  │   节点B · agent                       │ │  label = `${label||id} · ${type}`
│  └──────────────────────────────────────┘ │
│                                            │
│  [选中后] 该节点当前输出：                  │
│  ┌──────────────────────────────────────┐ │
│  │ <pre>{ JSON.stringify(variables[id]) }│ │  只读预览
│  └──────────────────────────────────────┘ │
│                                            │
│  修改变量（可选）                           │
│  [不改] [JSON 编辑]   ← Segmented          │
│                                            │
│  [选 JSON 编辑时]                          │
│  ┌──────────────────────────────────────┐ │
│  │ TextArea (monospace)                  │ │  预填 variable_snapshot
│  │ {"input": {"q": "修改后的值"}}        │ │  的 JSON.stringify(v, null, 2)
│  └──────────────────────────────────────┘ │
│                                            │
│              [取消]  [确定退回]            │
└────────────────────────────────────────────┘
```

### 4.3 交互流程
1. 点「退回重跑」→ 打开 Modal，同时触发拉工作流模板（`useQuery`，`enabled` 于 Modal open 且 task 有 workflow_id）。
2. 模板 loading 期间 Select disabled + `Spin`；完成后构建 `nodeIdToLabel: Record<node_id, {label, type}>`。
3. Select options = `checkpoint.completed_nodes` 过滤掉 `paused_at_node`，每项 label = `${label || node_id} · ${type}`。
4. 选中节点 → 下方 `<pre>` 展示 `JSON.stringify(taskDetail.variables[targetNodeId], null, 2)`（只读预览，确认退回点）。
5. 「修改变量」默认 Segmented = 「不改」。切到「JSON 编辑」时，TextArea 预填 `JSON.stringify(checkpoint.variable_snapshot, null, 2)`，用户编辑。
6. 点「确定退回」：
   - target 必选，否则 `message.warning('请选择退回节点')` 阻断
   - 若 Segmented = 「JSON 编辑」→ `JSON.parse` TextArea（失败 `message.error('JSON 格式错误')` 阻断，成功作为 `variables`）
   - 「不改」→ `variables` 不传（undefined）
   - `interveneMutation.mutate({ taskId, action: 'rewind', target_node_id: target, variables, version: taskDetail.version })`
7. 成功 → `interveneMutation.onSuccess`（已 `message.success` + invalidate detail）+ 关 Modal + 重置 state。WS 会自动刷新详情。
8. 失败 → `onError` 已有 `message.error(msg)`。

### 4.4 timeline 事件标签（小改）
在 timeline 事件中文标签 map（`tasks-page.tsx:893-907`）新增：
```ts
rewoun: { label: '已退回重跑', color: '#F59E0B' },
```
让退回操作后时间线正确显示中文标签（橙色，区别于绿色的 approve）。后端 interven 成功消息也是「已退回重跑」（`backend/app/api/v1/tasks.py:466`），用词一致。

## 5. 模块边界与改动清单

### 5.1 `frontend/src/services/tasks-api.ts`（类型扩展）
- `TaskIntervenePayload`（line 189-195）加 `target_node_id?: string` 和 `variables?: Record<string, unknown>`
- intervene API 封装（line 292）无需改（透传 `data`）

### 5.2 `frontend/src/pages/tasks-page.tsx`（核心改动）
- **interveneMutation**（line 244-246）：`mutationFn` 的参数类型加 `target_node_id?: string`、`variables?: Record<string, unknown>`，透传给 `tasksApi.intervene`
- **新增 state**：`rewindModalOpen: boolean`、`rewindTargetNode: string`、`rewindVarsMode: 'none' | 'json'`、`rewindVarsText: string`
- **新增 useQuery** 拉工作流模板（抄 line 287-289 模式）：
  ```ts
  const rewindWorkflowQuery = useQuery({
    queryKey: ['workflow-detail-for-rewind', rewindModalOpen ? detailTaskId : ''],
    queryFn: () => workflowsApi.get(taskDetail!.workflow_id),
    enabled: rewindModalOpen && !!taskDetail?.workflow_id,
  })
  ```
  构建 `nodeIdToLabel` map（从 `rewindWorkflowQuery.data?.nodes`）。
- **新增 handleRewind** handler：
  ```ts
  const handleRewind = useCallback(async () => {
    if (!rewindTargetNode) { message.warning('请选择退回节点'); return }
    let variables: Record<string, unknown> | undefined
    if (rewindVarsMode === 'json') {
      try { variables = JSON.parse(rewindVarsText) }
      catch { message.error('JSON 格式错误'); return }
    }
    interveneMutation.mutate({
      taskId: taskDetail!.id, action: 'rewind',
      target_node_id: rewindTargetNode, variables,
      version: taskDetail!.version,
    })
    setRewindModalOpen(false)
  }, [rewindTargetNode, rewindVarsMode, rewindVarsText, taskDetail, interveneMutation])
  ```
- **新增「退回重跑」按钮**（line 1069-1109 waiting_human 按钮组内，末尾）：
  ```tsx
  <Button icon={<RollbackOutlined />} onClick={() => { setRewindTargetNode(''); setRewindVarsMode('none'); setRewindModalOpen(true) }} loading={interveneMutation.isPending}>
    退回重跑
  </Button>
  ```
- **新增 Rewind Modal**（抄 line 1152 审批 Modal 结构，放在 Approval Modal 之后）：含 Select（节点）+ 预览 `<pre>` + Segmented（不改/JSON）+ TextArea（JSON）+ 确定按钮调 handleRewind
- **timeline 标签 map**（line 893-907）加 `rewoun: { label: '已退回重跑', color: '#F59E0B' }`

## 6. 边界情况

| 场景 | 处理 |
|---|---|
| `completed_nodes` 为空（异常态） | Select 无选项，显示 antd `Empty`「无可回退的节点」，确定按钮 disabled |
| workflow 模板拉取失败/超时 | Select 显示 node_id 作为 label（降级），`Spin` 消失，不阻断 |
| target 选中但 task 已被别人 approve | 后端返回 409，`message.error` 提示，WS 刷新后状态变化，Modal 已关 |
| JSON 编辑但变量 key 与被裁剪节点无关 | 后端仍接受（merge 语义），前端不额外校验 |
| variable_snapshot 含 system 等系统变量 | 预填时保留原样，用户自行决定是否改（与后端 merge 一致） |
| task 非 waiting_human | 不渲染「退回重跑」按钮（条件渲染） |
| variables JSON 编辑后清空成 `{}` | 后端 `if variables:` 判空，`{}` 视为不改（与「不改」等价），符合预期 |

## 7. 测试计划

前端无现成单测框架（grep 未发现 `*.test.tsx` / vitest / jest 配置），采用**手动验证清单**：

1. waiting_human task 详情面板出现「退回重跑」按钮，其它状态不出现
2. 点按钮弹 Modal，Select 列出 completed_nodes（排除当前 human 节点），label 为节点名
3. 选节点后下方预览该节点当前输出 JSON
4. 「不改」模式提交 → 成功退回重跑，timeline 出现「已退回重跑」橙色标签
5. 「JSON 编辑」模式：预填当前 snapshot，改成新值提交 → 成功，重跑后变量生效
6. JSON 格式错误 → `message.error` 阻断提交
7. 未选节点 → `message.warning` 阻断
8. task 被 approve 后再点 rewind → 后端 409 → `message.error` 提示
9. completed_nodes 为空 → Select 空 + Empty 提示
10. 非等待状态（running/completed）→ 按钮不显示

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| `tasks-page.tsx` 已 ~1200 行，再加 rewind 会更大 | rewind 相关 state/handler/Modal 集中在一处，注释分隔；若后续继续膨胀可抽 `RewindModal` 子组件（V1 暂不抽，保持单文件改动聚焦） |
| workflow 模板请求延迟影响 Modal 体验 | Select disabled + Spin，且有 `queryKey` 缓存（react-query 默认 cache），重复打开不重复请求 |
| variables JSON 编辑易出错 | 预填当前 snapshot 作为起点（而非空白），提交前 JSON.parse 校验 + 明确错误提示 |
| 用户误退回到错误节点 | 选中后预览节点当前输出 + 确定按钮文案「确定退回」（非「确定」），强化确认感 |

# 前端人工审核「退回重跑」(rewind) 对接实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `frontend/`（antd）task 详情面板为 `waiting_human` 状态新增「退回重跑」入口，对接后端 `intervene(action=rewind)` API。

**Architecture:** 详情面板 Drawer 的 waiting_human 操作区加「退回重跑」按钮，点击弹出 Modal（antd Select 选目标节点 + 预览节点输出 + 可折叠 JSON 变量编辑）。复用现有 `interveneMutation`（扩 2 字段）、审批 Modal 的 Segmented + JSON parse 模式、WS 自动刷新。改动集中在 `tasks-page.tsx` 单文件 + `tasks-api.ts` 类型扩展。

**Tech Stack:** React 19 + antd v6 + @tanstack/react-query 5 + TypeScript + Vite。

**对应 Spec:** `docs/superpowers/specs/2026-07-17-frontend-rewind-design.md`

**测试说明:** 前端无单测框架（无 vitest/jest 配置）。本计划用「改动 + 构建验证（`npm run build` / `tsc --noEmit`）+ 手动验证点」结构，不写自动化测试。每个任务结束跑 `npm run build` 确保类型和编译通过。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `frontend/src/services/tasks-api.ts` | `TaskIntervenePayload` 类型加 `target_node_id`/`variables` 字段 | 修改 |
| `frontend/src/pages/tasks-page.tsx` | interveneMutation 扩字段 + rewind state + useQuery 拉模板 + handleRewind + 按钮 + Rewind Modal + timeline 标签 | 修改 |

---

## Task 1: 扩展 `TaskIntervenePayload` 类型

为后续 mutation/API 调用做准备。纯类型改动，最先做。

**Files:**
- Modify: `frontend/src/services/tasks-api.ts:189-195`

- [ ] **Step 1: 修改 `TaskIntervenePayload` 接口**

打开 `frontend/src/services/tasks-api.ts`，找到 `TaskIntervenePayload`（约 line 189-195），当前内容：

```ts
export interface TaskIntervenePayload {
  action: string
  /** @deprecated use comment */
  reason?: string
  comment?: CommentValue
  version: number
}
```

改为（追加两个可选字段）：

```ts
export interface TaskIntervenePayload {
  action: string
  /** @deprecated use comment */
  reason?: string
  comment?: CommentValue
  version: number
  /** rewind 专用：回退到的目标节点（必须是已执行过的节点） */
  target_node_id?: string
  /** rewind 可选：覆盖变量池的输入值（merge 语义） */
  variables?: Record<string, unknown>
}
```

- [ ] **Step 2: 类型检查通过**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npx tsc --noEmit 2>&1 | tail -5`
Expected: 无错误输出（或仅已有的无关 warning）。新加的两个可选字段向后兼容，不会破坏现有 intervene 调用。

- [ ] **Step 3: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/services/tasks-api.ts
git commit -m "feat(rewind-fe): TaskIntervenePayload 加 target_node_id/variables 字段"
```

---

## Task 2: 扩展 `interveneMutation` 透传 rewind 字段

让 mutation 能接收并透传 `target_node_id`/`variables`。

**Files:**
- Modify: `frontend/src/pages/tasks-page.tsx:244-247`

- [ ] **Step 1: 修改 `interveneMutation` 的 `mutationFn` 参数类型与透传**

打开 `frontend/src/pages/tasks-page.tsx`，找到 `interveneMutation`（约 line 244-247），当前内容：

```ts
  const interveneMutation = useMutation({
    mutationFn: ({ taskId, action, version, comment }: { taskId: string; action: string; version: number; comment?: CommentValue }) =>
      tasksApi.intervene(taskId, { action, version, comment }),
```

改为（参数类型加 `target_node_id`/`variables`，透传到 `tasksApi.intervene`）：

```ts
  const interveneMutation = useMutation({
    mutationFn: ({ taskId, action, version, comment, target_node_id, variables }: { taskId: string; action: string; version: number; comment?: CommentValue; target_node_id?: string; variables?: Record<string, unknown> }) =>
      tasksApi.intervene(taskId, { action, version, comment, target_node_id, variables }),
```

（只改 `mutationFn` 这一行，`onSuccess`/`onError` 不动。）

- [ ] **Step 2: 类型检查通过**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npx tsc --noEmit 2>&1 | tail -5`
Expected: 无错误。`tasksApi.intervene` 的参数类型 `TaskIntervenePayload` 已在 Task 1 加了这两个字段。

- [ ] **Step 3: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/pages/tasks-page.tsx
git commit -m "feat(rewind-fe): interveneMutation 透传 target_node_id/variables"
```

---

## Task 3: 新增 rewind 相关 state + 拉工作流模板的 useQuery

为 Modal 准备数据。这一步加 state 和查询，还不加 UI。

**Files:**
- Modify: `frontend/src/pages/tasks-page.tsx`（state 区约 line 100 附近，useQuery 区约 line 287 附近）

- [ ] **Step 1: 新增 rewind state**

在 `tasks-page.tsx` 找到现有 approval state（约 line 100-102）：

```ts
  const [approvalComment, setApprovalComment] = useState('')

  const [approvalCommentMode, setApprovalCommentMode] = useState<'text' | 'json'>('text')
```

在其**下方**新增 rewind state：

```ts
  /* ─── Rewind state（退回重跑 Modal）─── */
  const [rewindModalOpen, setRewindModalOpen] = useState(false)
  const [rewindTargetNode, setRewindTargetNode] = useState<string>('')
  // 变量编辑模式：'none' = 不改变量(纯退回), 'json' = JSON 编辑
  const [rewindVarsMode, setRewindVarsMode] = useState<'none' | 'json'>('none')
  const [rewindVarsText, setRewindVarsText] = useState<string>('')
```

- [ ] **Step 2: 新增拉工作流模板的 useQuery（构建节点名映射）**

找到现有 edit-task 的 workflow 查询（约 line 287-289）：

```ts
  const { data: editWorkflowData } = useQuery({
    queryKey: ['workflow-detail-for-edit', editTask?.workflow_id ?? ''],
    queryFn: () => workflowsApi.get(editTask!.workflow_id),
    enabled: !!editTask?.workflow_id,
  })
```

在其**下方**新增 rewind 用的 workflow 查询：

```ts
  // rewind Modal 打开时拉工作流模板，用于把 completed_nodes 的 node_id 映射成节点名
  const rewindWorkflowQuery = useQuery({
    queryKey: ['workflow-detail-for-rewind', rewindModalOpen && taskDetail ? taskDetail.workflow_id : ''],
    queryFn: () => workflowsApi.get(taskDetail!.workflow_id),
    enabled: rewindModalOpen && !!taskDetail?.workflow_id,
    staleTime: 60_000,
  })
  // node_id → { label, type } 映射，供 Select 显示节点名
  const rewindNodeMap = useMemo(() => {
    const nodes = rewindWorkflowQuery.data?.nodes ?? []
    const m: Record<string, { label: string; type: string }> = {}
    for (const n of nodes) {
      m[n.node_id] = { label: n.label || n.node_id, type: n.type }
    }
    return m
  }, [rewindWorkflowQuery.data])
```

> 说明：`useMemo` 已在文件顶部导入（确认：`tasks-page.tsx` 顶部有 `import { useState, useMemo, useCallback, ... } from 'react'`，若 `useMemo` 未导入则补上）。`workflowsApi` 已在 line 46 导入。`taskDetail` 来自 line 207 的 useQuery。

- [ ] **Step 3: 确认 `useMemo` 已导入**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && grep -n "useMemo" src/pages/tasks-page.tsx | head -3`
Expected: 至少 1 行命中（说明已导入）。若**无**命中，则在顶部 react 导入里补上 `useMemo`：
找到 `import { useState, ... } from 'react'`，把 `useMemo` 加进去。

- [ ] **Step 4: 类型检查通过**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npx tsc --noEmit 2>&1 | tail -5`
Expected: 无错误。新 state 和 useQuery 暂未被消费，但类型应正确。

- [ ] **Step 5: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/pages/tasks-page.tsx
git commit -m "feat(rewind-fe): rewind state + 拉工作流模板 useQuery (节点名映射)"
```

---

## Task 4: 新增 `handleRewind` handler + 打开 Modal 的逻辑

实现提交逻辑（校验 → JSON parse → mutate）和 Modal 打开时的初始化。

**Files:**
- Modify: `frontend/src/pages/tasks-page.tsx`（handler 区，建议放在 `handleApprovalConfirm` 附近）

- [ ] **Step 1: 新增 `openRewindModal` 和 `handleRewind`**

找到现有 approval handler（约 line 426-469 区域，`handleApprovalConfirm` / `buildComment` 附近）。在其**附近**（同级，在 `return (` JSX 之前）新增：

```ts
  /* ─── Rewind handlers ─── */
  const openRewindModal = useCallback(() => {
    setRewindTargetNode('')
    setRewindVarsMode('none')
    // 预填当前 variable_snapshot 作为 JSON 编辑起点
    const snapshot = taskDetail?.checkpoint?.variable_snapshot
    setRewindVarsText(snapshot ? JSON.stringify(snapshot, null, 2) : '{}')
    setRewindModalOpen(true)
  }, [taskDetail])

  const handleRewind = useCallback(() => {
    if (!taskDetail) return
    if (!rewindTargetNode) {
      message.warning('请选择退回节点')
      return
    }
    let variables: Record<string, unknown> | undefined
    if (rewindVarsMode === 'json') {
      try {
        variables = JSON.parse(rewindVarsText)
      } catch {
        message.error('JSON 格式错误，请检查变量输入')
        return
      }
    }
    interveneMutation.mutate({
      taskId: taskDetail.id,
      action: 'rewind',
      target_node_id: rewindTargetNode,
      variables,
      version: taskDetail.version,
    })
    setRewindModalOpen(false)
  }, [taskDetail, rewindTargetNode, rewindVarsMode, rewindVarsText, interveneMutation])
```

> 说明：
> - `message` 已在文件顶部从 antd 导入（line 13）。
> - `openRewindModal` 重置 state + 预填 snapshot + 打开 Modal。`variables` 在 `'none'` 模式下保持 `undefined`（不传），符合后端「纯退回」语义。
> - `handleRewind` 提交后立即关 Modal（乐观关闭）；失败由 `interveneMutation.onError` 的 `message.error` 处理。

- [ ] **Step 2: 类型检查通过**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npx tsc --noEmit 2>&1 | tail -5`
Expected: 无错误。

- [ ] **Step 3: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/pages/tasks-page.tsx
git commit -m "feat(rewind-fe): openRewindModal + handleRewind (校验/JSON parse/mutate)"
```

---

## Task 5: 在详情面板加「退回重跑」按钮

在 waiting_human 操作区暴露入口。

**Files:**
- Modify: `frontend/src/pages/tasks-page.tsx:1069-1125`（waiting_human 按钮组）

- [ ] **Step 1: 确认 `RollbackOutlined` 图标已导入**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && grep -n "RollbackOutlined" src/pages/tasks-page.tsx | head -2`
Expected: 若有命中说明已导入；若**无**命中，则在 antd icons 导入行补上。

找到 `from '@ant-design/icons'` 的导入（约 line 14），确认是否有 `RollbackOutlined`。若无，把它加到该 import 的图标列表里（例如在 `CloseCircleOutlined` 附近加 `RollbackOutlined,`）。

- [ ] **Step 2: 在 waiting_human 按钮组加「退回重跑」按钮**

找到 waiting_human 操作区（约 line 1069-1125），它是一个 IIFE：

```tsx
                {taskDetail.status === 'waiting_human' && (() => {
                  const humanOptions: string[] = ...
                  if (humanOptions.length === 0) {
                    return (
                      <Button type="primary" ...>继续</Button>
                    )
                  }
                  return (
                    <>
                      <Button type="primary" ...>批准</Button>
                      <Button danger ...>驳回</Button>
                    </>
                  )
                })()}
```

这个 IIFE 有两个分支（无选项→继续 / 有选项→批准+驳回）。**退回按钮在两个分支都要出现**（无论有无 human options 都允许退回）。最干净的做法：把整个 IIFE 的返回值包一层 Fragment，在末尾加退回按钮。

把 IIFE 改为（在每个分支的按钮后都加退回按钮，或重构为统一返回）。推荐重构为：先算出「继续/批准+驳回」按钮，再统一附加退回按钮。改法如下——将原 IIFE 替换为：

```tsx
                {taskDetail.status === 'waiting_human' && (() => {
                  const humanOptions: string[] = Array.isArray(taskDetail.checkpoint?.human_context?.options)
                    ? taskDetail.checkpoint.human_context.options.filter(Boolean)
                    : []

                  return (
                    <>
                      {humanOptions.length === 0 ? (
                        // 无选项 → 通用"继续"按钮
                        <Button
                          type="primary"
                          icon={<CheckOutlined />}
                          onClick={() => interveneMutation.mutate({
                            taskId: taskDetail.id,
                            action: 'resume',
                            version: taskDetail.version,
                            comment: '',
                          })}
                          loading={interveneMutation.isPending}
                        >
                          继续
                        </Button>
                      ) : (
                        // 有选项 → 批准/驳回
                        <>
                          <Button
                            type="primary"
                            icon={<CheckOutlined />}
                            onClick={() => handleApprove(taskDetail)}
                            loading={interveneMutation.isPending}
                            style={{ backgroundColor: '#8B5CF6', borderColor: '#8B5CF6' }}
                          >
                            批准
                          </Button>
                          <Button
                            danger
                            icon={<CloseCircleOutlined />}
                            onClick={() => handleReject(taskDetail)}
                            loading={interveneMutation.isPending}
                          >
                            驳回
                          </Button>
                        </>
                      )}
                      {/* 退回重跑：无论有无 human options 都可用 */}
                      <Button
                        icon={<RollbackOutlined />}
                        onClick={openRewindModal}
                        loading={interveneMutation.isPending}
                      >
                        退回重跑
                      </Button>
                    </>
                  )
                })()}
```

> 关键：`RollbackOutlined` 退回按钮放在 IIFE 统一返回的 Fragment 末尾，`type` 不指定（默认 default 样式，避免抢批准的 primary 紫色）。`onClick={openRewindModal}`。

- [ ] **Step 3: 类型检查 + 构建通过**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npx tsc --noEmit 2>&1 | tail -5`
Expected: 无错误。

- [ ] **Step 4: 手动验证点（按钮渲染）**

启动 dev server: `cd /Users/huyuekai/company/agent-flow/frontend && npm run dev`
打开浏览器，找到一个 `waiting_human` 状态的 task，打开详情 Drawer，确认：
- 操作区出现「退回重跑」按钮（RollbackOutlined 图标，位于批准/驳回之后）
- 非 waiting_human 状态的 task 不显示该按钮
- 点击按钮目前无反应（Modal 在 Task 6 加）—— 这一步只验证按钮渲染

- [ ] **Step 5: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/pages/tasks-page.tsx
git commit -m "feat(rewind-fe): 详情面板 waiting_human 加「退回重跑」按钮"
```

---

## Task 6: 新增 Rewind Modal（Select + 预览 + 变量编辑）

核心 UI。放在 Approval Modal 之后，抄其结构。

**Files:**
- Modify: `frontend/src/pages/tasks-page.tsx`（在 Approval Modal 约 line 1222 之后新增）

- [ ] **Step 1: 在 Approval Modal 之后新增 Rewind Modal**

找到 Approval Modal 的结束 `</Modal>`（约 line 1222，紧跟着是 `{/* ─── Edit Scheduled Task Modal ─── */}`）。在其**之后**、Edit Scheduled Task Modal **之前**插入 Rewind Modal：

```tsx
      {/* ─── Rewind Modal（退回重跑）─── */}
      <Modal
        title="退回重跑"
        open={rewindModalOpen}
        onOk={handleRewind}
        onCancel={() => setRewindModalOpen(false)}
        okText="确定退回"
        cancelText="取消"
        confirmLoading={interveneMutation.isPending}
        destroyOnClose
        width={560}
      >
        {(() => {
          if (!taskDetail?.checkpoint) {
            return <div className="text-sm text-[#94A3B8]">无可回退的执行上下文</div>
          }
          const pausedAt = taskDetail.checkpoint.paused_at_node ?? ''
          const completed = (taskDetail.checkpoint.completed_nodes ?? []).filter(n => n !== pausedAt)
          const selectOptions = completed.map(n => {
            const meta = rewindNodeMap[n]
            return { value: n, label: meta ? `${meta.label} · ${meta.type}` : n }
          })
          const wfLoading = rewindWorkflowQuery.isLoading
          const targetOutput = rewindTargetNode ? taskDetail.variables?.[rewindTargetNode] : undefined

          return (
            <div className="flex flex-col gap-3 py-2">
              <p className="text-sm text-[#475569]">
                选择一个已执行的节点，任务将从该节点重新执行其全部下游。当前审批节点（{pausedAt}）不可选。
              </p>

              {/* 目标节点选择 */}
              <div>
                <label className="block text-sm text-[#0F172A] mb-1.5">退回到节点 <span className="text-red-500">*</span></label>
                <Select
                  value={rewindTargetNode || undefined}
                  onChange={(v: string) => setRewindTargetNode(v)}
                  placeholder="选择一个已执行的节点"
                  options={selectOptions}
                  loading={wfLoading}
                  disabled={wfLoading || completed.length === 0}
                  showSearch
                  optionFilterProp="label"
                  style={{ width: '100%' }}
                  notFoundContent={wfLoading ? <Spin size="small" /> : (completed.length === 0 ? <Empty description="无可回退的节点" /> : null)}
                />
              </div>

              {/* 选中节点的当前输出预览（确认退回点）*/}
              {rewindTargetNode && (
                <div>
                  <label className="block text-sm text-[#0F172A] mb-1.5">该节点当前输出</label>
                  <pre className="text-xs font-mono bg-[#F8FAFC] border border-line rounded-lg p-3 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[#475569]">
                    {targetOutput === undefined ? '(无输出)' : JSON.stringify(targetOutput, null, 2)}
                  </pre>
                </div>
              )}

              {/* 可选：修改变量 */}
              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <label className="block text-sm text-[#0F172A]">修改变量（可选）</label>
                  <Segmented
                    size="small"
                    value={rewindVarsMode}
                    onChange={(val) => setRewindVarsMode(val as 'none' | 'json')}
                    options={[
                      { label: '不改', value: 'none' },
                      { label: 'JSON 编辑', value: 'json' },
                    ]}
                  />
                </div>
                {rewindVarsMode === 'json' && (
                  <Input.TextArea
                    value={rewindVarsText}
                    onChange={(e) => setRewindVarsText(e.target.value)}
                    placeholder='{"input": {"q": "修改后的值"}}'
                    rows={6}
                    className="font-mono"
                  />
                )}
                {rewindVarsMode === 'none' && (
                  <div className="text-xs text-[#94A3B8]">不修改变量，仅退回并重跑下游节点。</div>
                )}
              </div>
            </div>
          )
        })()}
      </Modal>
```

> 说明：
> - 用 IIFE 内联计算 `completed`/`selectOptions`/`targetOutput`，与现有 Approval Modal 的内联计算风格一致。
> - `Select` 的 `options` 排除了 `paused_at_node`（后端会拒绝退回当前暂停节点）。
> - `notFoundContent` 处理三种态：模板 loading（Spin）、无候选（Empty）、正常（null）。
> - `optionFilterProp="label"` + `showSearch` 让 Select 支持搜索节点。
> - 变量编辑抄 Approval Modal 的 Segmented + TextArea 模式，JSON 模式 `font-mono`。
> - `Empty`/`Spin` 已在 line 13 从 antd 导入。

- [ ] **Step 2: 确认 `Select` 已从 antd 导入**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && grep -n "Select" src/pages/tasks-page.tsx | head -3`
Expected: 命中（Select 已在 line 13 的 antd import 里）。若**无** `Select` 命中（只有其它含 select 字样的），则在 line 13 的 antd 导入补 `Select,`。

- [ ] **Step 3: 类型检查 + 构建通过**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npx tsc --noEmit 2>&1 | tail -8`
Expected: 无错误。若报 `Select` 的 `onChange` 类型问题，确认 `onChange={(v: string) => setRewindTargetNode(v)}`（antd Select 单选 onChange 参数是 value）。

- [ ] **Step 4: 完整构建验证**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npm run build 2>&1 | tail -8`
Expected: 构建成功（`dist/` 产出，无 TS 错误）。

- [ ] **Step 5: 手动验证点（Modal 完整交互）**

启动 dev server，找一个 `waiting_human` task：
1. 点「退回重跑」→ Modal 弹出
2. Select 列出 completed_nodes（排除当前 human 节点），显示「节点名 · 类型」
3. 选一个节点 → 下方预览该节点当前输出 JSON
4. 「不改」模式 → 点「确定退回」→ 提交成功，Modal 关闭，timeline 出现退回相关变化（Task 7 加标签后会显示中文）
5. 切「JSON 编辑」→ TextArea 预填当前 snapshot，改值 → 提交成功
6. JSON 故意写错 → 点确定 → `message.error('JSON 格式错误...')` 阻断
7. 不选节点直接确定 → `message.warning('请选择退回节点')` 阻断
8. 模板加载中 Select 显示 Spin

- [ ] **Step 6: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/pages/tasks-page.tsx
git commit -m "feat(rewind-fe): Rewind Modal (Select 选目标节点 + 预览输出 + 可折叠 JSON 变量编辑)"
```

---

## Task 7: timeline 事件标签加 `rewoun`

让退回操作在时间线正确显示中文标签。

**Files:**
- Modify: `frontend/src/pages/tasks-page.tsx:893-907`（timeline 事件中文标签 map）

- [ ] **Step 1: 在 timeline 事件标签 map 加 `rewoun`**

找到 timeline 事件中文标签 map（约 line 893-907），它形如：

```ts
                    waiting_human:     { label: '等待审批',   color: '#8B5CF6' },
                    // ...
                    approve:           { label: '审批通过',   color: '#10B981' },
                    // ...
                    intervene_retry:    { label: '人工重试',   color: '#F59E0B' },
```

在该 map 内（`intervene_retry` 附近）新增一行：

```ts
                    rewoun:            { label: '已退回重跑', color: '#F59E0B' },
```

> 用橙色 `#F59E0B`（与 `intervene_retry` 同色，表示「人工干预类」），区别于绿色 approve。文案「已退回重跑」与后端 intervene 成功消息一致（`backend/app/api/v1/tasks.py:466`）。

- [ ] **Step 2: 类型检查通过**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npx tsc --noEmit 2>&1 | tail -5`
Expected: 无错误。

- [ ] **Step 3: 手动验证点（timeline 标签）**

启动 dev server，执行一次 rewind（Task 6 的流程 4 或 5），退回成功后查看该 task 的 timeline：
- 出现一条橙色「已退回重跑」标签的事件（对应后端 timeline 的 `event_type=rewoun`）

- [ ] **Step 4: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/pages/tasks-page.tsx
git commit -m "feat(rewind-fe): timeline 事件标签加 rewoun(已退回重跑)"
```

---

## 验收标准（Definition of Done）

实现完成后，以下全部成立：

1. `cd /Users/huyuekai/company/agent-flow/frontend && npm run build` 构建成功，无 TS 错误
2. `waiting_human` task 详情面板出现「退回重跑」按钮；其它状态不出现
3. 点按钮弹 Modal：Select 列出 completed_nodes（排除当前 human 节点），label 为「节点名 · 类型」
4. 选节点后预览该节点当前输出
5. 「不改」提交 → 成功退回重跑；「JSON 编辑」改值提交 → 成功且变量生效
6. JSON 格式错误 / 未选节点 → message 阻断提交
7. 退回成功后 timeline 出现橙色「已退回重跑」标签
8. 后端 409/422 错误（如任务已被 approve、目标节点非法）→ `message.error` 正确提示

## 未覆盖（V1 范围外，符合 spec §1.2）

- `frontend-studio/` 的 rewind 对接
- 看板卡片上的 rewind 快捷入口
- 基于节点 schema 的结构化变量表单（用 JSON textarea）
- i18n

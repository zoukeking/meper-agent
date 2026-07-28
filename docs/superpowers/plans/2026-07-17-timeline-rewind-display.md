# Timeline rewind 后展示优化实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化 task 详情页 timeline 在 rewind 后的展示：隐藏 node_start 事件，被 rewoun 废弃的旧轮 node_complete/node_failed 标记「已废弃」并禁用其「查看执行详情」按钮。

**Architecture:** 纯前端改动，集中在 `frontend/src/pages/tasks-page.tsx` 的 timeline 渲染段（约 line 911-1039）。改动 A 在 `timeline.filter` 里隐藏 node_start；改动 B 在渲染前用 rewoun 的 `rewound_nodes` 算出「被废弃的旧轮事件下标集合」，渲染时对这些记录置灰 + 加标签 + 禁用详情按钮。不动后端。

**Tech Stack:** React 19 + antd v6 + TypeScript + Vite。

**对应 Spec:** `docs/superpowers/specs/2026-07-17-timeline-rewind-display-design.md`

**测试说明:** 前端无单测框架。每个任务用 `npx tsc --noEmit -p tsconfig.app.json` + `npm run build` 验证类型和编译，附手动验证点。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `frontend/src/pages/tasks-page.tsx` | timeline 过滤逻辑（隐藏 node_start）+ 废弃判定算法 + 渲染（置灰/标签/禁用按钮）+ 导入 Tooltip | 修改 |

---

## Task 1: 隐藏 node_start 事件 + 导入 Tooltip

改动 A（隐藏 node_start）+ 为 Task 2 预先导入 Tooltip（改一处 import，避免 Task 2 再动 import）。

**Files:**
- Modify: `frontend/src/pages/tasks-page.tsx`（antd import 行约 line 13；timeline filter 约 line 926-932）

- [ ] **Step 1: 在 antd import 加 `Tooltip`**

找到 antd 导入行（约 line 13）：
```ts
import { Button, Tag, message, Spin, Modal, Drawer, Input, Empty, Alert, Segmented, DatePicker, Switch, Radio, Divider, Select } from 'antd'
```
在导入列表里加 `Tooltip`（加在末尾即可）：
```ts
import { Button, Tag, message, Spin, Modal, Drawer, Input, Empty, Alert, Segmented, DatePicker, Switch, Radio, Divider, Select, Tooltip } from 'antd'
```

- [ ] **Step 2: 在 timeline filter 里隐藏 node_start**

找到 timeline 过滤逻辑（约 line 926-932）：
```ts
                  const filtered = timeline.filter(e => {
                    // 审批已完成 → 隐藏所有 waiting_human
                    if (hasAnyApproval && e.event_type === 'waiting_human') return false
                    // 人工审批节点的 node_complete 只是引擎恢复信号，审批事件已覆盖
                    if (e.event_type === 'node_complete' && e.data?.node_type === 'human') return false
                    return true
                  })
```

改为（在最前面加一条隐藏 node_start 的规则）：
```ts
                  const filtered = timeline.filter(e => {
                    // 隐藏 node_start：data 仅 node_id/node_type（已拼进标签），折叠块看起来空
                    if (e.event_type === 'node_start') return false
                    // 审批已完成 → 隐藏所有 waiting_human
                    if (hasAnyApproval && e.event_type === 'waiting_human') return false
                    // 人工审批节点的 node_complete 只是引擎恢复信号，审批事件已覆盖
                    if (e.event_type === 'node_complete' && e.data?.node_type === 'human') return false
                    return true
                  })
```

- [ ] **Step 3: 类型检查 + 构建通过**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npx tsc --noEmit -p tsconfig.app.json 2>&1 | tail -5`
Expected: 无错误（`Tooltip` 是 antd 有效导出；filter 改动是纯逻辑）。

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npm run build 2>&1 | tail -5`
Expected: 构建成功。

- [ ] **Step 4: 手动验证点**

启动 `npm run dev`，打开任意 task 详情：
- timeline 里**不再出现「节点开始」/「Agent 节点开始」等 node_start 行**
- 每个节点只显示一条 node_complete（或 node_failed）
- 其它事件（任务创建/审批/rewoun 等）不受影响

- [ ] **Step 5: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/pages/tasks-page.tsx
git commit -m "feat(timeline): 隐藏 node_start 事件 + 导入 Tooltip"
```

---

## Task 2: 废弃旧轮记录标记 + 禁用详情按钮

改动 B：用 rewoun 的 rewound_nodes 算出被废弃的旧轮事件，渲染时置灰 + 加「已废弃」标签 + 禁用「查看执行详情」按钮。

**Files:**
- Modify: `frontend/src/pages/tasks-page.tsx`（`return (` 之后、`filtered.map` 之前插入废弃判定算法 约 line 972-976；行渲染容器 line 992；主标签行 line 1000-1003；查看执行详情按钮 line 1005-1016）

- [ ] **Step 1: 插入废弃判定算法（supersededIdx）**

找到 `return (` + 连接线 div + `filtered.map` 的起始（约 line 972-976）：
```tsx
                  return (
                    <div className="relative pl-7">
                      {/* 连续连接线 */}
                      <div className="absolute left-[9px] top-2 bottom-2 w-[2px] bg-[#E2E8F0]" />
                      {filtered.map((evt, idx) => {
```

在 `return (` **之前**（即 IIFE 内、return 语句之前）插入废弃判定算法：

```tsx
                  // 废弃判定：一条 node_complete/node_failed，若其 node_id 出现在其后
                  // 某个 rewoun 的 rewound_nodes 里，说明它是被退回重跑掉的旧轮 → 标记废弃。
                  // 旧轮记录的行内 output_summary 是真快照（保留），但其「查看执行详情」按钮
                  // 取的 checkpointer thread 已被 rewind 清掉覆盖（新轮内容，误导），故禁用。
                  const supersededIdx = new Set<number>()
                  for (let i = 0; i < filtered.length; i++) {
                    const evt = filtered[i]
                    if (evt.event_type !== 'node_complete' && evt.event_type !== 'node_failed') continue
                    const nodeId = evt.data?.node_id as string | undefined
                    if (!nodeId) continue
                    for (let j = i + 1; j < filtered.length; j++) {
                      const later = filtered[j]
                      if (later.event_type === 'rewoun') {
                        const rewound = (later.data?.rewound_nodes as string[] | undefined) ?? []
                        if (rewound.includes(nodeId)) {
                          supersededIdx.add(i)
                          break
                        }
                      }
                    }
                  }
```

- [ ] **Step 2: 在 map 回调顶部算出 superseded 标志**

找到 `filtered.map((evt, idx) => {` 回调内的开头（约 line 977）：
```tsx
                      {filtered.map((evt, idx) => {
                        const meta = eventMeta[evt.event_type] ?? { label: evt.event_type, color: '#94A3B8' }
```

在 `const meta = ...` **之前**加一行：
```tsx
                      {filtered.map((evt, idx) => {
                        const superseded = supersededIdx.has(idx)
                        const meta = eventMeta[evt.event_type] ?? { label: evt.event_type, color: '#94A3B8' }
```

- [ ] **Step 3: 行容器加置灰**

找到行容器（约 line 993）：
```tsx
                          <div key={idx} className="relative py-2">
```
改为（superseded 时加 `opacity-50`）：
```tsx
                          <div key={idx} className={`relative py-2${superseded ? ' opacity-50' : ''}`}>
```

- [ ] **Step 4: 主标签后加「已废弃」标签**

找到主标签行（约 line 1000-1003）：
```tsx
                            <div className="flex items-baseline gap-2 flex-wrap">
                              <span className="text-xs font-medium" style={{ color: meta.color }}>{displayLabel}</span>
                              <span className="text-[11px] text-[#94A3B8]">{formatDateTime(evt.timestamp)}</span>
                              {evt.actor && <span className="text-[11px] text-[#94A3B8]">· {evt.actor}</span>}
```
在 `{displayLabel}` 那一行 `<span>` 之后、`formatDateTime` 之前，加废弃标签：
```tsx
                            <div className="flex items-baseline gap-2 flex-wrap">
                              <span className="text-xs font-medium" style={{ color: meta.color }}>{displayLabel}</span>
                              {superseded && <span className="text-[10px] text-[#94A3B8] bg-[#F1F5F9] rounded px-1 py-0.5">已废弃</span>}
                              <span className="text-[11px] text-[#94A3B8]">{formatDateTime(evt.timestamp)}</span>
                              {evt.actor && <span className="text-[11px] text-[#94A3B8]">· {evt.actor}</span>}
```

- [ ] **Step 5: 「查看执行详情」按钮加 superseded 分支**

找到「查看执行详情」按钮（约 line 1005-1016）：
```tsx
                              {isNodeEvent && nodeType === 'agent' &&
                                (evt.event_type === 'node_complete' || evt.event_type === 'node_failed') && (
                                <button
                                  onClick={() => setNodeDetail({
                                    taskId: taskDetail.id,
                                    nodeId: evt.data?.node_id as string,
                                  })}
                                  className="text-[10px] text-[#1E5EFF] hover:underline border-0 bg-transparent cursor-pointer p-0 ml-1"
                                >
                                  查看执行详情
                                </button>
                              )}
```

改为（superseded 时用 Tooltip + 灰色 span 替代可点击 button）：
```tsx
                              {isNodeEvent && nodeType === 'agent' &&
                                (evt.event_type === 'node_complete' || evt.event_type === 'node_failed') && (
                                superseded ? (
                                  <Tooltip title="该记录已被退回重跑覆盖，详情不可用">
                                    <span className="text-[10px] text-[#94A3B8] cursor-not-allowed ml-1">查看执行详情</span>
                                  </Tooltip>
                                ) : (
                                  <button
                                    onClick={() => setNodeDetail({
                                      taskId: taskDetail.id,
                                      nodeId: evt.data?.node_id as string,
                                    })}
                                    className="text-[10px] text-[#1E5EFF] hover:underline border-0 bg-transparent cursor-pointer p-0 ml-1"
                                  >
                                    查看执行详情
                                  </button>
                                )
                              )}
```

> 说明：superseded 时用 `<span>` + `cursor-not-allowed` + 灰色 `#94A3B8`，外裹 antd `Tooltip`（Task 1 已导入）。不点击、无 hover 下划线，视觉上明确「不可用」。Tooltip 文案「该记录已被退回重跑覆盖，详情不可用」解释为何禁用。

- [ ] **Step 6: 类型检查 + 构建通过**

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npx tsc --noEmit -p tsconfig.app.json 2>&1 | tail -5`
Expected: 无错误。

Run: `cd /Users/huyuekai/company/agent-flow/frontend && npm run build 2>&1 | tail -5`
Expected: 构建成功。

- [ ] **Step 7: 手动验证点**

启动 `npm run dev`，找一个做过 rewind 的 task（timeline 里有 rewoun 事件）：
1. rewoun 之前的、属于 rewound_nodes 的旧 node_complete 记录：整行变淡（opacity-50）+ 显示灰色「已废弃」标签 + 「查看执行详情」变成灰色不可点击 + hover 显示 Tooltip「该记录已被退回重跑覆盖，详情不可用」
2. rewoun 之后的新轮 node_complete 记录：正常显示（不淡、无废弃标签）+ 「查看执行详情」可点击
3. rewoun 事件本身：橙色「已退回重跑」标签，正常显示
4. 非 agent 节点（如 start/tool）的旧轮记录：置灰 + 「已废弃」标签（无详情按钮，不报错）
5. 没做过 rewind 的 task：所有 node_complete 正常，无废弃标记，详情按钮可用

- [ ] **Step 8: 提交**

```bash
cd /Users/huyuekai/company/agent-flow
git add frontend/src/pages/tasks-page.tsx
git commit -m "feat(timeline): 被 rewoun 废弃的旧轮记录标记已废弃 + 禁用查看执行详情"
```

---

## 验收标准（Definition of Done）

1. `cd /Users/huyuekai/company/agent-flow/frontend && npm run build` 构建成功，无 TS 错误
2. timeline 不再显示任何 node_start 事件
3. rewind 后：rewoun 之前被覆盖的旧 node_complete/node_failed 置灰 + 「已废弃」标签 + 详情按钮禁用（Tooltip 提示）
4. rewoun 之后的新轮记录正常显示、详情按钮可用
5. 非 rewind 场景不受影响（无废弃标记，详情按钮正常）
6. 多次 rewind：中间轮也被正确标记废弃

## 未覆盖（spec §1.2 非目标）

- 同节点多轮折叠成一组（后续再说）
- 后端改动（timeline 仍 append-only）
- 隐藏旧轮记录（本次保留 + 标记）
- node_start 补充入参快照（本次直接隐藏）

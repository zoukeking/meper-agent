# Timeline rewind 后展示优化设计文档

**日期**: 2026-07-17
**状态**: 设计完成，待实施

## 1. 概述

rewind（退回重跑）功能上线后，task 详情页 timeline（执行流程）出现两个 UX 问题：

1. **旧轮记录与新轮混淆**：timeline 是 append-only 扁平列表，前端无聚合。rewind 后被重跑的节点在 timeline 里保留旧轮的 node_complete，又追加新一轮的 node_complete，于是同一节点出现两条记录。更糟的是：旧轮记录的「查看执行详情」按钮从 LangGraph checkpointer thread 取数据，而 rewind 已清掉旧 thread 并重跑覆盖，点开看到的是新轮内容——旧记录存在但内容和新的一样，让人困惑。
2. **node_start 没有内容**：node_start 事件的 data 只有 `{node_id, node_type}`，这两个字段已被前端拼进主标签，折叠「详细数据」里剩下的内容相当于重复，看起来是空的。

### 1.1 目标
- 隐藏所有 node_start 事件（每个节点只留 node_complete/node_failed 一条）
- 被 rewind 废弃的旧轮 node_complete/node_failed：保留显示但标记「已废弃」（置灰 + 标签），并禁用其「查看执行详情」按钮（因 checkpointer thread 已被覆盖，点开是新轮内容，会误导）
- rewoun 事件本身保留显示（橙色「已退回重跑」标签，作为「这里发生过退回」的视觉锚点）

### 1.2 非目标（本次不含）
- 不做「同节点多轮折叠成一组」（信息密度优化，后续再说）
- 不改后端（timeline 仍是 append-only，废弃判定纯前端）
- 不隐藏旧轮记录（保留审计轨迹，仅标记 + 禁用按钮）
- 不给 node_start 补充入参快照（直接隐藏更简洁）

## 2. 现状分析

timeline 渲染在 `frontend/src/pages/tasks-page.tsx:911-1039`，是一个 IIFE：
- line 918-932：`timeline.filter(...)` 过滤逻辑（当前只过滤审批已解决后的 waiting_human、human 节点的 node_complete）
- line 976：`filtered.map((evt, idx) => ...)` 扁平渲染，**无 node_id 聚合、无 rewind 去重**
- line 1005-1016：agent 节点的 node_complete/node_failed 显示「查看执行详情」按钮，调 `setNodeDetail` → `getNodeTimeline(taskId, nodeId)` 从 checkpointer thread 取数据
- line 990、1019-1029：`hasData = Object.keys(evt.data ?? {}).length > 0` 控制折叠「详细数据」块显示

后端 timeline 事件 data 字段：
| event_type | data | 位置 |
|---|---|---|
| node_start | `{node_id, node_type}` | `engine.py:734-735` |
| node_complete | `{node_id, node_type, output_summary, usage}` | `engine.py:773-774` |
| node_failed | `{node_id, node_type, error}` | `engine.py:942-947` |
| rewoun | `{node_id, rewound_nodes, comment, triggered_by, [variables_overridden]}` | `task_service.py:1063-1098` |

关键事实：rewind 后旧轮 node_complete 的**行内 `output_summary` 是旧快照（有审计价值，保留）**，但其「查看执行详情」按钮取的 checkpointer thread 已被 rewind 清掉覆盖（新轮内容，误导）。所以策略是：旧轮记录保留行内数据、禁用详情按钮。

## 3. 设计

### 3.1 改动 A：隐藏 node_start

在 timeline 过滤逻辑（line 926-932 的 `filtered = timeline.filter(...)`）增加一条：过滤掉 `event_type === 'node_start'`。

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

### 3.2 改动 B：标记废弃旧轮记录 + 禁用详情按钮

#### 废弃判定算法
在过滤后、渲染前，扫描 filtered 算出「被后续 rewoun 废弃的节点完成事件下标集合」`supersededIdx: Set<number>`：

```ts
// 废弃判定：一条 node_complete/node_failed，若其 node_id 出现在其后某个 rewoun 的 rewound_nodes 里，则废弃
const supersededIdx = new Set<number>()
for (let i = 0; i < filtered.length; i++) {
  const evt = filtered[i]
  if (evt.event_type !== 'node_complete' && evt.event_type !== 'node_failed') continue
  const nodeId = evt.data?.node_id as string | undefined
  if (!nodeId) continue
  // 向后扫描，看是否存在一个 rewoun 事件，其 rewound_nodes 包含该 nodeId
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

> 算法说明：只看「后续」的 rewoun。一条 node_complete 若被其后的 rewoun 覆盖，说明它是被重跑掉的旧轮。rewoun 之前的 rewoun 不影响（那是更早的退回，与本条无关）。`rewound_nodes` 来自后端 `rewind_task` 写入的 rewoun 事件 payload（`task_service.py:1063-1098`）。

#### 渲染时的废弃处理
在 `filtered.map((evt, idx) => ...)` 内（line 976），对 `supersededIdx.has(idx)` 的记录：
1. 整行降低不透明度：容器 `<div key={idx} className="relative py-2">` 加 `opacity-50`
2. 主标签后追加「已废弃」灰色 Tag：`<span className="text-[10px] text-[#94A3B8]">已废弃</span>`（不用 antd Tag，保持与现有 `text-[11px]` 文本风格一致，轻量）
3. 禁用「查看执行详情」按钮：用 antd `Tooltip`（已导入）包裹，按钮设 `disabled` + 灰色样式，Tooltip 文案「该记录已被退回重跑覆盖，详情不可用」

「查看执行详情」按钮当前代码（line 1005-1016）改为：

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

> 说明：`superseded` = `supersededIdx.has(idx)`，在 map 回调顶部算出。用 `<span>` + `cursor-not-allowed` 替代 disabled button（antd button 在纯文本链接场景下样式不一致；span + Tooltip 更贴合现有「文字链接」视觉）。

### 3.3 边界情况

| 场景 | 处理 |
|---|---|
| 非 agent 节点（start/tool/gateway）的旧轮记录 | 标记废弃 + 置灰（这些节点本来就没「查看执行详情」按钮，只做置灰 + 「已废弃」标签） |
| 多次 rewind（退回后再退回） | 算法天然支持：每个 node_complete 检查其后的所有 rewoun，任一覆盖即废弃。多次退回的中间轮也会被标记 |
| rewound_nodes 缺失（旧数据/异常） | `(later.data?.rewound_nodes as string[] | undefined) ?? []` 兜底空数组，不会误判 |
| 同一 rewoun 覆盖多个节点 | rewound_nodes 是数组，算法遍历包含判定，多节点都会被标记 |
| 新轮记录（rewoun 之后的 node_complete） | 不会被标记（它后面没有覆盖它的 rewoun），正常显示，详情按钮可用 |

## 4. 改动文件

仅 `frontend/src/pages/tasks-page.tsx`：
- line 926-932：过滤逻辑加 `node_start` 隐藏（改动 A）
- line 972-976 之间（`return (` 之后、`filtered.map` 之前）：插入 `supersededIdx` 计算（改动 B 判定）
- line 992（`<div key={idx}>`）：加 `opacity-50`（改动 B 置灰）
- line 1000-1003 区域（主标签行）：加「已废弃」标签（改动 B）
- line 1005-1016（查看执行详情按钮）：加 superseded 分支 + Tooltip（改动 B 禁用）

## 5. 测试计划

前端无单测框架，用 `tsc --noEmit` + `npm run build` + 手动验证：

1. 普通 task（无 rewind）：timeline 不显示 node_start，每节点只一条 node_complete；详情按钮正常可用
2. rewind 一次：rewoun 标签显示（橙色）；rewoun 之前的被覆盖节点记录置灰 + 「已废弃」标签 + 详情按钮禁用（Tooltip 提示）；rewoun 之后的新轮记录正常
3. 多次 rewind：中间轮也被标记废弃
4. 非 agent 节点旧轮：置灰 + 「已废弃」标签（无详情按钮，不报错）
5. node_start 完全不出现（包括非 rewind 场景）

## 6. 风险

| 风险 | 缓解 |
|---|---|
| 废弃判定算法 O(n²)（双重循环） | timeline 事件数通常 < 100，n² 可忽略；若未来 timeline 变长可优化为单遍 + Map |
| `rewound_nodes` 字段名/结构变化 | 算法用 `?? []` 兜底，字段缺失不会误判（只是不标记） |
| 隐藏 node_start 后，正在执行的节点（只有 node_start 还没 node_complete）从 timeline 消失 | 这是预期行为：执行中的节点本就没有完成信息可展示，详情区另有「节点进度」展示（看板的 finishedNodes）；timeline 只展示已完成事件更清晰 |

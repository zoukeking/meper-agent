# Agent 欢迎词与推荐问题/操作 — 设计方案

> 状态：草案（待评审）　｜　范围：meper-agent　｜　关联客户端：frontend-client
> 决策已确认（见 §2）

## 1. 背景与目标

当前 Agent 配置只有面向 LLM 的提示词卡槽（`prompt_slots`）与描述（`description`），**没有任何面向终端用户的「首屏展示内容」**。frontend-client 新建对话后的欢迎态（`ChatView.tsx:167-174`）是硬编码的「和 {agent.name} 开始对话」，所有 agent 千篇一律，缺少引导。

本需求为 Agent 增加两种面向终端用户的展示内容：

- **欢迎词**（welcome message）：新建对话首屏展示的一段文本（支持 Markdown），用于介绍 agent 能力、定调语气。
- **推荐问题/操作**（recommended items）：若干快捷按钮，终端用户点击即把预设内容作为消息发送，一键开聊。

目标：让每个 agent 在 frontend-client 拥有个性化的首屏引导，降低用户开口成本。

## 2. 需求范围与已确认决策

| 决策点 | 选定方案 |
|---|---|
| 推荐项数据结构 | **对象 `{label, prompt?}`**：按钮显示 `label`；点击发送 `prompt`，`prompt` 为空则发送 `label`。既覆盖「推荐问题」(label=一句话) 也覆盖「操作」(按钮文案 ≠ 实际指令)。 |
| 快捷按钮点击行为 | **直接发送并开始对话**：点击即把内容作为 user message 发出，复用 frontend-client 现成的 `submit()`。 |
| 覆盖范围 | **仅 frontend-client**（第一方对话客户端）。ext 嵌入端（ChatUI_Ant_X 等）不在本次范围，后续可复用同一后端字段扩展。 |
| 欢迎词格式 | 支持 **Markdown**（frontend-client 已有 `react-markdown` + `remark-gfm`，渲染零成本；studio 编辑用 `textarea`）。 |

非目标：
- 不做富文本所见即所得编辑器（textarea + Markdown 渲染即可）。
- 不改 ext / ChatUI_Ant_X 嵌入端。
- 不做推荐项的「条件展示 / 分组 / 图标」等增强（MVP 只支持有序文本列表）。

## 3. 数据模型设计

存储：MongoDB（`agents` collection），**schemaless，无需迁移脚本**。老文档读取时用 `.get(key, default)` 兜底（与现有字段一致）。

### 3.1 新增字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `welcome_message` | `str` | `""` | 欢迎词，Markdown 文本，上限 2000 字符。 |
| `recommended_items` | `list[RecommendedItem]` | `[]` | 推荐项列表，有序，上限 **10 条**。 |

### 3.2 推荐项子模型 `RecommendedItem`

```python
class RecommendedItem(BaseModel):
    label: str = Field(..., min_length=1, max_length=100, description="按钮显示文案")
    prompt: str = Field(default="", max_length=500, description="实际发送内容；留空则用 label")
```

前端读取约定：发送内容 = `item.prompt || item.label`。

JSON 示例：
```json
{
  "welcome_message": "你好，我是**销售助理**。可以问我销售相关的问题，或直接点下方按钮。",
  "recommended_items": [
    { "label": "我们公司主要卖什么？", "prompt": "" },
    { "label": "导出本月报表", "prompt": "帮我把本月销售数据导出为 Excel" }
  ]
}
```

## 4. 后端实现（逐文件）

### 4.1 `backend/app/models/agent.py`
- 新增 `RecommendedItem`（如上）。
- `Agent` 模型加两个字段（带默认值，约在 `:62` `updated_at` 之前）。

### 4.2 `backend/app/schemas/agent.py`
- 定义/复用 `RecommendedItem`（可与 model 共用，或在此定义独立的 schema；建议**共用 model 里的 `RecommendedItem`**，避免重复）。
- `AgentUpdate`（`:75`）加：
  - `welcome_message: str = Field(default="", max_length=2000)`
  - `recommended_items: list[RecommendedItem] = Field(default_factory=list, max_length=10)`
- `AgentResponse`（`:155`）加同样两字段（用于透出）。
- 清洗：
  - `welcome_message` 复用现有 `sanitize_text` 的 `@field_validator`（扩展到该字段名）。
  - `recommended_items` 加一个 `@field_validator(mode="after")`：逐条 `sanitize_text(label)` / `sanitize_text(prompt)`，并校验条数 ≤ 10、label 非空。

### 4.3 `backend/app/services/agent_service.py`
三处补字段（与现有字段并列）：
- `create_agent`（`:87-104` 的 doc dict）：写入 `welcome_message=""`、`recommended_items=[]` 默认值。
- `update_agent`（`:241-255` 的 `set_fields`）：把入参的两字段加入 `$set`。
- `duplicate_agent`（`:465-478`）：显式复制两字段（否则复制 agent 会丢配置）。

### 4.4 `backend/app/api/v1/agents.py`
- `_doc_to_response`（`:35-59`）补：
  ```python
  welcome_message=doc.get("welcome_message", ""),
  recommended_items=doc.get("recommended_items", []),
  ```
- 由于 `list_agents` 返回 `AgentListResponse`，其 `items: list[AgentResponse]` 走同一出口，**列表接口自动带上新字段** → frontend-client 的 `GET /v1/agents` 无需任何额外接口即可拿到。

> 鉴权说明：frontend-client 走内部 `/api/v1/agents`（Bearer token，见 `frontend-client/src/api/client.ts:96`），**不走** `/api/v1/ext/agents`。故本次无需改 ext 链路。

## 5. frontend-studio 实现（管理后台配置 UI）

### 5.1 `frontend-studio/src/types.ts`
视图模型 `Agent` 接口（`:1-28`）加：
```ts
welcomeMessage: string
recommendedItems: { label: string; prompt: string }[]
```

### 5.2 `frontend-studio/src/services/adapters.ts`
- `toStudioAgent`（后端→视图，`:57-84`）：映射 `welcome_message → welcomeMessage`、`recommended_items → recommendedItems`（prompt 为空时填 ""）。
- `fromStudioAgent`（视图→后端，`:87-130`）：反向映射回 `welcome_message`、`recommended_items`。

### 5.3 `frontend-studio/src/services/agent-api.ts`
`AgentUpdateInput`（`:46-63`）加：
```ts
welcome_message?: string
recommended_items?: { label: string; prompt: string }[]
```

### 5.4 `frontend-studio/src/components/AgentEditorPage.tsx`
新增一个 `Section`（参照现有「Prompt 配置」「执行参数」区段，`:300-318`）——「欢迎与引导」：
- **欢迎词**：`<Field label="欢迎词（支持 Markdown）"><textarea rows={4} .../></Field>`，绑定 `form.welcomeMessage`（参照 `:224-226` 的 systemPrompt textarea 写法）。
- **推荐项**：参照 `features/workflow-editor/node-config-panels/GatewayNodeConfig.tsx:42-119` 的「条件列表」map + 增删模式：
  - 顶部「+ 添加推荐项」按钮（`lucide-react` 的 `Plus`）。
  - 每条一个卡片：`显示文案` input（必填）+ `发送内容` input（可选，placeholder「留空则同显示文案」）+ 右上「删除」按钮（`Trash2`）。
  - 空列表时显示「暂无推荐项，点击添加」。
  - 数量超过 10 时禁用「添加」并提示。
  - 增/改/删通过 `set({ recommendedItems: [...] })` 更新（`set` helper 见 `:142`）。

> 保存走现有 `handleSave` → `agentApi.update`（PUT 全量替换）。前端始终提交完整 `recommended_items` 数组（含顺序），与后端全量替换语义一致。

## 6. frontend-client 实现（终端对话渲染）

### 6.1 `frontend-client/src/types.ts`
- `AgentRecord`（后端原始形态，约 `:25-40`）加 `welcome_message?: string`、`recommended_items?: { label: string; prompt: string }[]`。
- `AgentSummary`（`:17-24`）加 `welcomeMessage?: string`、`recommendedItems?: { label: string; prompt: string }[]`。

### 6.2 `frontend-client/src/api/chat.ts`
`toLocalAgent`（`:13-22`）透传两字段：
```ts
welcomeMessage: item.welcome_message ?? '',
recommendedItems: item.recommended_items ?? [],
```

### 6.3 `frontend-client/src/components/ChatView.tsx`
改写 `welcome-state`（`:167-174`，当前硬编码）：
- **欢迎词**：若 `agent.welcomeMessage` 非空，用 `react-markdown`（+ `remark-gfm`）渲染，替代硬编码的「和 {agent.name} 开始对话」；为空时回退到现有默认文案。
- **推荐项快捷按钮**：在欢迎词下方渲染 `agent.recommendedItems.map(item => <Button onClick={() => submit(item.prompt || item.label)}>{item.label}</Button>)`。
  - `submit`（`:91-95`）已就绪：清空输入框/附件后调用 `send(text, [])`。点击即发送，符合已确认的交互。
  - 样式：antd `<Button>`，新增 className（如 `welcome-suggestion`），在 `styles.css` 追加规则，沿用 `--client-accent` / `--client-accent-soft` / `--client-border`，圆角与全局一致（`borderRadius:12`，`main.tsx:38`）。参考 `.clarification-options`（`styles.css:595` 附近）。
  - 加载/流式响应中（`running`）时禁用按钮，避免重复发送。
- 为空且无推荐项时，回退到现有默认欢迎态（保证不破坏未配置的 agent）。

## 7. 改动文件清单

**后端（4）**
- `backend/app/models/agent.py` — 加 `RecommendedItem` + 2 字段
- `backend/app/schemas/agent.py` — `AgentUpdate` / `AgentResponse` 加字段 + 清洗
- `backend/app/services/agent_service.py` — create / update / duplicate 三处
- `backend/app/api/v1/agents.py` — `_doc_to_response` 补字段

**frontend-studio（4）**
- `frontend-studio/src/types.ts`
- `frontend-studio/src/services/adapters.ts`
- `frontend-studio/src/services/agent-api.ts`
- `frontend-studio/src/components/AgentEditorPage.tsx`

**frontend-client（3）**
- `frontend-client/src/types.ts`
- `frontend-client/src/api/chat.ts`
- `frontend-client/src/components/ChatView.tsx`（+ `src/styles.css` 追加按钮样式）

共计 11 个文件，无 DB 迁移，无新依赖。

## 8. 验收标准

1. **studio 配置**：在 AgentEditorPage 能填写欢迎词、增删改推荐项（label 必填、prompt 可选）；保存后重新打开编辑页，内容正确回显。
2. **后端存储/透出**：`PUT /api/v1/agents/{id}` 能写入两字段；`GET /api/v1/agents`（含列表）响应里包含两字段；`duplicate` 复制的 agent 带上配置。
3. **客户端渲染**：frontend-client 新建对话后，首屏显示该 agent 的欢迎词（Markdown 正确渲染）+ 推荐项按钮；点击按钮立即以「发送内容」发出消息并收到 agent 回复；`prompt` 为空时发送 `label`。
4. **兜底**：未配置欢迎词/推荐项的 agent，frontend-client 回退到现有默认欢迎态，无报错、无空按钮。
5. **安全**：欢迎词与 label/prompt 经 `sanitize_text` 清洗，能阻断存储型 XSS（前端 Markdown 渲染默认不执行内联 HTML）。
6. **约束**：已发布（published）agent 编辑欢迎词/推荐项时，遵循现有「已发布不可编辑」规则（自动生效，无需额外处理）。

## 9. 风险与约束

- **已发布 Agent 不可编辑**：`agent_service.update_agent` 对 `status==published` 直接拒绝（`:220-224`）。配置欢迎词/推荐项需在 draft 状态，或先 archive。这是既有规则，新字段自动遵循。
- **全量替换语义**：`AgentUpdate` 是 PUT 全量替换，studio 前端每次保存提交完整 `recommended_items`。前端删/排序后直接提交即可，无并发合并问题（单管理员场景）。
- **Markdown 渲染安全**：frontend-client 用 `react-markdown`（默认不渲染 raw HTML），结合后端 `sanitize_text`，双层防御 XSS。
- **字段长度/数量上限**：欢迎词 2000 字符、推荐项 ≤ 10 条、label ≤ 100、prompt ≤ 500，后端 schema 强校验，防止滥用。
- **前端类型一致性**：三端（studio / client / 后端）的推荐项对象 shape 需保持 `{label, prompt}` 一致；命名在后端用 `recommended_items`（snake_case），前端视图用 `recommendedItems`（camelCase），由 adapter 转换。

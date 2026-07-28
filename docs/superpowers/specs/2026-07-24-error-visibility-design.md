# 错误可见性改造设计

> **日期**：2026-07-24
> **状态**：待实现
> **目标**：让所有执行链路与管理操作中的错误都能被前端感知，杜绝"出错但前端无反应、不执行"的静默问题。

## 1. 背景与问题

当前项目存在大量"错误被静默"的情况，导致前端无响应、用户困惑。经全链路审计，问题分布在两个通道：

### 通道 A：SSE 执行流（agent 对话 / 工具执行）

| 错误场景 | 现状 | 后果 |
|---------|------|------|
| 工具执行失败（MCP 权限/超时、Skill 报错、Sandbox 错误、Subagent 失败） | `tool_wrapper.py` 把 `ToolException` 转成 `ToolMessage(status="error")` 回传 LLM（设计正确），但 `stream_events.py:166` 的 `on_tool_end` **只读 `output.content`，不检查 `status` 字段**，前端收到伪装成成功的 `tool_result` | MCP 没权限、工具超时等错误前端完全无感 |
| LLM 调用失败（**模型欠费**、限流、超时） | `on_chat_model_error` → `ErrorEvent(source="llm")`，能传给前端，但 frontend-studio（`ChatHomepage.tsx:885`）和 frontend-client（`use-chat.ts:413`）收到 error 就 `throw`，**中断整个流**且丢弃 `source` | 模型欠费时错误闪现一下流就断了，体验差 |
| 工具/MCP 加载失败 | `mcp_tool_cache.py:189` MCP server 连不上 → `return []`；`registry.py` / `tool_builder.py` 加载失败 → `return None`；`context.py` 静默 `continue` | 工具悄悄消失，MCP 服务端出问题时前端不知道工具为何不可用 |
| 顶层异常 | `agent_execution_service.py:188` 拼 dict `{'type':'error','content':str(exc)}`，**绕过 ErrorEvent schema，丢失 `source`** | 前端拿不到结构化错误来源 |
| 工具成功/失败判定 | 三端前端**各用不同方式嗅探文本**判断是否错误：studio 用正则 `/\b(error|fail)/i`、client 用 `/(error\|failed\|traceback)/i`、主前端用 `startsWith('Error')` | 脆弱、不一致，漏判误判 |

### 通道 B：REST 管理 API（CRUD）

| 错误场景 | 现状 | 后果 |
|---------|------|------|
| **名称重复**（Agent/Tool/MCP 创建重名） | 后端**正确**抛 `ConflictError(409)`，返回结构化 `{error:{code,message}}` | 后端没问题 |
| 创建/更新/删除失败 | 后端异常处理健全（`ExceptionMiddleware` 统一转 `AppError` 响应） | 后端没问题 |
| **前端接收** | 三个前端的 TanStack Query **都没有全局 `defaultOptions.mutations.onError`**，axios/fetch 拦截器只 `reject` 不 toast。是否提示完全取决于每个 mutation 是否手写了 `onError` | `WorkflowDesigner` 的 save/publish/create、`api-keys-page` 的 toggleWhStatus/deleteWh 等多个 mutation **无 onError**，名称重复等错误前端完全无反应，按钮 loading 结束看似成功 |

**根因**：后端错误处理基本健全；问题集中在前端"最后一公里"——既缺少全局兜底（REST mutation），又丢失结构化字段（SSE 工具错误）。

### 不在本次范围（YAGNI）

- **WebSocket 断连静默**（任务状态停止更新用户不知道）：独立通道，本次不处理
- **Workflow 节点错误**（Gateway 全条件异常走 default、Parallel gather 吞异常）：走 task/Redis/WS 通道，独立机制，单独立项
- **执行期 MCP RBAC 权限校验**：安全功能，不是错误可见性，单独立项

## 2. 设计方案

### 2.1 核心原则

1. **不破坏现有好的设计**：工具错误的"回传 LLM 自愈"（`ToolException → ToolMessage`）保留，只让它在传给前端时不再被伪装成成功
2. **后端做最小改动**：补全结构化字段，不重造错误处理体系
3. **前端建立全局兜底**：一处修复堵住所有现在和未来遗漏的 mutation

### 2.2 第一类机制：工具执行报错写入工具结果

**适用场景**：MCP/普通工具/Skill/Subagent/Sandbox 在实际 `invoke` 时抛 `ToolException`（含 MCP `isError=true`、权限不足、超时、参数校验失败）。**不含**工具加载失败（那属于第二类）。

**信号载体**：带 `status` 字段的 `tool_result` 事件。**不中断流**——错误已回传 LLM，agent 可能重试/换工具/转告用户，流应继续。

#### 后端改动 1：扩展事件 schema

**文件**：`backend/app/engine/harness_integration/adapters/app_event.py`（第 81-87 行）

给 `ToolResultEvent` 加 `status` 字段：

```python
class ToolResultEvent(_Base):
    """The result content of a completed tool invocation."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: str
    content: str
    status: Literal["success", "error"] = "success"   # 新增，默认 success 保持向后兼容
```

#### 后端改动 2：on_tool_end 检查 ToolMessage 的 status（关键修复）

**文件**：`backend/app/engine/harness_integration/adapters/stream_events.py:155-171`

```python
elif kind == "on_tool_end":
    output = data.get("output")
    tool_name = event.get("name") or "unknown"
    if output is None:
        content = ""
    elif hasattr(output, "content"):
        content = str(output.content)
    else:
        content = str(output)
    # 新增：检查 ToolMessage 的 status，把 error 透传给前端
    status = "error" if getattr(output, "status", None) == "error" else "success"
    await on_event(
        ToolResultEvent(
            tool_name=tool_name,
            content=content,
            status=status,
        )
    )
```

这一改同时点亮：普通工具、MCP（`isError=true` → `ToolException`）、Skill、Subagent delegate、Sandbox 工具失败——它们都经 `tool_wrapper.py:81-86` 转 `ToolMessage(status="error")`。

**`tool_wrapper.py` 无需改动**——现状已正确把工具错误转成 `ToolMessage(status="error")` 回传 LLM。

### 2.3 第二类机制：普通接口错误返回前端（全量暴露）

**适用场景**：所有非工具执行的错误，**无论服务端还是客户端错误都暴露给前端**。包括：模型欠费、配置错误、工具/模型加载失败、静默回退、名称重复、服务端内部异常等。

**信号载体**：`error` 事件（走 ErrorEvent schema，带 `source`）。**中断流**——这类错误用户需要知晓或处理。

#### 后端改动 3：顶层异常走 ErrorEvent schema（修复 source 丢失）

**文件**：`backend/app/services/agent_execution_service.py:184-188`（stream）和 `:257-261`（resume）

把直接拼 dict 改为走 `ErrorEvent` schema，并补全 `source` 字段：

```python
except Exception as exc:
    run_error = exc
    logger.error("agent_stream_error", agent_id=agent_id, request_id=request_id, error=str(exc))
    logger.exception("agent_stream_error_traceback")
    # 走 ErrorEvent schema，保留 source 字段（不再拼裸 dict）
    err_data = ErrorEvent(
        message=str(exc),
        source=_classify_error_source(exc),
    ).model_dump()
    # 前端契约用 content，ErrorEvent 用 message，做字段重映射
    err_data["content"] = err_data.pop("message", "")
    await event_queue.put(f"data: {safe_json(err_data)}\n\n")
    result = {}
```

新增错误来源分类辅助函数（在同文件或独立模块）：

```python
def _classify_error_source(exc: BaseException) -> str:
    """根据异常类型判定错误来源，供前端区分展示。"""
    # LLM 相关：模型欠费、限流、超时、鉴权
    if _is_llm_error(exc):
        return "llm"
    # 工具相关：加载失败、配置错误
    if _is_tool_error(exc):
        return "tool"
    # 其余（含服务端内部异常）归 graph
    return "graph"
```

判定逻辑基于异常类型/消息内容启发式判断（LLM 库抛的特定异常类型、错误消息中的关键词如 "quota"/"rate limit"/"insufficient"）。**注意**：按"所有错误都暴露"的原则，服务端内部异常也用 `str(exc)` 暴露给前端，不做脱敏（用户明确要求全量覆盖）。

#### 后端改动 4：收集加载诊断并发出 error 事件（修复工具静默消失）

**机制**：`resolve_harness_context` 返回的 `hctx` 字典中新增 `load_errors: list[dict]` 字段。`execution.py` 的 `stream()` / `resume()` 在执行 graph 前后（建议在 `stream_events_to_app_events` 之前，让用户尽早看到加载失败），把每条 `load_errors` 通过 `_make_event_callback` 作为 `ErrorEvent(source="tool")` 发出。

**文件 1**：`backend/app/engine/harness_integration/context.py` — `resolve_harness_context`

在函数内初始化 `load_errors: list[dict] = []`，在各工具加载的静默 catch 处收集：

```python
load_errors: list[dict] = []

# MCP 工具加载（原依赖 mcp_tool_cache 静默 return []）
for conn_doc in mcp_docs:
    try:
        tools = await get_mcp_tools_cached([conn_doc["_id"]], ...)
        resolved_tools.extend(tools)
    except Exception as exc:
        logger.warning("mcp_tools_load_failed", ...)
        load_errors.append({
            "tool_name": conn_doc.get("name", "mcp"),
            "error": f"MCP 工具加载失败: {exc}",
        })

# 普通工具加载（原 build_tool 返回 None 时静默不 append）
for tool_doc in tool_docs:
    try:
        tool = await build_tool(tool_doc)
        if tool is None:
            load_errors.append({
                "tool_name": tool_doc.get("name", "tool"),
                "error": f"工具构建失败: {tool_doc.get('name')}",
            })
        else:
            resolved_tools.append(tool)
    except Exception as exc:
        logger.warning("tool_build_failed", ...)
        load_errors.append({
            "tool_name": tool_doc.get("name", "tool"),
            "error": f"工具构建失败: {exc}",
        })

# 最终放进返回的 hctx
hctx["load_errors"] = load_errors
```

**文件 2**：`backend/app/engine/harness_integration/execution.py` — `stream()` 和 `resume()`

在 `stream_events_to_app_events(...)` 调用**之前**，先把加载错误发出去：

```python
from app.engine.harness_integration.adapters.app_event import ErrorEvent

# 发出工具/MCP 加载失败（在 graph 执行前，让用户尽早知道）
for err in hctx.get("load_errors", []):
    evt = ErrorEvent(
        message=f"[{err['tool_name']}] {err['error']}",
        source="tool",
    )
    await _make_event_callback(on_event)(evt)

event_stream = graph.astream_events(state, config=config, version="v2")
await stream_events_to_app_events(event_stream, _make_event_callback(on_event), ...)
```

`invoke()`（非流式）无需发事件，但 `load_errors` 仍收集（可供 workflow agent 节点的 NodeResult 使用）。

**文件 3**：`backend/app/engine/tool/mcp_tool_cache.py:189-199` — `get_mcp_tools_cached`

把静默 `return []` 改为 `raise`（让上层 `context.py` 的 try/except 捕获并收集为 load_error）：

```python
except Exception as exc:
    # 提取 ExceptionGroup 的子异常以显示真正原因
    if hasattr(exc, "exceptions"):
        details = "; ".join(str(e) for e in exc.exceptions)
        logger.error("mcp_tools_fetch_failed", connection_ids=connection_ids, error=details)
        raise RuntimeError(f"MCP 连接失败: {details}") from exc
    else:
        logger.error("mcp_tools_fetch_failed", connection_ids=connection_ids, error=str(exc))
        raise
```

> **注意**：`get_mcp_tools_cached` 还被 workflow Tool 节点（`node_executor.py:_execute_mcp_tool`）使用。改 raise 后，workflow 路径的 try/except（`node_executor.py:838-866`）已能捕获并转成 `NodeResult(success=False)`，不会破坏 workflow。但需测试确认。

**涉及的具体加载失败点**：
- `context.py` MCP 工具加载（原静默依赖 `mcp_tool_cache.py` 的 `return []`）
- `context.py` 普通工具 `build_tool` 返回 None（原静默不 append）
- `registry.py` 动态加载失败、community 工具 config/build 失败（原 `return None` + log）——这些在 harness 层，若 agent 通过 registry 加载工具，需在 `context.py` 调用处补 try/except 收集

### 2.4 通道 B：REST 管理错误 —— 全局 mutation onError 兜底

**根因**：两个前端（`frontend`、`frontend-studio`）的 QueryClient 都没有 `defaultOptions.mutations.onError`，无 onError 的 mutation 失败时静默。

#### 前端改动 1：QueryClient 加全局 mutation onError

**frontend**（`frontend/src/main.tsx:8-16` 和 `frontend/src/config/query-client.ts`）：

统一使用 `config/query-client.ts`（修复 main.tsx 内联创建未用 config 的问题），加全局兜底：

```typescript
// frontend/src/config/query-client.ts
import { QueryClient } from '@tanstack/react-query'
import { message } from 'antd'

function extractErrorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    return (err as { message: string }).message
  }
  return '操作失败'
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false },
    mutations: {
      onError: (err: unknown) => {
        message.error(extractErrorMessage(err))
      },
    },
  },
})
```

`main.tsx` 改为 `import { queryClient } from './config/query-client'` 而非内联创建。

**frontend-studio**（`frontend-studio/src/main.tsx:9-17`）：

```typescript
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 30_000 },
    mutations: {
      onError: (err: unknown) => {
        toast.error(extractErrorMessage(err))
      },
    },
  },
})
```

（`extractErrorMessage` 在两个前端各自定义，或抽到 lib 共用。）

**显式静默机制**：需要静默某些 mutation 的调用点，显式覆盖 `onError: () => {}`。这一步堵住所有现在和未来无 onError 的 mutation，名称重复（409）等错误将自动 toast。

#### 前端改动 2：清理重复的手动 onError（可选优化）

加了全局兜底后，部分 mutation 既有全局又手写了 `onError: (err) => message.error(...)`，会触发两次提示。需检查并清理重复（保留有特殊逻辑的，如刷新列表后提示）。

### 2.5 通道 A 前端：SSE 工具结果与错误事件统一处理

#### 前端改动 3：tool_result 读 status 字段，废弃正则嗅探（三端统一）

**frontend-studio**（`ChatHomepage.tsx:824-872`）：
- `case 'tool_result'` 分支：用 `evt.status === 'error'` 替代正则 `/\b(error|fail)/i.test(resultContent)`（826 行）
- 类型定义补 `status?: 'success' | 'error'`

**frontend-client**（`use-chat.ts:352-391`）：
- `event.type === 'tool_result'` 分支：用 `event.status === 'error'` 替代正则（357 行）
- `types.ts` 的 `StreamEvent` 补 `status?: 'success' | 'error'`、`source?: string` 字段

**frontend**（`chat-panel.tsx:838-878`）：
- `eventType === 'tool_result'` 分支：用 `e.status === 'error'` 替代 `startsWith('Error')`（850、868 行）

#### 前端改动 4：error 事件不中断流 + 消费 source（studio/client 对齐 legacy）

**frontend-studio**（`ChatHomepage.tsx:885-886`）和 **frontend-client**（`use-chat.ts:413-414`）的 `case 'error': throw new Error(...)` 改为：不 throw，把错误记录到当前 agent 消息的 timeline，保留 `source` 用于图标/文案区分，流继续。参考 legacy `chat-panel.tsx:731-745` 的做法（已是不 throw 模式）。

#### 前端改动 5：历史回填渲染 error entry

**frontend-studio**（`ChatHomepage.tsx:64-137` 的 `agentMessageToDisplay`）：增加 `type === 'error'` 分支，让从 DB 回填的历史错误也能显示（持久化层已存 error 事件，只是前端不认）。

## 3. 改动清单总览

### 后端（5 处）

| 文件 | 改动 |
|------|------|
| `app/engine/harness_integration/adapters/app_event.py` | `ToolResultEvent` 加 `status: Literal["success","error"]` 字段（默认 success） |
| `app/engine/harness_integration/adapters/stream_events.py:155-171` | `on_tool_end` 检查 ToolMessage `status`，透传到 ToolResultEvent |
| `app/services/agent_execution_service.py:184-188, 257-261` | 顶层异常走 ErrorEvent schema，新增 `_classify_error_source()` 分类错误来源 |
| `app/engine/harness_integration/context.py` + `execution.py` | `resolve_harness_context` 收集加载失败到 `hctx["load_errors"]`；`stream()`/`resume()` 在 graph 执行前把它们作为 ErrorEvent(source=tool) 发出 |
| `app/engine/tool/mcp_tool_cache.py:189-199` | `get_mcp_tools_cached` 失败时 raise（不再静默 `return []`），由 context.py 捕获收集 |

### 前端（通道 B：REST 全局兜底）

| 文件 | 改动 |
|------|------|
| `frontend/src/config/query-client.ts` + `frontend/src/main.tsx` | 加全局 `mutations.onError`，main.tsx 改用 config 文件 |
| `frontend-studio/src/main.tsx` | 加全局 `mutations.onError` |

### 前端（通道 A：SSE 统一处理）

| 文件 | 改动 |
|------|------|
| `frontend-studio/src/components/ChatHomepage.tsx:824-872, 885-886, 64-137` | tool_result 读 status；error 不 throw；历史回填渲染 error |
| `frontend-client/src/hooks/use-chat.ts:352-391, 413-414` + `types.ts` | tool_result 读 status；error 不 throw；类型补字段 |
| `frontend/src/components/chat-panel.tsx:838-878` | tool_result 读 status（已是不 throw，无需改 error 分支） |

## 4. 测试与验证

### 后端单测

1. `stream_events_to_app_events`：
   - 给 `on_tool_end` 事件，output 是 `ToolMessage(status="error")` → 断言发出 `ToolResultEvent(status="error")`
   - 给正常 ToolMessage → 断言发出 `ToolResultEvent(status="success")`
2. `_classify_error_source`：
   - LLM 限流异常 → `"llm"`
   - 工具加载异常 → `"tool"`
   - 裸 `Exception` → `"graph"`
3. `context.py` 加载诊断：模拟 MCP 工具加载失败 → 断言 `load_errors` 收集到对应条目

### 前端验证

手动验证场景：
1. **名称重复**：创建重名 Agent → 自动 toast "名称重复"（全局兜底）
2. **MCP 权限不足**：调用无权限 MCP 工具 → 工具卡片标红，显示错误（status=error）
3. **模型欠费**：配置欠费模型执行 → error 事件不中断流，显示错误且带 source=llm
4. **工具加载失败**：MCP server 离线时执行 agent → 收到该工具加载失败的 error 事件
5. **历史回填**：切换到含错误的旧会话 → 错误消息正常显示

## 5. 风险与权衡

- **全局 mutation onError 可能导致重复 toast**：部分手写了 onError 的 mutation 会触发两次。需在实现时清理重复（保留有特殊逻辑的）。
- **加载诊断可能产生过多 error 事件**：若 agent 绑定多个 MCP，每个都失败会发多个 error。可接受（信息明确），或后续聚合为一条。
- **服务端内部异常暴露原文**：按用户要求全量覆盖，`str(exc)` 可能含技术细节。若后续需脱敏，可在 `_classify_error_source` 中对 graph 类做消息清洗。
- **向后兼容**：`ToolResultEvent.status` 默认 `"success"`，旧前端不读该字段也能正常工作；error 事件仍带 `content`（兼容旧契约）。

# 错误可见性改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让所有执行链路与管理操作中的错误都能被前端感知，杜绝"出错但前端无反应"的静默问题。

**Architecture:** 两条通道并行修复——(A) SSE 执行流：给 ToolResultEvent 加 status 字段透传工具错误，顶层异常走 ErrorEvent schema 保留 source，收集工具加载失败并发出 error 事件；(B) REST 管理 API：给两个前端 QueryClient 加全局 `mutations.onError` 兜底，一处堵住所有无 onError 的 mutation 静默。

**Tech Stack:** Python / FastAPI / LangGraph / Pydantic（后端）；React / TanStack Query / TypeScript（前端）

**Spec:** `docs/superpowers/specs/2026-07-24-error-visibility-design.md`

---

## File Structure

### 后端（修改文件，无新建）
| 文件 | 职责 |
|------|------|
| `backend/app/engine/harness_integration/adapters/app_event.py` | 事件 schema：ToolResultEvent 加 status 字段 |
| `backend/app/engine/harness_integration/adapters/stream_events.py` | 事件翻译：on_tool_end 检查 ToolMessage status |
| `backend/app/engine/harness_integration/context.py` | 上下文装配：收集工具/MCP 加载失败到 load_errors |
| `backend/app/engine/harness_integration/execution.py` | 执行入口：把 load_errors 作为 error 事件发出 |
| `backend/app/services/agent_execution_service.py` | 流编排：顶层异常走 ErrorEvent schema + 错误来源分类 |
| `backend/app/engine/tool/mcp_tool_cache.py` | MCP 缓存：失败时 raise 不再静默 return [] |

### 前端（修改文件，无新建）
| 文件 | 职责 |
|------|------|
| `frontend/src/config/query-client.ts` | 主前端 QueryClient：加全局 mutations.onError |
| `frontend/src/main.tsx` | 主前端入口：改用 config/query-client.ts |
| `frontend-studio/src/main.tsx` | Studio 入口：加全局 mutations.onError |
| `frontend-studio/src/services/agent-api.ts` | Studio 类型：ToolResultEvent/ErrorEvent 加字段 |
| `frontend-studio/src/components/ChatHomepage.tsx` | Studio SSE：tool_result 读 status，error 不 throw |
| `frontend-client/src/types.ts` | Client 类型：StreamEvent 加 status/source 字段 |
| `frontend-client/src/hooks/use-chat.ts` | Client SSE：tool_result 读 status，error 不 throw |
| `frontend/src/components/chat-panel.tsx` | 主前端 SSE：tool_result 读 status |

---

## Task 1: 后端 — ToolResultEvent 加 status 字段

**Files:**
- Modify: `backend/app/engine/harness_integration/adapters/app_event.py:81-87`

- [ ] **Step 1: 给 ToolResultEvent 加 status 字段**

将 `app_event.py` 第 81-87 行的 `ToolResultEvent` 改为：

```python
class ToolResultEvent(_Base):
    """The result content of a completed tool invocation."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: str
    content: str
    status: Literal["success", "error"] = "success"
```

- [ ] **Step 2: 验证现有测试仍通过**

Run: `cd backend && python -m pytest tests/ -k "app_event or stream_events or tool_result" -v --no-header 2>&1 | tail -20`
Expected: 现有测试 PASS（status 默认 "success"，向后兼容）

- [ ] **Step 3: Commit**

```bash
git add backend/app/engine/harness_integration/adapters/app_event.py
git commit -m "feat(event): ToolResultEvent 增加 status 字段标记工具成功/失败"
```

---

## Task 2: 后端 — on_tool_end 检查 ToolMessage status（核心修复）

**Files:**
- Modify: `backend/app/engine/harness_integration/adapters/stream_events.py:155-171`
- Test: `backend/tests/` 下新建或扩展 stream_events 测试

- [ ] **Step 1: 写失败测试 — ToolMessage(status=error) 应产生 ToolResultEvent(status=error)**

在 `backend/tests/` 下找到 stream_events 的现有测试文件（如 `tests/engine/harness_integration/test_stream_events.py`，若不存在则按现有测试目录结构创建）。添加测试：

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from langchain_core.messages import ToolMessage


async def _collect_tool_end_events(tool_message):
    """Helper: 模拟一个 on_tool_end 事件，收集发出的 AppEvent。"""
    from app.engine.harness_integration.adapters.stream_events import stream_events_to_app_events

    events = []

    async def on_event(evt):
        events.append(evt)

    fake_event = {
        "event": "on_tool_end",
        "name": "mcp__github__create_issue",
        "data": {"output": tool_message},
    }

    async def _aiter():
        yield fake_event

    await stream_events_to_app_events(_aiter(), on_event)
    return events


@pytest.mark.asyncio
async def test_tool_end_error_status_propagated():
    """ToolMessage(status=error) 应产生 status=error 的 ToolResultEvent。"""
    error_msg = ToolMessage(
        content="Error executing tool: permission denied",
        name="mcp__github__create_issue",
        tool_call_id="call_1",
        status="error",
    )
    events = await _collect_tool_end_events(error_msg)
    tool_results = [e for e in events if getattr(e, "type", None) == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].status == "error"
    assert "permission denied" in tool_results[0].content


@pytest.mark.asyncio
async def test_tool_end_success_status_default():
    """正常 ToolMessage 应产生 status=success 的 ToolResultEvent。"""
    ok_msg = ToolMessage(
        content="issue created #42",
        name="mcp__github__create_issue",
        tool_call_id="call_1",
    )
    events = await _collect_tool_end_events(ok_msg)
    tool_results = [e for e in events if getattr(e, "type", None) == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].status == "success"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/engine/harness_integration/test_stream_events.py -v 2>&1 | tail -20`
Expected: FAIL — `status` 属性可能存在但当前代码不读它，或者 `status` 字段在 ToolResultEvent 上因为没被赋值而不匹配（取决于具体实现，断言 `status == "error"` 会失败因为当前代码没传 status）

- [ ] **Step 3: 修改 on_tool_end 分支透传 status**

将 `stream_events.py` 第 155-171 行改为：

```python
        elif kind == "on_tool_end":
            output = data.get("output")
            tool_name = event.get("name") or "unknown"
            # Extract content: ToolMessage may stringify with metadata if we
            # naively str() it; use .content when available.
            if output is None:
                content = ""
            elif hasattr(output, "content"):
                content = str(output.content)
            else:
                content = str(output)
            # 检查 ToolMessage 的 status：tool_wrapper 把 ToolException 转成
            # status="error" 的 ToolMessage 回传 LLM。这里同步透传给前端，
            # 让前端能结构化区分工具成功/失败，不再靠正则嗅探文本。
            status = "error" if getattr(output, "status", None) == "error" else "success"
            await on_event(
                ToolResultEvent(
                    tool_name=tool_name,
                    content=content,
                    status=status,
                )
            )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/engine/harness_integration/test_stream_events.py -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/harness_integration/adapters/stream_events.py backend/tests/engine/harness_integration/test_stream_events.py
git commit -m "feat(event): on_tool_end 透传 ToolMessage status 给前端"
```

---

## Task 3: 后端 — 顶层异常走 ErrorEvent schema + 错误来源分类

**Files:**
- Modify: `backend/app/services/agent_execution_service.py:184-188`（stream）、`:257-261`（resume）
- Test: `backend/tests/` 下扩展 agent_execution_service 测试

- [ ] **Step 1: 写失败测试 — 顶层异常应发出带 source 的 ErrorEvent**

在 `backend/tests/` 找到 agent_execution_service 的测试文件（如 `tests/services/test_agent_execution_service.py`）。添加测试验证错误来源分类函数：

```python
from app.services.agent_execution_service import _classify_error_source


def test_classify_llm_error():
    """LLM 限流/欠费类异常归 llm。"""
    class RateLimitError(Exception):
        pass
    exc = RateLimitError("Rate limit exceeded for model gpt-4o")
    assert _classify_error_source(exc) == "llm"


def test_classify_generic_error_as_graph():
    """未识别的异常归 graph。"""
    exc = RuntimeError("unexpected")
    assert _classify_error_source(exc) == "graph"


def test_classify_none_returns_graph():
    """无异常信息时归 graph。"""
    assert _classify_error_source(Exception("")) == "graph"
```

> 注意：`_classify_error_source` 的 LLM 判定逻辑基于异常类型名和消息内容启发式判断。测试中的 `RateLimitError` 类型名需匹配实现中的判定规则，实现时应覆盖主流 LLM 库的异常类型名（如 `RateLimitError`、`APITimeoutError`、`AuthenticationError`、消息含 "quota"/"rate limit"/"insufficient_quota"/"余额不足"/"欠费" 等）。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/services/test_agent_execution_service.py -k classify -v 2>&1 | tail -10`
Expected: FAIL — `_classify_error_source` 尚未定义，ImportError

- [ ] **Step 3: 新增 `_classify_error_source` 辅助函数**

在 `agent_execution_service.py` 文件中（建议放在文件末尾的 helpers 区，`_build_initial_state` 之后）新增：

```python
# LLM 类异常的类型名关键词（主流 LLM 库：openai / anthropic / 通义等）。
# 这些异常在模型欠费、限流、超时、鉴权失败时抛出。
_LLM_ERROR_TYPE_KEYWORDS = (
    "ratelimit", "rate_limit", "apitimeout", "authentication",
    "permissiondenied", "insufficient_quota", "quotaexceeded",
    "connection", "timeout",
)
# 错误消息中的 LLM 类关键词。
_LLM_ERROR_MSG_KEYWORDS = (
    "rate limit", "quota", "insufficient_quota", "余额不足", "欠费",
    "api key", "invalid_api_key", "authentication",
    "model_not_found", "context_length_exceeded",
)


def _classify_error_source(exc: BaseException) -> str:
    """根据异常类型/消息判定错误来源，供前端区分展示。

    返回 "llm"（模型欠费/限流/超时/鉴权）、"tool"（工具加载/配置错误）、
    或 "graph"（其余含服务端内部异常）。

    注意：按"所有错误都暴露给前端"的原则，这里只分类来源，不脱敏——
    顶层异常的 str(exc) 会原样发给前端。
    """
    type_name = type(exc).__name__.lower()
    msg = str(exc).lower()

    # LLM 类异常：匹配类型名或消息关键词
    if any(kw in type_name for kw in _LLM_ERROR_TYPE_KEYWORDS):
        return "llm"
    if any(kw in msg for kw in _LLM_ERROR_MSG_KEYWORDS):
        return "llm"

    # tool / config 类异常（NotFoundError 涉及工具/模型配置时）
    # 目前简化处理：未明确归类的都走 graph
    return "graph"
```

- [ ] **Step 4: 运行分类测试确认通过**

Run: `cd backend && python -m pytest tests/services/test_agent_execution_service.py -k classify -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: 修改 stream 的顶层异常处理（走 ErrorEvent schema）**

将 `agent_execution_service.py` 第 184-188 行：

```python
            except Exception as exc:
                run_error = exc
                logger.error("agent_stream_error", agent_id=agent_id, request_id=request_id, error=str(exc))
                logger.exception("agent_stream_error_traceback")
                await event_queue.put(f"data: {safe_json({'type': 'error', 'content': str(exc)})}\n\n")
                result = {}
```

改为：

```python
            except Exception as exc:
                run_error = exc
                logger.error("agent_stream_error", agent_id=agent_id, request_id=request_id, error=str(exc))
                logger.exception("agent_stream_error_traceback")
                # 走 ErrorEvent schema 保留 source 字段，前端可据此区分
                # 错误来源（llm/tool/graph），不再丢字段。
                from app.engine.harness_integration.adapters.app_event import ErrorEvent

                err_evt = ErrorEvent(
                    message=str(exc),
                    source=_classify_error_source(exc),
                ).model_dump()
                # 前端契约用 content，ErrorEvent 用 message，做字段重映射
                err_evt["content"] = err_evt.pop("message", "")
                await event_queue.put(f"data: {safe_json(err_evt)}\n\n")
                result = {}
```

- [ ] **Step 6: 修改 resume 的顶层异常处理（同样走 ErrorEvent schema）**

将 `agent_execution_service.py` 第 257-261 行：

```python
            except Exception as exc:
                run_error = exc
                logger.error("agent_resume_error", agent_id=agent_id, error=str(exc))
                logger.exception("agent_resume_error_traceback")
                await event_queue.put(f"data: {safe_json({'type': 'error', 'content': str(exc)})}\n\n")
                result = {}
```

改为：

```python
            except Exception as exc:
                run_error = exc
                logger.error("agent_resume_error", agent_id=agent_id, error=str(exc))
                logger.exception("agent_resume_error_traceback")
                from app.engine.harness_integration.adapters.app_event import ErrorEvent

                err_evt = ErrorEvent(
                    message=str(exc),
                    source=_classify_error_source(exc),
                ).model_dump()
                err_evt["content"] = err_evt.pop("message", "")
                await event_queue.put(f"data: {safe_json(err_evt)}\n\n")
                result = {}
```

- [ ] **Step 7: 运行相关测试确认通过**

Run: `cd backend && python -m pytest tests/services/test_agent_execution_service.py -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/agent_execution_service.py backend/tests/services/test_agent_execution_service.py
git commit -m "feat(execution): 顶层异常走 ErrorEvent schema 保留 source 字段"
```

---

## Task 4: 后端 — MCP 工具加载失败不再静默（harness McpToolLoader）

**Files:**
- Modify: `backend/app/engine/harness_integration/context.py:170-193`（Agent 路径 MCP 加载）

> **背景**：Agent 对话路径用 harness 的 `McpToolLoader.load_tools`（loader.py:78-97），它在单个 server 失败时只 `logger.warning` 然后跳过。我们要让它把失败信息暴露给上层。不改 harness 库的 `load_tools` 签名（避免影响其他调用方），而是在 `context.py` 中逐 server 加载并收集错误。

- [ ] **Step 1: 修改 context.py 的 MCP 加载段，逐 server 加载收集错误**

> 注意：当前代码（context.py:170-193）一次性把所有 config 传给 `mcp_loader.load_tools(mcp_configs)`。要收集单个 server 的失败，改为逐个 config 调用 `load_tools([config])`。

在 context.py 函数开头（约第 100 行 `harness_context_resolve_start` log 之后）新增 `load_errors` 列表：

```python
    load_errors: list[dict] = []
```

然后将第 170-193 行的 MCP 加载段：

```python
    # 4. MCP:用 harness McpToolLoader 替换 backend 的 MCP 工具
    mcp_connection_ids = agent.get("mcp_connection_ids") or []
    if mcp_connection_ids:
        from agent_flow_harness import McpConnectionConfig, McpToolLoader

        from app.services.mcp_connection_service import McpConnectionService

        mcp_configs: list[McpConnectionConfig] = []
        for conn_id in mcp_connection_ids:
            conn_doc = await McpConnectionService.get_connection(conn_id)
            if conn_doc:
                mcp_configs.append(McpConnectionConfig(
                    name=conn_doc.get("name", conn_id),
                    url=conn_doc.get("url", ""),
                    protocol=conn_doc.get("protocol", "streamable-http"),
                    auth_type=conn_doc.get("auth_type", "none"),
                    auth_config=conn_doc.get("auth_config") or {},
                    timeout=conn_doc.get("timeout", 30),
                    default_params=conn_doc.get("default_params") or {},
                ))
        if mcp_configs:
            mcp_loader = McpToolLoader()
            mcp_tools = await mcp_loader.load_tools(mcp_configs)
            all_tools.extend(mcp_tools)
```

改为（逐 server 加载，收集失败）：

```python
    # 4. MCP:用 harness McpToolLoader 替换 backend 的 MCP 工具。
    # 逐 server 加载而非一次性全部加载，以便单个 server 失败时收集错误
    # 信息暴露给前端（不再静默跳过）。
    mcp_connection_ids = agent.get("mcp_connection_ids") or []
    if mcp_connection_ids:
        from agent_flow_harness import McpConnectionConfig, McpToolLoader

        from app.services.mcp_connection_service import McpConnectionService

        mcp_loader = McpToolLoader()
        for conn_id in mcp_connection_ids:
            conn_doc = await McpConnectionService.get_connection(conn_id)
            if not conn_doc:
                load_errors.append({
                    "tool_name": f"mcp:{conn_id}",
                    "error": f"MCP 连接 {conn_id} 不存在",
                })
                continue
            config = McpConnectionConfig(
                name=conn_doc.get("name", conn_id),
                url=conn_doc.get("url", ""),
                protocol=conn_doc.get("protocol", "streamable-http"),
                auth_type=conn_doc.get("auth_type", "none"),
                auth_config=conn_doc.get("auth_config") or {},
                timeout=conn_doc.get("timeout", 30),
                default_params=conn_doc.get("default_params") or {},
            )
            try:
                conn_tools = await mcp_loader.load_tools([config])
                all_tools.extend(conn_tools)
            except Exception as exc:
                logger.warning("mcp_connection_load_failed", connection=conn_doc.get("name"), error=str(exc))
                load_errors.append({
                    "tool_name": f"mcp:{conn_doc.get('name', conn_id)}",
                    "error": f"MCP 工具加载失败: {exc}",
                })
```

> 注意：`McpToolLoader.load_tools([config])` 内部仍会 try/except 单个 server（loader.py:80-89）。即使它内部 catch 了不抛出，load_tools 也会返回空列表——这种情况下工具数变少但无错误。为彻底捕获，需确认 `_connect_and_load` 在连接失败时是否抛异常。经查 loader.py:80-81，`_connect_and_load` 在连接失败时会抛异常被外层 try/except 捕获。因此这里改为直接调用 `_connect_and_load` 更准确——但那是私有方法。**实际实现时**：如果 `load_tools([config])` 返回空列表但没有抛异常，说明 loader 内部吞了错误，此时应在 load 后检查 `if not conn_tools` 并补一条 load_error（提示"MCP server 未返回工具"）。

实现时补充（load_tools 返回空但未抛异常的情况）：

```python
            try:
                conn_tools = await mcp_loader.load_tools([config])
                if not conn_tools:
                    load_errors.append({
                        "tool_name": f"mcp:{conn_doc.get('name', conn_id)}",
                        "error": f"MCP server {conn_doc.get('name')} 未返回工具（可能连接失败或无可用工具）",
                    })
                all_tools.extend(conn_tools)
            except Exception as exc:
                ...  # 如上
```

- [ ] **Step 2: 同样收集自定义工具加载失败（context.py:195-214）**

将第 195-214 行的自定义工具加载段：

```python
    # 4.5. 自定义工具 (openapi / code / prebuilt)
    custom_tools = agent.get("custom_tools") or []
    if custom_tools:
        from app.engine.tool.tool_builder import build_tool
        from app.services.tool_service import ToolService

        for binding in custom_tools:
            tool_id = binding.get("tool_id", "")
            user_args = binding.get("user_args", {})
            if not tool_id:
                continue
            docs = await ToolService.get_tools_by_ids([tool_id])
            if not docs:
                continue
            doc = docs[0]
            # 解密 user_args 里的 sensitive 字段
            user_args = _decrypt_user_args(doc, user_args)
            tool = await build_tool(doc, user_args=user_args)
            if tool is not None:
                all_tools.append(tool)
```

改为（收集 build_tool 失败和返回 None 的情况）：

```python
    # 4.5. 自定义工具 (openapi / code / prebuilt)。收集加载失败暴露给前端。
    custom_tools = agent.get("custom_tools") or []
    if custom_tools:
        from app.engine.tool.tool_builder import build_tool
        from app.services.tool_service import ToolService

        for binding in custom_tools:
            tool_id = binding.get("tool_id", "")
            user_args = binding.get("user_args", {})
            if not tool_id:
                continue
            docs = await ToolService.get_tools_by_ids([tool_id])
            if not docs:
                load_errors.append({
                    "tool_name": f"custom:{tool_id}",
                    "error": f"自定义工具 {tool_id} 不存在",
                })
                continue
            doc = docs[0]
            # 解密 user_args 里的 sensitive 字段
            user_args = _decrypt_user_args(doc, user_args)
            try:
                tool = await build_tool(doc, user_args=user_args)
            except Exception as exc:
                logger.warning("custom_tool_build_failed", tool_id=tool_id, error=str(exc))
                load_errors.append({
                    "tool_name": f"custom:{doc.get('name', tool_id)}",
                    "error": f"自定义工具构建失败: {exc}",
                })
                continue
            if tool is None:
                load_errors.append({
                    "tool_name": f"custom:{doc.get('name', tool_id)}",
                    "error": f"自定义工具 {doc.get('name')} 构建返回空",
                })
            else:
                all_tools.append(tool)
```

- [ ] **Step 3: 把 load_errors 加入 hctx 返回值**

将 context.py 第 304-313 行的 return dict：

```python
    return {
        "agent_doc": agent_doc,
        "llm": llm,
        "tools": all_tools,
        "sb_token": sb_token,
        "ws_token": ws_token,
        "ut_token": ut_token,
        "middlewares": [UsageMiddleware()],
        "context_window": context_window,
    }
```

改为（加 load_errors）：

```python
    return {
        "agent_doc": agent_doc,
        "llm": llm,
        "tools": all_tools,
        "load_errors": load_errors,
        "sb_token": sb_token,
        "ws_token": ws_token,
        "ut_token": ut_token,
        "middlewares": [UsageMiddleware()],
        "context_window": context_window,
    }
```

- [ ] **Step 4: 运行相关测试确认不破坏现有行为**

Run: `cd backend && python -m pytest tests/engine/harness_integration/ -v 2>&1 | tail -20`
Expected: PASS（load_errors 是新增字段，不影响现有断言）

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/harness_integration/context.py
git commit -m "feat(context): 收集 MCP/自定义工具加载失败到 load_errors 不再静默"
```

---

## Task 5: 后端 — execution.py 把 load_errors 作为 error 事件发出

**Files:**
- Modify: `backend/app/engine/harness_integration/execution.py:55-76`（stream）、`:196-218`（resume）

- [ ] **Step 1: 修改 stream() 在 graph 执行前发出加载错误**

将 execution.py 第 55-76 行（stream 函数的 try 块，从 `session_id = state.get(...)` 到 `await stream_events_to_app_events(...)` 之后）：

```python
    usage_summary: dict = {}
    try:
        session_id = state.get("session_id", "")
        graph = build_agent_graph(
            hctx["agent_doc"], checkpointer=get_checkpointer(),
            middleware=hctx["middlewares"], tools=hctx["tools"],
        )
        config = build_config(
            hctx["agent_doc"],
            hctx["llm"],
            tools=hctx["tools"],
            context_window=hctx["context_window"],
            middlewares=hctx["middlewares"],
            thread_id=session_id,
        )
        await _maybe_migrate_legacy(graph, config, legacy_records)

        event_stream = graph.astream_events(state, config=config, version="v2")
        await stream_events_to_app_events(
            event_stream,
            _make_event_callback(on_event),
            enable_thinking=enable_thinking,
        )
```

改为（在 graph 执行前先发出 load_errors）：

```python
    usage_summary: dict = {}
    try:
        # 先发出工具/MCP 加载失败，让用户尽早知道哪些工具不可用
        await _emit_load_errors(hctx, on_event)

        session_id = state.get("session_id", "")
        graph = build_agent_graph(
            hctx["agent_doc"], checkpointer=get_checkpointer(),
            middleware=hctx["middlewares"], tools=hctx["tools"],
        )
        config = build_config(
            hctx["agent_doc"],
            hctx["llm"],
            tools=hctx["tools"],
            context_window=hctx["context_window"],
            middlewares=hctx["middlewares"],
            thread_id=session_id,
        )
        await _maybe_migrate_legacy(graph, config, legacy_records)

        event_stream = graph.astream_events(state, config=config, version="v2")
        await stream_events_to_app_events(
            event_stream,
            _make_event_callback(on_event),
            enable_thinking=enable_thinking,
        )
```

- [ ] **Step 2: 同样修改 resume() 发出加载错误**

将 execution.py resume 函数（第 196-218 行）的 try 块开头，在 `session_id = state.get(...)` 之前加：

```python
    try:
        # 先发出工具/MCP 加载失败
        await _emit_load_errors(hctx, on_event)

        session_id = state.get("session_id", "")
        graph = build_agent_graph(...)
        ...
```

- [ ] **Step 3: 新增 _emit_load_errors 辅助函数**

在 execution.py 的 `_make_event_callback` 函数之后（约第 35 行后）新增：

```python
async def _emit_load_errors(hctx, on_event) -> None:
    """把 context 收集的工具/MCP 加载失败作为 error 事件发给前端。

    每条 load_error 发一个 ErrorEvent(source="tool")，前端据此知道
    某个工具因故不可用（如 MCP server 离线、自定义工具构建失败）。
    """
    from app.engine.harness_integration.adapters.app_event import ErrorEvent

    callback = _make_event_callback(on_event)
    for err in hctx.get("load_errors", []):
        evt = ErrorEvent(
            message=f"[{err['tool_name']}] {err['error']}",
            source="tool",
        )
        await callback(evt)
```

- [ ] **Step 4: 运行测试确认不破坏**

Run: `cd backend && python -m pytest tests/engine/harness_integration/ -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/harness_integration/execution.py
git commit -m "feat(execution): 把工具加载失败作为 error 事件发给前端"
```

---

## Task 6: 后端 — mcp_tool_cache.py 失败时 raise（workflow 路径）

**Files:**
- Modify: `backend/app/engine/tool/mcp_tool_cache.py:189-199`

> **背景**：`get_mcp_tools_cached` 被 workflow Tool 节点（`node_executor.py:_execute_mcp_tool`）使用。当前失败时静默 `return []`，导致 workflow 路径报"MCP 工具未在连接中找到"掩盖真正的连接失败根因。改为 raise，由 `node_executor.py:838-866` 的现有 try/except 捕获转成 `NodeResult(success=False)`。

- [ ] **Step 1: 修改 get_mcp_tools_cached 失败时 raise**

将 `mcp_tool_cache.py` 第 189-199 行：

```python
    try:
        client = MultiServerMCPClient(connections, tool_name_prefix=True)
        tools = await client.get_tools()
    except Exception as exc:
        # 提取 ExceptionGroup 的子异常以显示真正原因
        if hasattr(exc, "exceptions"):
            details = "; ".join(str(e) for e in exc.exceptions)  # type: ignore[attr-defined]
            logger.error("mcp_tools_fetch_failed", connection_ids=connection_ids, error=details)
        else:
            logger.error("mcp_tools_fetch_failed", connection_ids=connection_ids, error=str(exc))
        return []
```

改为：

```python
    try:
        client = MultiServerMCPClient(connections, tool_name_prefix=True)
        tools = await client.get_tools()
    except Exception as exc:
        # 提取 ExceptionGroup 的子异常以显示真正原因，然后 raise
        # 让上层（workflow node_executor / agent context.py）捕获并
        # 暴露给前端，不再静默 return [] 掩盖连接失败。
        if hasattr(exc, "exceptions"):
            details = "; ".join(str(e) for e in exc.exceptions)  # type: ignore[attr-defined]
            logger.error("mcp_tools_fetch_failed", connection_ids=connection_ids, error=details)
            raise RuntimeError(f"MCP 连接失败: {details}") from exc
        else:
            logger.error("mcp_tools_fetch_failed", connection_ids=connection_ids, error=str(exc))
            raise
```

- [ ] **Step 2: 确认 workflow node_executor 的现有 try/except 能捕获**

读取 `backend/app/engine/workflow/node_executor.py:838-866` 确认 `_execute_mcp_tool` 有 `except Exception as exc: last_error = ...` 兜底（已有），raise 后会被捕获转成 `NodeResult(success=False, error_message=...)`。无需改动。

Run: `cd backend && python -m pytest tests/engine/workflow/ -k mcp -v 2>&1 | tail -20`
Expected: PASS（现有 workflow MCP 测试应仍通过；若有测试断言 return [] 行为则需更新）

- [ ] **Step 3: Commit**

```bash
git add backend/app/engine/tool/mcp_tool_cache.py
git commit -m "fix(mcp): get_mcp_tools_cached 失败时 raise 不再静默 return []"
```

---

## Task 7: 前端 frontend — QueryClient 加全局 mutations.onError

**Files:**
- Modify: `frontend/src/config/query-client.ts`
- Modify: `frontend/src/main.tsx:4, 8-16`

- [ ] **Step 1: 在 query-client.ts 加全局 mutation onError + extractErrorMessage**

将 `frontend/src/config/query-client.ts` 全文替换为：

```typescript
/**
 * TanStack Query client instance.
 */
import { QueryClient } from '@tanstack/react-query'
import { message } from 'antd'

/** 从未知错误对象提取用户可读的消息。 */
export function extractErrorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    return (err as { message: string }).message
  }
  return '操作失败'
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000, // 30 seconds
      retry: 1,
      refetchOnWindowFocus: false,
    },
    mutations: {
      // 全局兜底：所有 mutation 失败自动 toast，避免遗漏 onError 导致静默。
      // 需要静默的调用点显式覆盖 onError: () => {}。
      onError: (err: unknown) => {
        message.error(extractErrorMessage(err))
      },
    },
  },
})
```

- [ ] **Step 2: main.tsx 改用 config/query-client.ts**

将 `frontend/src/main.tsx` 第 1-16 行：

```typescript
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})
```

改为：

```typescript
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { queryClient } from './config/query-client'
import './index.css'
```

（删除内联创建的 queryClient，改用 import）

- [ ] **Step 3: 检查并清理重复的手动 onError（避免双重 toast）**

在 `frontend/src/` 下搜索同时有 `onError` 且仅做 `message.error(extractErrorMessage)` 无其它逻辑的 mutation。这些会被全局兜底重复触发 toast，应删除它们的 onError（保留有特殊逻辑的，如刷新列表）。

Run: `cd frontend && grep -rn "onError:" src/ --include="*.tsx" --include="*.ts" | grep -i "message.error"`

逐个检查命中行：若 onError 体内仅 `message.error(...)` 且无其它逻辑 → 删掉该 onError（全局兜底已覆盖）；若有其它逻辑（如 invalidate、setState）→ 保留但移除 message.error 行。

- [ ] **Step 4: 构建验证**

Run: `cd frontend && npx tsc --noEmit 2>&1 | tail -20`
Expected: 无类型错误

- [ ] **Step 5: Commit**

```bash
git add frontend/src/config/query-client.ts frontend/src/main.tsx
git commit -m "feat(frontend): QueryClient 加全局 mutations.onError 兜底错误提示"
```

---

## Task 8: 前端 frontend-studio — QueryClient 加全局 mutations.onError

**Files:**
- Modify: `frontend-studio/src/main.tsx:1-17`

- [ ] **Step 1: 在 main.tsx 加全局 mutation onError**

> Studio 没有 config/query-client.ts，直接在 main.tsx 内联（与现有模式一致）。

将 `frontend-studio/src/main.tsx` 第 1-17 行：

```typescript
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import App from './App.tsx';
import {AuthInitializer} from './components/AuthInitializer';
import './index.css';
import '@xyflow/react/dist/style.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
});
```

改为：

```typescript
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import App from './App.tsx';
import {AuthInitializer} from './components/AuthInitializer';
import {toast} from './components/ui/toast';
import './index.css';
import '@xyflow/react/dist/style.css';

/** 从未知错误对象提取用户可读的消息。 */
function extractErrorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    return (err as { message: string }).message;
  }
  return '操作失败';
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
    mutations: {
      // 全局兜底：所有 mutation 失败自动 toast，避免遗漏 onError 导致静默。
      onError: (err: unknown) => {
        toast.error(extractErrorMessage(err), {duration: 0});
      },
    },
  },
});
```

> 注意：`toast` 路径 `./components/ui/toast` 已在项目其他文件中使用（如 WorkflowDesigner.tsx）。`duration: 0` 表示错误 toast 不自动消失，需用户手动关闭（与 WorkflowDesigner 现有错误处理一致）。

- [ ] **Step 2: 检查并清理重复的手动 onError**

Run: `cd frontend-studio && grep -rn "onError:" src/ --include="*.tsx" --include="*.ts" | grep -i "toast.error"`

逐个检查：若 onError 体内仅 `toast.error(...)` 无其它逻辑 → 删掉；有其它逻辑 → 保留但移除 toast.error 行。

> 特别注意 `WorkflowDesigner.tsx` 的 saveMutation/publishMutation/createMutation：它们当前**无 onError**（这正是静默问题所在），加全局兜底后它们会自动 toast，无需额外改动。

- [ ] **Step 3: 构建验证**

Run: `cd frontend-studio && npx tsc --noEmit 2>&1 | tail -20`
Expected: 无类型错误

- [ ] **Step 4: Commit**

```bash
git add frontend-studio/src/main.tsx
git commit -m "feat(studio): QueryClient 加全局 mutations.onError 兜底错误提示"
```

---

## Task 9: 前端 frontend-studio — SSE tool_result 读 status，error 不 throw

**Files:**
- Modify: `frontend-studio/src/services/agent-api.ts:169-173`（ToolResultEvent 类型）、`:188-191`（ErrorEvent 类型）
- Modify: `frontend-studio/src/components/ChatHomepage.tsx:824-872`（tool_result 分支）、`:885-886`（error 分支）

- [ ] **Step 1: 给 ToolResultEvent 和 ErrorEvent 类型加字段**

将 `agent-api.ts` 第 169-173 行：

```typescript
/** Tool returned a result */
export interface ToolResultEvent {
  type: 'tool_result'
  tool_name: string
  content: string
}
```

改为：

```typescript
/** Tool returned a result */
export interface ToolResultEvent {
  type: 'tool_result'
  tool_name: string
  content: string
  status?: 'success' | 'error'
}
```

将 `agent-api.ts` 第 188-191 行：

```typescript
/** Error during execution */
export interface ErrorEvent {
  type: 'error'
  content: string
}
```

改为：

```typescript
/** Error during execution */
export interface ErrorEvent {
  type: 'error'
  content: string
  source?: 'llm' | 'tool' | 'graph'
}
```

- [ ] **Step 2: ChatHomepage tool_result 分支改用 status 字段**

将 `ChatHomepage.tsx` 第 824-826 行：

```typescript
          case 'tool_result': {
            const resultContent = evt.content;
            const isError = /\b(error|fail)/i.test(resultContent);
```

改为：

```typescript
          case 'tool_result': {
            const resultContent = evt.content;
            const isError = evt.status === 'error';
```

（后续 846、865 行的 `isError ? 'error' : 'success'` 无需改动，变量名不变）

- [ ] **Step 3: ChatHomepage error 分支改为不 throw**

将 `ChatHomepage.tsx` 第 885-886 行：

```typescript
          case 'error':
            throw new Error(evt.content);
```

改为：

```typescript
          case 'error': {
            // 不 throw 中断流：把错误记录到 agent 消息，保留 source 供展示。
            // 错误可能来自中途（如某个工具的加载失败），后续仍可能有事件。
            const errContent = evt.content || '执行出错';
            setStreamError(errContent);
            setLiveMessages((prev) =>
              updateMsg(prev, agentMsgId, { status: 'error', content: `❌ ${errContent}` }),
            );
            break;
          }
```

> 注意：这里改为不 throw 后，流会继续处理后续事件。若 error 是致命的（agent 崩溃），后端的 done 事件会随后到达正常结束流。`streamError` 仍被设置用于顶部横幅展示。

- [ ] **Step 4: 构建验证**

Run: `cd frontend-studio && npx tsc --noEmit 2>&1 | tail -20`
Expected: 无类型错误

- [ ] **Step 5: Commit**

```bash
git add frontend-studio/src/services/agent-api.ts frontend-studio/src/components/ChatHomepage.tsx
git commit -m "feat(studio): tool_result 读 status 字段，error 事件不中断流"
```

---

## Task 10: 前端 frontend-client — SSE tool_result 读 status，error 不 throw

**Files:**
- Modify: `frontend-client/src/types.ts:169-181`（StreamEvent）
- Modify: `frontend-client/src/hooks/use-chat.ts:352-391`（tool_result）、`:413-414`（error）

- [ ] **Step 1: 给 StreamEvent 类型加 status 和 source 字段**

将 `types.ts` 第 169-181 行的 StreamEvent 接口：

```typescript
export interface StreamEvent {
  type?: StreamEventType
  done?: true
  content?: string
  tool_name?: string
  args?: Record<string, unknown>
  auto?: boolean
  question?: string
  clarification_type?: string
  context?: string | null
  options?: string[] | null
  interrupt_id?: string
}
```

改为（加 status、source）：

```typescript
export interface StreamEvent {
  type?: StreamEventType
  done?: true
  content?: string
  tool_name?: string
  args?: Record<string, unknown>
  auto?: boolean
  question?: string
  clarification_type?: string
  context?: string | null
  options?: string[] | null
  interrupt_id?: string
  status?: 'success' | 'error'
  source?: 'llm' | 'tool' | 'graph'
}
```

- [ ] **Step 2: use-chat.ts tool_result 分支改用 status 字段**

读取 `use-chat.ts` 第 352-365 行确认当前结构，然后将正则嗅探行（约 357 行）：

```typescript
      const isError = /(^|\b)(error|failed|traceback)(\b|:)/i.test(event.content)
```

改为：

```typescript
      const isError = event.status === 'error'
```

- [ ] **Step 3: use-chat.ts error 分支改为不 throw**

将 `use-chat.ts` 第 413-414 行：

```typescript
      } else if (event.type === 'error') {
        throw new Error(event.content || 'Agent 执行失败')
```

改为：

```typescript
      } else if (event.type === 'error') {
        // 不 throw 中断流：记录错误到当前消息，后续仍可能有事件。
        const errContent = event.content || 'Agent 执行失败'
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, status: 'error', error: errContent }
              : m,
          ),
        )
```

> 注意：`assistantId` 和 `setMessages` 的变量名需与 use-chat.ts 上下文一致。实现时读取该函数确认确切的变量名（可能是 `assistantId`、`agentMsgId` 或其他）。

- [ ] **Step 4: 构建验证**

Run: `cd frontend-client && npx tsc --noEmit 2>&1 | tail -20`
Expected: 无类型错误

- [ ] **Step 5: Commit**

```bash
git add frontend-client/src/types.ts frontend-client/src/hooks/use-chat.ts
git commit -m "feat(client): tool_result 读 status 字段，error 事件不中断流"
```

---

## Task 11: 前端 frontend — chat-panel tool_result 读 status

**Files:**
- Modify: `frontend/src/components/chat-panel.tsx:850, 868`

- [ ] **Step 1: tool_result 分支改用 status 字段替代 startsWith 嗅探**

将 `chat-panel.tsx` 第 850 行：

```typescript
            const isError = e.content?.startsWith('Error')
```

改为：

```typescript
            const isError = e.status === 'error'
```

将第 868 行同样：

```typescript
            const isError = e.content?.startsWith('Error')
```

改为：

```typescript
            const isError = e.status === 'error'
```

> 注意：需确认 `chat-panel.tsx` 中 ToolResultEvent 的类型定义是否也需加 status 字段。搜索该文件或其 import 的类型定义，给 ToolResultEvent 接口加 `status?: 'success' | 'error'`。

- [ ] **Step 2: 确认 ToolResultEvent 类型已加 status 字段**

Run: `cd frontend && grep -n "ToolResultEvent" src/components/chat-panel.tsx src/services/agent-api.ts`

若 `frontend/src/services/agent-api.ts` 有 ToolResultEvent 定义，同样加 `status?: 'success' | 'error'`。若 chat-panel.tsx 内联定义，在其接口加字段。

- [ ] **Step 3: 构建验证**

Run: `cd frontend && npx tsc --noEmit 2>&1 | tail -20`
Expected: 无类型错误

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/chat-panel.tsx
git commit -m "feat(frontend): chat-panel tool_result 读 status 字段替代文本嗅探"
```

---

## Task 12: 前端 frontend-studio — 历史回填渲染 error entry

**Files:**
- Modify: `frontend-studio/src/components/ChatHomepage.tsx:64-137`（agentMessageToDisplay）

- [ ] **Step 1: 读取 agentMessageToDisplay 确认结构**

Run: `cd frontend-studio && sed -n '60,140p' src/components/ChatHomepage.tsx`

阅读 `agentMessageToDisplay` 函数，确认它如何把 timeline entries 转成 Message 对象。找到处理 timeline entry 的 switch/if 分支。

- [ ] **Step 2: 给 agentMessageToDisplay 加 type === 'error' 分支**

在处理 timeline entry 的逻辑中，找到 `type === 'tool_result'` 或 `type === 'text'` 的分支，在其后增加对 `type === 'error'` 的处理：

```typescript
            if (entry.type === 'error') {
              // 历史回填的错误消息：渲染为 error 状态的 agent 消息
              return {
                ...base,
                role: 'agent',
                status: 'error',
                content: `❌ ${entry.content || '执行出错'}`,
              }
            }
```

> 注意：`base` 变量名和返回结构需与该函数实际代码一致。实现时根据 `agentMessageToDisplay` 的真实结构调整。

- [ ] **Step 3: 构建验证**

Run: `cd frontend-studio && npx tsc --noEmit 2>&1 | tail -20`
Expected: 无类型错误

- [ ] **Step 4: Commit**

```bash
git add frontend-studio/src/components/ChatHomepage.tsx
git commit -m "feat(studio): 历史回填渲染 error timeline entry"
```

---

## Task 13: 端到端验证

**Files:** 无（验证任务）

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && python -m pytest tests/ -v --tb=short 2>&1 | tail -30`
Expected: 所有测试 PASS

- [ ] **Step 2: 三个前端构建验证**

Run:
```bash
cd frontend && npx tsc --noEmit 2>&1 | tail -5
cd ../frontend-studio && npx tsc --noEmit 2>&1 | tail -5
cd ../frontend-client && npx tsc --noEmit 2>&1 | tail -5
```
Expected: 三个前端均无类型错误

- [ ] **Step 3: 手动验证场景（需要本地服务运行）**

启动后端 + 前端，逐个验证：

1. **名称重复**：创建重名 Agent → 应自动 toast "名称已存在"（全局兜底）
2. **MCP 工具失败**：配置一个无效 MCP 连接并执行 agent → 工具卡片标红 + 显示加载失败 error 事件
3. **工具执行错误**：调用会抛错的工具 → tool_result 卡片标红（status=error），流不中断
4. **模型欠费**：配置一个无效 API key 的模型执行 → error 事件显示错误，带 source=llm，流不中断
5. **历史回填**：切换到含错误的旧会话 → 错误消息正常显示

- [ ] **Step 4: Commit（如有验证中发现的修复）**

```bash
git add -A
git commit -m "test: 错误可见性改造端到端验证通过"
```

---

## Self-Review 结果

**Spec coverage（对照 spec 各节）：**
- ✅ 第一类机制（工具执行报错写入工具结果）：Task 1（schema）+ Task 2（on_tool_end 透传）
- ✅ 第二类机制-顶层异常（普通接口错误返回前端）：Task 3（ErrorEvent schema + source 分类）
- ✅ 第二类机制-加载失败：Task 4（context 收集 load_errors）+ Task 5（execution 发出 error 事件）
- ✅ mcp_tool_cache 不再静默：Task 6
- ✅ 通道 B REST 全局兜底：Task 7（frontend）+ Task 8（studio）
- ✅ 前端 SSE tool_result 读 status：Task 9（studio）+ Task 10（client）+ Task 11（frontend）
- ✅ 前端 error 不中断流：Task 9（studio）+ Task 10（client）；frontend/chat-panel.tsx 已是不 throw 模式（spec 第 304 行确认），无需改
- ✅ 历史回填渲染 error：Task 12（studio）

**Placeholder scan：** 无 TBD/TODO。Task 12 的 Step 2 代码片段标注了"需根据实际结构调整"，这是因为 agentMessageToDisplay 的完整代码未读取，但给出了明确的模式。Task 10 Step 3 同理标注了变量名需确认。

**Type consistency：**
- `status: Literal["success", "error"]`（后端 Pydantic）与 `status?: 'success' | 'error'`（前端 TS）一致 ✅
- `source: Literal["llm", "tool", "graph"]`（后端）与 `source?: 'llm' | 'tool' | 'graph'`（前端）一致 ✅
- `load_errors` 字段名在 context.py（设置）和 execution.py（读取 `hctx.get("load_errors")`）一致 ✅
- `_emit_load_errors` / `_classify_error_source` / `extractErrorMessage` 命名跨任务一致 ✅

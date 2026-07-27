"""harness execution — stream / invoke / resume entry points.

Each function assembles the harness context (via ``resolve_harness_context``),
builds the graph + config, and executes. ErrorEvent fields are remapped to
match the frontend contract (``message`` → ``content``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.engine.harness_integration.adapters.app_event import AppEvent

from app.engine.harness_integration.context import (
    _maybe_migrate_legacy,
    get_checkpointer,
    release_harness_context,
    resolve_harness_context,
)


def _make_event_callback(on_event):
    """Create an adapter that converts AppEvent → dict and remaps error fields."""

    async def _on_event_dict(app_event: AppEvent) -> None:
        data = app_event.model_dump()
        # ErrorEvent 字段是 {message, source},前端契约用 {content}。
        if data.get("type") == "error":
            data["content"] = data.pop("message", "")
        await on_event(data)

    return _on_event_dict


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


async def stream(
    agent: dict,
    state: dict,
    on_event,
    *,
    enable_thinking: bool = False,
    legacy_records: list[dict] | None = None,
    user_token: str | None = None,
) -> dict:
    """流式执行 harness graph,通过 on_event 推送 AppEvent dict。"""
    from agent_flow_harness import build_agent_graph, build_config

    from app.engine.harness_integration.adapters import stream_events_to_app_events

    hctx = await resolve_harness_context(
        agent, state, enable_thinking=enable_thinking, user_token=user_token,
    )
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
        # Extract token usage before hctx is released
        for mw in hctx["middlewares"]:
            if hasattr(mw, "summary"):
                usage_summary = mw.summary
    finally:
        release_harness_context(hctx)

    return {"step_count": 0, "usage": usage_summary}


async def invoke(
    agent: dict,
    state: dict,
    *,
    enable_thinking: bool = False,
    workspace: Any | None = None,
    legacy_records: list[dict] | None = None,
    cancel_checker: Callable[[], Awaitable[bool]] | None = None,
    user_token: str | None = None,
) -> dict:
    """非流式执行 harness graph(供 invoke 端点 / workflow agent 节点使用)。

    Args:
        cancel_checker: 可选的异步取消检查器。传入后 compress_node 每轮
            REACT 迭代会检查它，返回 True 时 interrupt() 优雅挂起 agent。
        user_token: 外部终端用户 token(回调验证模式),透传给 MCP server。
    """
    from agent_flow_harness import build_agent_graph, build_config

    hctx = await resolve_harness_context(
        agent, state, enable_thinking=enable_thinking, workspace=workspace,
        user_token=user_token,
    )
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
            cancel_checker=cancel_checker,
        )
        await _maybe_migrate_legacy(graph, config, legacy_records)
        result = await graph.ainvoke(state, config=config)
        # Extract token usage before hctx is released
        for mw in hctx["middlewares"]:
            if hasattr(mw, "summary"):
                result["usage"] = mw.summary
        return result
    finally:
        release_harness_context(hctx)


async def resume_agent(
    agent: dict,
    state: dict,
    *,
    thread_id: str,
    resume_value: str = "continue",
    enable_thinking: bool = False,
    workspace: Any | None = None,
    cancel_checker: Callable[[], Awaitable[bool]] | None = None,
) -> dict:
    """恢复被 interrupt() 挂起的 agent（非流式，供工作流恢复使用）。

    用 ``Command(resume=resume_value)`` + 相同 ``thread_id`` 续接 LangGraph
    checkpointer 中的状态，REACT 循环从断点继续，完整上下文（messages /
    tool 结果 / step_count）不丢失。
    """
    from agent_flow_harness import build_agent_graph, build_config
    from langgraph.types import Command

    hctx = await resolve_harness_context(
        agent, state, enable_thinking=enable_thinking, workspace=workspace,
    )
    try:
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
            thread_id=thread_id,
            cancel_checker=cancel_checker,
        )
        return await graph.ainvoke(Command(resume=resume_value), config=config)
    finally:
        release_harness_context(hctx)


async def resume(
    agent: dict,
    state: dict,
    on_event,
    answer: str,
    *,
    enable_thinking: bool = False,
    user_token: str | None = None,
) -> dict:
    """恢复被 interrupt 挂起的 graph,用 Command(resume=answer) 继续。"""
    from agent_flow_harness import build_agent_graph, build_config
    from langgraph.types import Command

    from app.engine.harness_integration.adapters import stream_events_to_app_events

    hctx = await resolve_harness_context(
        agent, state, enable_thinking=enable_thinking, user_token=user_token,
    )
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

        event_stream = graph.astream_events(
            Command(resume=answer), config=config, version="v2",
        )
        await stream_events_to_app_events(
            event_stream,
            _make_event_callback(on_event),
            enable_thinking=enable_thinking,
        )
        # Extract token usage before hctx is released
        for mw in hctx["middlewares"]:
            if hasattr(mw, "summary"):
                usage_summary = mw.summary
    finally:
        release_harness_context(hctx)

    return {"step_count": 0, "usage": usage_summary}

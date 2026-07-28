"""Middleware bridge for the native ``langgraph.prebuilt.ToolNode``.

``ToolNode`` accepts an ``awrap_tool_call`` interceptor that receives every
tool call before execution. This module builds such an interceptor from a
harness :class:`~agent_flow_harness.middleware.chain.MiddlewareChain`, wiring
the ``run_before_tool`` / ``run_after_tool`` hooks without giving up the
native node's error handling, concurrency, and command support.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.types import Command

    from agent_flow_harness.middleware.chain import MiddlewareChain

logger = structlog.get_logger(__name__)


def make_tool_wrapper(
    chain: MiddlewareChain,
) -> Callable[
    [ToolCallRequest, Callable[[ToolCallRequest], Awaitable["ToolMessage | Command[Any]"]]],  # noqa: UP006
    Awaitable["ToolMessage | Command[Any]"],  # noqa: UP006
]:
    """Create an ``awrap_tool_call`` that runs middleware around tool execution.

    Args:
        chain: The middleware chain whose ``run_before_tool`` /
            ``run_after_tool`` hooks should fire on each tool call.

    Returns:
        An async wrapper compatible with ``ToolNode(awrap_tool_call=...)``.
    """

    async def awrap(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], Awaitable["ToolMessage | Command[Any]"]],  # noqa: UP006
    ) -> "ToolMessage | Command[Any]":
        state: Any = request.state
        tc: dict[str, Any] = dict(request.tool_call)

        # before_tool — middleware may observe / modify the call args.
        tc = await chain.run_before_tool(state, tc)

        # Re-inject any middleware modifications into the request.
        modified = request.override(tool_call=tc)  # type: ignore[arg-type]

        # Execute via the native ToolNode (handles errors, concurrency).
        #
        # ToolException（工具业务失败）转成 error ToolMessage 返回给 LLM，
        # 让模型据此决定下一步（重试 / 换工具 / 转告用户），而不是 re-raise
        # 终止整个 agent 流。MCP adapter 在 MCP ``isError=true`` 时抛的正是
        # ToolException（langchain_mcp_adapters/tools.py），langchain 工具的
        # 业务校验失败也用它。
        #
        # 为何在这里处理而非用 ToolNode(handle_tool_errors=...)：langgraph 1.2.4
        # 的 ToolNode 在配了 awrap_tool_call 时，_arun_one 外层 except Exception
        # 不检查 handled_types，会把 GraphInterrupt（HITL ask_clarification 用的
        # interrupt()）也吞成 error ToolMessage，破坏人机协同。在此用
        # ``except ToolException`` 精确捕获：GraphInterrupt 不是 ToolException
        # 子类，会正确冒泡挂起 graph；其它系统异常（KeyError 等）也不被吞，
        # 由上层 agent_execution_service 的兜底 except 处理。文案与旧 react
        # 引擎 (engine/react.py:218) 一致。
        try:
            result = await execute(modified)
        except ToolException as exc:
            logger.warning(
                "tool_execution_failed",
                tool_name=tc.get("name", ""),
                error=str(exc),
            )
            result = ToolMessage(
                content=f"Error executing tool: {exc}",
                name=tc.get("name", ""),
                tool_call_id=tc.get("id", ""),
                status="error",
            )

        # after_tool — middleware observes the result content.
        result_content = result.content if isinstance(result, ToolMessage) else ""
        await chain.run_after_tool(state, tc, str(result_content))

        return result

    return awrap


__all__ = ["make_tool_wrapper"]

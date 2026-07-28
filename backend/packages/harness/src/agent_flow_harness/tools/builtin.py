"""内建工具汇总 — harness 自带的三层能力型/文件shell 工具清单。

本模块集中定义 ``BUILTIN_TOOLS``（name → BaseTool 实例），作为"能力清单"
供应用层按需取用。注意：``BUILTIN_TOOLS`` 本身只是一个注册表，**不会**被
自动注入到任何 Agent —— 实际注入哪些内建工具，由应用层
``app.engine.harness_integration.context.resolve_harness_context`` 按 Agent 的
``builtin_config`` 白名单（opt-in）决定。

三层工具模型（SPEC §Always）：
- 第一层（能力型）：delegate_to_subagent / ask_clarification / tool_search
- 第二层（文件/shell）：bash / read / write / glob / grep（委托 Sandbox）

注意：这是"能力清单"，不是"领域工具"。第三层领域工具（查 MES/发邮件等）
由用户通过 TOOL_REGISTRY.register 或 use 字符串注入，不在此处。

BUILTIN_TOOLS 通过模块 __getattr__ 延迟构建（PEP 562），避免 import 时
触发 tools↔sandbox/subagents/interaction 循环依赖。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

_CACHE: dict[str, Any] | None = None


def _load_builtin_tools() -> dict[str, "BaseTool"]:
    """加载所有内建工具，返回 name → BaseTool 字典。

    延迟 import：sandbox/subagents/interaction 在首次调用时才 import，
    此时 harness 包已完全初始化，无循环依赖。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    tools: dict[str, BaseTool] = {}

    # 第二层：文件/shell 工具（委托 Sandbox）
    from agent_flow_harness.sandbox.tools import bash, glob, grep, read, write

    for t in (bash, read, write, glob, grep):
        tools[t.name] = t

    # 第一层：能力型工具
    from agent_flow_harness.interaction import ask_clarification, tool_search
    from agent_flow_harness.subagents.delegate import delegate_to_subagent

    for t in (delegate_to_subagent, ask_clarification, tool_search):
        tools[t.name] = t

    _CACHE = tools
    return tools


def __getattr__(name: str) -> Any:
    """PEP 562 延迟导出 BUILTIN_TOOLS / BUILTIN_TOOL_NAMES。"""
    if name == "BUILTIN_TOOLS":
        return _load_builtin_tools()
    if name == "BUILTIN_TOOL_NAMES":
        return frozenset(_load_builtin_tools().keys())
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["BUILTIN_TOOLS", "BUILTIN_TOOL_NAMES"]  # noqa: F822 (via __getattr__)

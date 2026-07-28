"""Interaction 模块 — 第一层能力型内建工具 (v0.2-x 三层工具模型第一层)。

ask_clarification: HITL 追问，工具直调 langgraph interrupt() 挂起执行，
  宿主 resume 时传用户答案，工具返回答案给 LLM。
tool_search: 按关键词检索可用工具，读 TOOL_REGISTRY 单例。

设计见 docs/implementation-artifacts/v0-2-x-tool-registry-enhancement.md。
"""
from agent_flow_harness.interaction.clarification import (
    ClarificationField,
    ask_clarification,
)
from agent_flow_harness.interaction.tool_search import tool_search

__all__ = ["ask_clarification", "ClarificationField", "tool_search"]

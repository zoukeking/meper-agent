"""MCP 模块 — MCP 工具连接与加载（v0.2 增强）。

harness 提供 McpToolLoader，从连接配置加载 MCP 工具（langchain-mcp-adapters）。
应用层从 DB 读出连接配置传给 harness，harness 负责连接/缓存/工具名前缀。

langchain-mcp-adapters 为可选依赖（未安装时 load_tools 报 ImportError）。

终端用户 token 透传：宿主在 agent 执行前 set_user_token_context(token)，
loader 的 interceptor 会读取并在调用 MCP 时覆盖 Authorization header。
"""
from agent_flow_harness.mcp.loader import McpConnectionConfig, McpToolLoader
from agent_flow_harness.mcp.user_token_context import (
    get_user_token_context,
    reset_user_token_context,
    set_user_token_context,
)

__all__ = [
    "McpConnectionConfig",
    "McpToolLoader",
    "get_user_token_context",
    "reset_user_token_context",
    "set_user_token_context",
]

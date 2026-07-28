"""User-token ContextVar — MCP 工具调用时透传终端用户 token。

宿主在每次 Agent 执行前 set_user_token_context(token)，MCP 工具的
interceptor 内部 get_user_token_context() 读取，放进 Authorization
header 透传给 MCP server。ContextVar 保证异步任务隔离。

设计：token 仅在 ContextVar 生命周期内（单次请求）存在，不写入
Redis、不写日志、不入库。未设置（兼容模式或平台用户调用）时返回
None，loader 此时应使用 MCP connection 的静态凭证。
"""
from __future__ import annotations

import contextvars

_user_token_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_user_token", default=None
)


def set_user_token_context(token: str | None) -> "contextvars.Token[str | None]":
    """设置当前请求的 user_token。返回 Token 用于 reset。"""
    return _user_token_ctx.set(token)


def reset_user_token_context(token: "contextvars.Token[str | None]") -> None:
    """恢复到 set 之前的状态（用 set 返回的 Token）。"""
    _user_token_ctx.reset(token)


def get_user_token_context() -> str | None:
    """读取当前请求的 user_token。未设置返回 None（兼容模式/平台用户）。"""
    return _user_token_ctx.get()


__all__ = [
    "set_user_token_context",
    "reset_user_token_context",
    "get_user_token_context",
]

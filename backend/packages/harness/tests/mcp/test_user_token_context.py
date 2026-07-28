"""user_token ContextVar 测试 — set/get/reset 三函数。"""
from __future__ import annotations

from agent_flow_harness.mcp.user_token_context import (
    get_user_token_context,
    reset_user_token_context,
    set_user_token_context,
)


def test_default_is_none():
    """未设置时 get 返回 None（兼容模式 / 平台用户）。"""
    assert get_user_token_context() is None


def test_set_then_get_returns_same_token():
    """set 后 get 拿到设置的 token。"""
    token = set_user_token_context("user-token-abc")
    try:
        assert get_user_token_context() == "user-token-abc"
    finally:
        reset_user_token_context(token)
    assert get_user_token_context() is None


def test_set_none_explicitly():
    """set(None) 等价于显式标记为兼容模式（与默认无差异，但 token 合法）。"""
    token = set_user_token_context(None)
    try:
        assert get_user_token_context() is None
    finally:
        reset_user_token_context(token)


def test_reset_restores_previous_value():
    """reset 后恢复到 set 之前的值（嵌套场景）。"""
    outer = set_user_token_context("outer")
    try:
        inner = set_user_token_context("inner")
        assert get_user_token_context() == "inner"
        reset_user_token_context(inner)
        # reset 后恢复到 outer
        assert get_user_token_context() == "outer"
    finally:
        reset_user_token_context(outer)
    assert get_user_token_context() is None


def test_overwrite_without_reset_leaks():
    """直接覆盖（不 reset）会污染后续——这是 ContextVar 的语义，文档化在测试里。"""
    set_user_token_context("leaked")
    # 没有 reset,本测试结束后值还在 ContextVar 里。
    # 之所以列出这个测试是为了提醒:每次 set 必须配 reset,
    # 否则会污染同 context 的后续异步任务。
    assert get_user_token_context() == "leaked"
    # 手动清理,避免污染其它测试
    import contextvars

    contextvars.ContextVar("mcp_user_token").set(None)

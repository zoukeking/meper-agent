"""user_token interceptor 测试 — 验证 MCP 调用时 Authorization header 覆盖逻辑。

不连真实 MCP server，直接构造 fake MCPToolCallRequest + fake handler，
验证 interceptor 在两种模式下的行为:
- 有 user_token: 覆盖 Authorization 为 Bearer {user_token}
- 无 user_token (兼容模式/平台用户): 透传 request,不改 headers
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from agent_flow_harness.mcp.loader import _user_token_interceptor
from agent_flow_harness.mcp.user_token_context import (
    reset_user_token_context,
    set_user_token_context,
)


@dataclass
class _FakeRequest:
    """模拟 langchain-mcp-adapters 的 MCPToolCallRequest。"""

    name: str = "query_order"
    args: dict = None
    server_name: str = "partner"
    headers: dict = None

    def override(self, **updates):
        # 模拟 MCPToolCallRequest.override:返回新的 request,
        # 替换指定字段(其它字段保持引用相同)。
        return _FakeRequest(
            name=self.name,
            args=self.args,
            server_name=self.server_name,
            headers=updates.get("headers", self.headers),
        )


@pytest.fixture(autouse=True)
def _reset_token():
    reset_user_token_context(set_user_token_context(None))
    yield
    reset_user_token_context(set_user_token_context(None))


class TestUserTokenInterceptor:
    async def test_no_user_token_passes_through(self):
        """无 user_token (兼容模式) → 不修改 request,透传给 handler。"""
        captured: list = []

        async def handler(req):
            captured.append(req)
            return {"ok": True}

        req = _FakeRequest(headers={"Authorization": "Bearer static-tok"})
        result = await _user_token_interceptor(req, handler)

        assert result == {"ok": True}
        assert len(captured) == 1
        # handler 收到的就是原 request（未 override）
        assert captured[0] is req
        # Authorization 没被改
        assert captured[0].headers == {"Authorization": "Bearer static-tok"}

    async def test_with_user_token_overrides_authorization(self):
        """有 user_token → 覆盖 Authorization 为 Bearer {user_token}。"""
        set_user_token_context("user-abc-123")
        captured: list = []

        async def handler(req):
            captured.append(req)
            return {"ok": True}

        req = _FakeRequest(headers={"Authorization": "Bearer static-tok"})
        await _user_token_interceptor(req, handler)

        assert len(captured) == 1
        # override 后的 request headers 被 replaced
        assert captured[0].headers == {"Authorization": "Bearer user-abc-123"}

    async def test_with_user_token_overrides_even_when_no_static_header(self):
        """有 user_token → 即便 connection 没静态凭证也注入 Authorization。"""
        set_user_token_context("user-xyz")
        captured: list = []

        async def handler(req):
            captured.append(req)
            return {"ok": True}

        req = _FakeRequest(headers=None)
        await _user_token_interceptor(req, handler)

        assert captured[0].headers == {"Authorization": "Bearer user-xyz"}

    async def test_with_user_token_handler_called_once(self):
        """interceptor 不会重复调 handler,也不会跳过。"""
        set_user_token_context("user-1")
        handler = AsyncMock(return_value={"done": True})

        await _user_token_interceptor(_FakeRequest(), handler)

        handler.assert_awaited_once()

    async def test_no_user_token_handler_called_once(self):
        """无 user_token 时 handler 也恰好调一次。"""
        handler = AsyncMock(return_value={"done": True})

        await _user_token_interceptor(_FakeRequest(), handler)

        handler.assert_awaited_once()

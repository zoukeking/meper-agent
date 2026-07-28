"""MCP 工具加载器 — 从连接配置加载 MCP 工具。

harness 提供 McpToolLoader，接收连接配置（不含 DB 依赖），连接 MCP server，
返回 StructuredTool 列表。应用层从 DB 读出连接配置传给 harness。

用法：
    config = McpConnectionConfig(name="github", url="https://...", protocol="streamable-http")
    loader = McpToolLoader()
    tools = await loader.load_tools([config])
    # tools 是 StructuredTool 列表，名格式 mcp__github__create_issue
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import structlog

from agent_flow_harness.mcp.user_token_context import get_user_token_context

if TYPE_CHECKING:
    from langchain_core.tools import StructuredTool

logger = structlog.get_logger(__name__)

_MCP_PREFIX = "mcp__"
_DEFAULT_CACHE_TTL = 300  # 5 分钟


@dataclass
class McpConnectionConfig:
    """MCP 连接配置（应用层从 DB 读出后传入）。"""

    name: str
    url: str
    protocol: str = "streamable-http"  # "streamable-http" / "sse"
    auth_type: str = "none"  # none / api_key / bearer_token / basic
    auth_config: dict[str, Any] = field(default_factory=dict)
    timeout: int = 30
    default_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class _CacheEntry:
    tools: list["StructuredTool"]
    timestamp: float

    def is_expired(self, ttl: int) -> bool:
        return time.time() - self.timestamp > ttl


class McpToolLoader:
    """MCP 工具加载器（连接 + 缓存 + 工具名前缀）。

    给连接配置列表，连接 MCP server，返回 StructuredTool 列表。
    工具名格式：``mcp__{server_name}__{tool_name}``（对齐 Claude Code）。
    结果按 frozenset(config_names) 缓存，TTL 默认 5 分钟。
    """

    def __init__(self, cache_ttl: int = _DEFAULT_CACHE_TTL) -> None:
        self._cache: dict[frozenset[str], _CacheEntry] = {}
        self._cache_ttl = cache_ttl

    async def load_tools(
        self, configs: list[McpConnectionConfig]
    ) -> list["StructuredTool"]:
        """连接 MCP server，返回工具列表（带缓存）。"""
        if not configs:
            return []

        cache_key = frozenset(c.name for c in configs)
        cached = self._cache.get(cache_key)
        if cached is not None and not cached.is_expired(self._cache_ttl):
            logger.debug("mcp_tools_cache_hit", key=sorted(cache_key))
            return cached.tools

        tools: list[StructuredTool] = []
        for config in configs:
            try:
                conn_tools = await self._connect_and_load(config)
                tools.extend(conn_tools)
            except Exception as exc:
                logger.warning(
                    "mcp_connection_failed",
                    server=config.name,
                    url=config.url,
                    error=str(exc),
                )

        self._cache[cache_key] = _CacheEntry(tools=tools, timestamp=time.time())
        logger.info(
            "mcp_tools_loaded",
            servers=sorted(cache_key),
            tool_count=len(tools),
        )
        return tools

    def invalidate(self, server_name: str | None = None) -> None:
        """失效缓存。传 server_name 清含该 server 的条目，不传清全部。"""
        if server_name is None:
            self._cache.clear()
            return
        keys_to_remove = [
            k for k in self._cache if server_name in k
        ]
        for k in keys_to_remove:
            del self._cache[k]

    async def _connect_and_load(
        self, config: McpConnectionConfig
    ) -> list["StructuredTool"]:
        """连接单个 MCP server，返回工具列表（已加前缀）。"""

        from langchain_mcp_adapters.client import MultiServerMCPClient  # type: ignore[import-not-found]

        connection = self._build_connection(config)
        # tool_interceptors 在每次工具调用时执行：把当前请求的
        # user_token（若有）覆盖到 Authorization header，实现 per-user
        # 身份透传。token 从 ContextVar 动态读，不烘进工具闭包。
        client = MultiServerMCPClient(
            {config.name: connection},
            tool_name_prefix=True,
            tool_interceptors=[_user_token_interceptor],
        )
        raw_tools = await client.get_tools()

        # 加 mcp__ 前缀 + 注入 default_params
        result: list[StructuredTool] = []
        for tool in raw_tools:
            renamed = _rename_with_prefix(tool, config.name)
            if config.default_params:
                renamed = _inject_defaults(renamed, config.default_params)
            result.append(renamed)
        return result

    @staticmethod
    def _build_connection(config: McpConnectionConfig) -> dict[str, Any]:
        """构造 langchain-mcp-adapters 的连接配置 dict。"""
        from datetime import timedelta

        headers = _build_auth_headers(config)

        if config.protocol == "sse":
            return {
                "transport": "sse",
                "url": config.url,
                "headers": headers,
                "timeout": float(config.timeout),
            }
        # 默认 streamable-http
        return {
            "transport": "http",
            "url": config.url,
            "headers": headers,
            "timeout": timedelta(seconds=config.timeout),
        }


def _build_auth_headers(config: McpConnectionConfig) -> dict[str, str]:
    """根据 auth_type 构造 HTTP headers。"""
    if config.auth_type == "none" or not config.auth_config:
        return {}

    headers: dict[str, str] = {}
    if config.auth_type == "api_key":
        header_name = config.auth_config.get("header_name", "X-API-Key")
        headers[header_name] = config.auth_config.get("api_key", "")
    elif config.auth_type == "bearer_token":
        headers["Authorization"] = f"Bearer {config.auth_config.get('token', '')}"
    elif config.auth_type == "basic":
        import base64

        user = config.auth_config.get("username", "")
        pwd = config.auth_config.get("password", "")
        cred = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    return headers


# ---------------------------------------------------------------------------
# Tool-call interceptor — 透传 user_token
# ---------------------------------------------------------------------------

# langchain-mcp-adapters 的 interceptor Protocol：
#   async def interceptor(request, handler) -> result
# 其中 request.headers 可被 override，adapter 内部会把 override 的
# headers 合并到 connection.headers 上再发起请求。我们用这个机制
# 在每次工具调用时把当前请求的 user_token 注入 Authorization header，
# 覆盖 connection 配置里的静态凭证。
#
# 设计：token 从 ContextVar 动态读取（不烘进工具闭包），所以工具
# 实例的缓存 key 仍按 connection 维度，跨用户共享工具实例安全。


async def _user_token_interceptor(
    request: Any,
    handler: Callable[[Any], Awaitable[Any]],
) -> Any:
    """Inject current user_token into MCP call headers (overrides static).

    - 有 user_token：覆盖 Authorization 为 Bearer {user_token}
    - 无 user_token（兼容模式/平台用户）：透传，使用 connection 的静态凭证
    """
    user_token = get_user_token_context()
    if not user_token:
        return await handler(request)

    # adapter 的 MCPToolCallRequest.override 会保留其它字段，
    # 仅替换 headers；merge 由 adapter 的 execute_tool 完成。
    overridden = request.override(headers={"Authorization": f"Bearer {user_token}"})
    return await handler(overridden)


def _rename_with_prefix(
    tool: "StructuredTool", server_name: str
) -> "StructuredTool":
    """给工具名加 mcp__{server}__ 前缀（如果还没有）。"""
    old_name = tool.name
    if old_name.startswith(_MCP_PREFIX) or old_name.startswith("mcp_"):
        return tool

    # langchain-mcp-adapters 的 tool_name_prefix=True 生成 "{server}_{tool}"
    # 尝试剥离 server 前缀
    bare = old_name
    if old_name.startswith(f"{server_name}_"):
        bare = old_name[len(server_name) + 1 :]

    new_name = f"{_MCP_PREFIX}{server_name}__{bare}"
    return _clone_tool(tool, new_name)


def _clone_tool(tool: "StructuredTool", new_name: str) -> "StructuredTool":
    """用新名字重建 StructuredTool。"""
    from langchain_core.tools import StructuredTool

    return StructuredTool(
        name=new_name,
        description=tool.description,
        args_schema=tool.args_schema,
        func=tool.func,
        coroutine=tool.coroutine,
        response_format=getattr(tool, "response_format", "content"),
    )


def _inject_defaults(
    tool: "StructuredTool", defaults: dict[str, Any]
) -> "StructuredTool":
    """给工具注入 default_params（烘焙进 args_schema 默认值）。"""
    if not defaults:
        return tool

    from langchain_core.tools import StructuredTool

    old_schema = tool.args_schema
    if old_schema is None or not isinstance(old_schema, type):
        return tool

    # 动态子类化 args_schema，注入默认值
    try:
        new_schema = type(
            f"{old_schema.__name__}WithDefaults",
            (old_schema,),
            {k: v for k, v in defaults.items()},
        )
    except Exception:
        return tool  # 注入失败则不注入

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=new_schema,
        func=tool.func,
        coroutine=tool.coroutine,
        response_format=getattr(tool, "response_format", "content"),
    )


__all__ = ["McpConnectionConfig", "McpToolLoader"]

"""In-memory cache for MCP StructuredTool objects.

Caches the result of ``MultiServerMCPClient.get_tools()`` keyed by the
frozenset of MCP connection IDs.  Each cache entry has a configurable
TTL (default 5 minutes).  Tools held in the cache are safe to reuse —
each ``StructuredTool`` internally stores the connection config and
creates a fresh session on every invocation.

Cache invalidation is triggered by:
- TTL expiry (automatic)
- Explicit ``invalidate_cache(connection_id)`` when a connection is
  updated, deleted, or its tools are re-discovered.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool
from loguru import logger

# Default TTL in seconds (5 minutes)
_DEFAULT_TTL = 300


@dataclass
class _CacheEntry:
    """A single cache entry with expiry metadata."""

    tools: list[StructuredTool]
    created_at: float
    ttl: float

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > self.ttl


class McpToolCache:
    """Process-level singleton cache for MCP tools.

    Key = ``frozenset`` of MCP connection IDs.
    Value = ``_CacheEntry`` holding the resolved ``StructuredTool`` list.
    """

    def __init__(self, default_ttl: float = _DEFAULT_TTL) -> None:
        self._store: dict[frozenset[str], _CacheEntry] = {}
        self._default_ttl = default_ttl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, connection_ids: frozenset[str]) -> list[StructuredTool] | None:
        """Return cached tools for *connection_ids*, or ``None`` on miss / expiry."""
        entry = self._store.get(connection_ids)
        if entry is None:
            return None
        if entry.is_expired:
            del self._store[connection_ids]
            logger.debug("mcp_tool_cache_expired", key=_key_repr(connection_ids))
            return None
        return entry.tools

    def set(
        self,
        connection_ids: frozenset[str],
        tools: list[StructuredTool],
        ttl: float | None = None,
    ) -> None:
        """Store tools for *connection_ids* with optional TTL override."""
        effective_ttl = ttl if ttl is not None else self._default_ttl
        self._store[connection_ids] = _CacheEntry(
            tools=tools,
            created_at=time.monotonic(),
            ttl=effective_ttl,
        )
        logger.debug(
            "mcp_tool_cache_set",
            key=_key_repr(connection_ids),
            tool_count=len(tools),
            ttl=effective_ttl,
        )

    def invalidate(self, connection_id: str) -> int:
        """Invalidate all cache entries that include *connection_id*.

        Returns the number of entries removed.
        """
        removed = 0
        keys_to_remove = [
            key for key in self._store if connection_id in key
        ]
        for key in keys_to_remove:
            del self._store[key]
            removed += 1

        if removed:
            logger.debug(
                "mcp_tool_cache_invalidated",
                connection_id=connection_id,
                entries_removed=removed,
            )
        return removed

    def clear(self) -> None:
        """Clear all cached entries."""
        count = len(self._store)
        self._store.clear()
        if count:
            logger.debug("mcp_tool_cache_cleared", entries_cleared=count)

    @property
    def size(self) -> int:
        """Number of active cache entries."""
        return len(self._store)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_cache = McpToolCache()


def get_cache() -> McpToolCache:
    """Return the process-level ``McpToolCache`` singleton."""
    return _cache


async def get_mcp_tools_cached(
    connection_ids: list[str],
) -> list[StructuredTool]:
    """Resolve MCP tools with caching.

    Returns from cache on hit (and not expired).  On miss, fetches via
    ``MultiServerMCPClient``, stores in cache, then returns.

    If a connection has ``default_params``, the tools are wrapped to
    automatically merge those defaults on every invocation (user args
    override defaults).
    """
    if not connection_ids:
        return []

    key = frozenset(connection_ids)
    cache = get_cache()

    # Cache hit
    cached = cache.get(key)
    if cached is not None:
        logger.debug(
            "mcp_tool_cache_hit",
            key=_key_repr(key),
            tool_count=len(cached),
        )
        return cached

    # Cache miss — resolve from MCP servers
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from app.engine.tool.mcp_client import _build_connection_config
    from app.services.mcp_connection_service import McpConnectionService

    connections: dict[str, dict] = {}
    # Track per-connection default_params keyed by connection name
    conn_default_params: dict[str, dict] = {}
    for conn_id in connection_ids:
        conn_doc = await McpConnectionService.get_connection(conn_id)
        if conn_doc is None:
            logger.warning("mcp_connection_not_found", connection_id=conn_id)
            continue
        name = conn_doc.get("name", conn_id)
        connections[name] = _build_connection_config(
            url=conn_doc["url"],
            protocol=conn_doc.get("protocol", "streamable-http"),
            auth_type=conn_doc.get("auth_type", "none"),
            auth_config=conn_doc.get("auth_config", {}),
            timeout=conn_doc.get("timeout", 30),
        )
        dp = conn_doc.get("default_params", {})
        if dp:
            conn_default_params[name] = dp

    if not connections:
        return []

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

    # Rename from library format "{server}_{tool}" to "mcp__{server}__{tool}"
    # matching Claude Code's MCP tool naming convention.
    server_names = set(connections.keys())
    tools = [
        _rename_tool_to_mcp_prefix(tool, server_names)
        for tool in tools
    ]

    # Wrap tools with default_params if any connection has them
    if conn_default_params:
        tools = [
            _wrap_tool_with_defaults(tool, conn_default_params)
            for tool in tools
        ]

    # Store in cache
    cache.set(key, tools)
    return tools


def invalidate_cache(connection_id: str) -> int:
    """Convenience wrapper — invalidate cache entries for a connection ID."""
    return get_cache().invalidate(connection_id)


def _rename_tool_to_mcp_prefix(tool: StructuredTool, server_names: set[str]) -> StructuredTool:
    """Rename a tool from ``{server}_{tool}`` to ``mcp__{server}__{tool}``.

    The library's ``tool_name_prefix=True`` gives us ``github_search``.
    This function renames it to ``mcp__github__search`` (matching Claude Code).
    """
    original_name = tool.name

    # Skip if already in mcp__ format
    if original_name.startswith("mcp__"):
        return tool
    if original_name.startswith("mcp_"):
        return tool

    # Find which server this tool belongs to by matching the prefix
    for sn in server_names:
        prefix = f"{sn}_"
        if original_name.startswith(prefix):
            bare_name = original_name[len(prefix):]
            prefixed_name = f"mcp__{sn}__{bare_name}"

            original_func = tool.func
            original_coroutine = tool.coroutine

            def _sync(
                _func: Any = original_func,
                **kwargs: Any,
            ) -> Any:
                return _func(**kwargs)

            async def _async(
                _coro: Any = original_coroutine,
                _func: Any = original_func,
                **kwargs: Any,
            ) -> Any:
                if _coro is not None:
                    return await _coro(**kwargs)
                return _func(**kwargs)

            return StructuredTool.from_function(
                func=_sync,
                coroutine=_async,
                name=prefixed_name,
                description=tool.description,
                args_schema=tool.args_schema,
                response_format=tool.response_format,
            )

    # No matching server prefix found — return unchanged
    return tool


def _wrap_tool_with_defaults(
    tool: StructuredTool,
    conn_default_params: dict[str, dict],
) -> StructuredTool:
    """Wrap a StructuredTool so that ``default_params`` are merged on invoke.

    Strategy: create a new ``args_schema`` whose field defaults already
    contain the injected ``default_params``.  This way LangChain's own
    validation fills them in correctly, and user-supplied values still
    take precedence (they are explicit and override the schema default).
    """
    # Flatten all default_params into a single dict
    merged_defaults: dict[str, Any] = {}
    for dp in conn_default_params.values():
        merged_defaults.update(dp)

    if not merged_defaults:
        return tool

    # Build a new args_schema with default_params baked into field defaults
    new_schema = _inject_schema_defaults(tool.args_schema, merged_defaults)

    original_func = tool.func
    original_coroutine = tool.coroutine

    def _wrapped_sync(**kwargs: Any) -> Any:
        return original_func(**kwargs)

    async def _wrapped_async(**kwargs: Any) -> Any:
        if original_coroutine is not None:
            return await original_coroutine(**kwargs)
        return original_func(**kwargs)

    return StructuredTool.from_function(
        func=_wrapped_sync,
        coroutine=_wrapped_async,
        name=tool.name,
        description=tool.description,
        args_schema=new_schema,
        response_format=tool.response_format,
    )


def _inject_schema_defaults(
    schema_cls: type,
    defaults: dict[str, Any],
) -> type:
    """Create a subclass of *schema_cls* with field defaults from *defaults*.

    Only fields that exist in the schema and are present in *defaults*
    will have their defaults overridden.
    """
    if schema_cls is None:
        return schema_cls

    # Collect field overrides
    field_overrides: dict[str, Any] = {}
    for field_name, _field_info in schema_cls.model_fields.items():
        if field_name in defaults:
            field_overrides[field_name] = defaults[field_name]

    if not field_overrides:
        return schema_cls

    # Create a dynamic subclass with updated defaults
    return type(
        schema_cls.__name__,
        (schema_cls,),
        {
            "__annotations__": getattr(schema_cls, "__annotations__", {}),
            **field_overrides,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _key_repr(key: frozenset[str]) -> str:
    """Human-readable representation of a cache key."""
    return ",".join(sorted(key))

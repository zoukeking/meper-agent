"""MCP connection business logic — CRUD, testing, tool discovery, cascade delete."""
from __future__ import annotations

import re

from loguru import logger

from app.core.errors import ConflictError, NotFoundError
from app.db.mongodb import get_database
from app.engine.tool import mcp_client
from app.engine.tool.mcp_tool_cache import invalidate_cache as _invalidate_mcp_cache
from app.models.base import generate_id, utc_now
from app.models.mcp_connection import ConnectionStatus

# Sensitive keys in auth_config that should be encrypted at rest.
_SENSITIVE_AUTH_KEYS = {"api_key", "token", "password", "bearer_token", "secret"}


def _encrypt_auth_config(auth_config: dict) -> dict:
    """Encrypt sensitive fields in auth_config before storage."""
    if not auth_config or auth_config.get("auth_type", "none") == "none":
        return auth_config
    from app.core.crypto import encrypt_secret

    encrypted = {}
    for k, v in auth_config.items():
        if k in _SENSITIVE_AUTH_KEYS and v:
            encrypted[k] = encrypt_secret(str(v))
        else:
            encrypted[k] = v
    return encrypted


def _decrypt_auth_config(auth_config: dict) -> dict:
    """Decrypt sensitive fields in auth_config after retrieval.

    Falls back to plaintext if decryption fails (backward compat with
    pre-encryption data).
    """
    if not auth_config:
        return auth_config
    from app.core.crypto import CryptoError, decrypt_secret

    decrypted = {}
    for k, v in auth_config.items():
        if k in _SENSITIVE_AUTH_KEYS and v and isinstance(v, str):
            try:
                decrypted[k] = decrypt_secret(v)
            except (CryptoError, Exception):
                # Pre-encryption data stored as plaintext — use as-is
                decrypted[k] = v
        else:
            decrypted[k] = v
    return decrypted


# Sentinel value used by _mask_auth_config to redact secrets in API
# responses. Updates carrying this value (or an empty string) are treated
# as "unchanged" so the real secret is preserved (see _merge_auth_config).
_MASKED_VALUE = "***"


def _merge_auth_config(existing: dict | None, incoming: dict | None) -> dict:
    """Field-level merge of auth_config on update.

    The API redacts sensitive fields (api_key / token / password) to
    ``"***"`` in responses. Before this merge existed, an edit that round-
    tripped that masked value would overwrite the real secret with the
    literal string ``"***"``. We now start from the stored (decrypted)
    config and only apply fields the caller actually changed, skipping
    masked placeholders and empty strings.

    Args:
        existing: Stored auth_config (already decrypted). ``None`` → ``{}``.
        incoming: auth_config from the update request. ``None`` means the
            caller did not send the field at all → keep everything as-is.

    Returns:
        Merged auth_config dict (caller re-encrypts sensitive fields).
    """
    if incoming is None:
        return dict(existing or {})
    merged = dict(existing or {})
    for k, v in incoming.items():
        if v == _MASKED_VALUE or v == "":
            continue  # unchanged / cleared-intent not supported → keep old value
        merged[k] = v
    return merged


class McpConnectionService:
    """Service layer for MCP connection operations."""

    COLLECTION = "mcp_connections"

    @staticmethod
    def _collection():
        return get_database()[McpConnectionService.COLLECTION]

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def create_connection(data: dict) -> dict:
        """Create a new MCP connection.

        Args:
            data: Connection fields (name, url, protocol, auth_type, etc.).

        Returns:
            Created MongoDB document.

        Raises:
            ConflictError: If name is not unique.
        """
        col = McpConnectionService._collection()

        existing = await col.find_one({"name": data["name"]})
        if existing is not None:
            raise ConflictError(
                code="MCP_CONN_NAME_CONFLICT",
                message=f"MCP 连接名称 '{data['name']}' 已被占用",
                details={"field": "name"},
            )

        now_iso = utc_now().isoformat()
        doc = {
            "_id": generate_id("mcp"),
            "name": data["name"],
            "description": data.get("description", ""),
            "url": data["url"],
            "protocol": data.get("protocol", "streamable-http"),
            "auth_type": data.get("auth_type", "none"),
            "auth_config": _encrypt_auth_config(data.get("auth_config", {})),
            "timeout": data.get("timeout", 30),
            "default_params": data.get("default_params", {}),
            "status": ConnectionStatus.DISCONNECTED.value,
            "status_message": "",
            "last_connected_at": "",
            "tool_count": 0,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        try:
            await col.insert_one(doc)
        except Exception as exc:
            from pymongo.errors import DuplicateKeyError

            if isinstance(exc, DuplicateKeyError):
                raise ConflictError(
                    code="MCP_CONN_NAME_CONFLICT",
                    message=f"MCP 连接名称 '{data['name']}' 已被占用",
                ) from exc
            raise

        logger.info("mcp_connection_created", connection_id=doc["_id"], name=doc["name"])
        return doc

    @staticmethod
    async def get_connection(connection_id: str) -> dict | None:
        """Get an MCP connection by ID (auth_config decrypted)."""
        doc = await McpConnectionService._collection().find_one({"_id": connection_id})
        if doc and doc.get("auth_config"):
            doc["auth_config"] = _decrypt_auth_config(doc["auth_config"])
        return doc

    @staticmethod
    async def list_connections(
        page: int = 1,
        page_size: int = 20,
        name: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict], int]:
        """List MCP connections with pagination and optional filtering.

        Returns:
            Tuple of (connection_docs, total_count).
        """
        col = McpConnectionService._collection()
        filter_query: dict = {}
        if name:
            filter_query["name"] = {"$regex": re.escape(name), "$options": "i"}
        if status:
            filter_query["status"] = status

        total = await col.count_documents(filter_query)
        cursor = (
            col.find(filter_query)
            .sort("updated_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        items = await cursor.to_list(length=page_size)
        # Decrypt auth_config for each item
        for item in items:
            if item.get("auth_config"):
                item["auth_config"] = _decrypt_auth_config(item["auth_config"])
        return items, total

    @staticmethod
    async def update_connection(connection_id: str, data: dict) -> dict | None:
        """Update an MCP connection (full PUT).

        Args:
            connection_id: The connection ID.
            data: New field values.

        Returns:
            Updated document, or None if not found.

        Raises:
            ConflictError: If new name conflicts with another connection.
        """
        col = McpConnectionService._collection()

        existing = await col.find_one({"_id": connection_id})
        if existing is None:
            return None

        # Check name uniqueness if name is changing
        new_name = data.get("name")
        if new_name and new_name != existing["name"]:
            dup = await col.find_one({"name": new_name, "_id": {"$ne": connection_id}})
            if dup:
                raise ConflictError(
                    code="MCP_CONN_NAME_CONFLICT",
                    message=f"MCP 连接名称 '{new_name}' 已被占用",
                )

        now_iso = utc_now().isoformat()

        set_fields = {
            "name": data.get("name", existing["name"]),
            "description": data.get("description", existing.get("description", "")),
            "url": data.get("url", existing["url"]),
            "protocol": data.get("protocol", existing.get("protocol", "streamable-http")),
            "auth_type": data.get("auth_type", existing.get("auth_type", "none")),
            "auth_config": _encrypt_auth_config(
                # existing 来自 DB 是加密态，必须先解密再合并，否则保留下来的
                # 旧密文会被 _encrypt_auth_config 二次加密，读取时解密失败。
                _merge_auth_config(
                    _decrypt_auth_config(existing.get("auth_config", {})),
                    data.get("auth_config"),
                )
            ),
            "timeout": data.get("timeout", existing.get("timeout", 30)),
            "default_params": data.get("default_params", existing.get("default_params", {})),
            "updated_at": now_iso,
        }

        await col.update_one({"_id": connection_id}, {"$set": set_fields})

        logger.info("mcp_connection_updated", connection_id=connection_id)
        _invalidate_mcp_cache(connection_id)
        return await McpConnectionService.get_connection(connection_id)

    @staticmethod
    async def delete_connection(connection_id: str) -> bool:
        """Delete an MCP connection and cascade-remove its MCP tools.

        Returns:
            True if deleted, False if not found.
        """
        col = McpConnectionService._collection()

        existing = await col.find_one({"_id": connection_id})
        if existing is None:
            return False

        # Cascade: remove all tools from this connection
        tools_col = get_database()["tools"]
        result = await tools_col.delete_many({"mcp_connection_id": connection_id})
        removed_tools = result.deleted_count

        await col.delete_one({"_id": connection_id})

        # 清理 agents.mcp_connection_ids 中的残留引用
        try:
            await get_database()["agents"].update_many(
                {"mcp_connection_ids": connection_id},
                {"$pull": {"mcp_connection_ids": connection_id}},
            )
        except Exception as exc:
            logger.warning(
                "mcp_agent_refs_cleanup_partial",
                connection_id=connection_id,
                error=str(exc),
            )

        logger.info(
            "mcp_connection_deleted",
            connection_id=connection_id,
            cascaded_tools=removed_tools,
        )
        _invalidate_mcp_cache(connection_id)
        return True

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    @staticmethod
    async def test_connection(connection_id: str) -> dict:
        """Test an MCP connection and update its status.

        Returns:
            Test result dict with success, server_info, tool_count, error.
        """
        col = McpConnectionService._collection()
        doc = await col.find_one({"_id": connection_id})
        if doc is None:
            raise NotFoundError(
                code="MCP_CONN_NOT_FOUND",
                message=f"MCP 连接 {connection_id} 不存在",
            )

        now_iso = utc_now().isoformat()

        # Update status to connecting
        await col.update_one(
            {"_id": connection_id},
            {"$set": {
                "status": ConnectionStatus.CONNECTING.value,
                "status_message": "正在连接...",
                "updated_at": now_iso,
            }},
        )

        result = await mcp_client.test_connection(
            url=doc["url"],
            protocol=doc.get("protocol", "streamable-http"),
            auth_type=doc.get("auth_type", "none"),
            auth_config=doc.get("auth_config", {}),
            timeout=doc.get("timeout", 30),
        )

        if result["success"]:
            await col.update_one(
                {"_id": connection_id},
                {"$set": {
                    "status": ConnectionStatus.CONNECTED.value,
                    "status_message": "连接成功",
                    "last_connected_at": now_iso,
                    "tool_count": result["tool_count"],
                    "updated_at": now_iso,
                }},
            )
        else:
            await col.update_one(
                {"_id": connection_id},
                {"$set": {
                    "status": ConnectionStatus.ERROR.value,
                    "status_message": result["error"][:500],
                    "updated_at": now_iso,
                }},
            )

        return result

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    @staticmethod
    async def discover_tools(connection_id: str) -> dict:
        """Discover tools from an MCP server and sync to tools collection.

        Returns:
            Discover result dict.
        """
        col = McpConnectionService._collection()
        doc = await col.find_one({"_id": connection_id})
        if doc is None:
            raise NotFoundError(
                code="MCP_CONN_NOT_FOUND",
                message=f"MCP 连接 {connection_id} 不存在",
            )

        try:
            discovered = await mcp_client.discover_tools(doc)
        except Exception as exc:
            # Unwrap asyncio.TaskGroup's ExceptionGroup so the API
            # response carries the real underlying error instead of
            # the opaque "unhandled errors in a TaskGroup (N sub-exception)".
            if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                error_msg = " | ".join(
                    f"{type(s).__name__}: {s}" for s in exc.exceptions
                )
            else:
                error_msg = f"{type(exc).__name__}: {exc}"
            return {
                "connection_id": connection_id,
                "discovered": 0,
                "created": 0,
                "updated": 0,
                "deactivated": 0,
                "tools": [],
                "error": error_msg,
            }

        tools_col = get_database()["tools"]
        now_iso = utc_now().isoformat()

        created_count = 0
        updated_count = 0
        discovered_names: set[str] = set()
        tool_names: list[str] = []

        for tool_info in discovered:
            discovered_names.add(tool_info["name"])
            tool_names.append(tool_info["name"])

            # Check if tool already exists for this connection
            existing = await tools_col.find_one({
                "mcp_connection_id": connection_id,
                "name": tool_info["name"],
            })

            if existing:
                # Update schema
                await tools_col.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {
                        "description": tool_info["description"],
                        "input_schema": tool_info["input_schema"],
                        "output_schema": tool_info["output_schema"],
                        "instructions": tool_info["instructions"],
                        "updated_at": now_iso,
                    }},
                )
                updated_count += 1
            else:
                # Create new tool
                tool_doc = {
                    "_id": generate_id("tool"),
                    "name": tool_info["name"],
                    "description": tool_info["description"],
                    "input_schema": tool_info["input_schema"],
                    "output_schema": tool_info["output_schema"],
                    "instructions": tool_info["instructions"],
                    "source": "mcp",
                    "source_file": "",
                    "mcp_connection_id": connection_id,
                    "version": 1,
                    "tags": [],
                    "files": [],
                    "created_at": now_iso,
                    "updated_at": now_iso,
                }
                await tools_col.insert_one(tool_doc)
                created_count += 1

        # Remove tools that no longer exist on the server
        deactivate_count = 0
        async for existing_tool in tools_col.find({
            "mcp_connection_id": connection_id,
            "source": "mcp",
        }):
            if existing_tool["name"] not in discovered_names:
                await tools_col.delete_one({"_id": existing_tool["_id"]})
                deactivate_count += 1

        # Update connection status
        await col.update_one(
            {"_id": connection_id},
            {"$set": {
                "status": ConnectionStatus.CONNECTED.value,
                "status_message": "连接成功",
                "tool_count": len(discovered),
                "last_connected_at": now_iso,
                "updated_at": now_iso,
            }},
        )

        logger.info(
            "mcp_tools_synced",
            connection_id=connection_id,
            created=created_count,
            updated=updated_count,
            deactivated=deactivate_count,
        )

        _invalidate_mcp_cache(connection_id)

        return {
            "connection_id": connection_id,
            "discovered": len(discovered),
            "created": created_count,
            "updated": updated_count,
            "deactivated": deactivate_count,
            "tools": tool_names,
            "error": "",
        }

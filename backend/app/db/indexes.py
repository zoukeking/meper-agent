"""MongoDB index management.

Creates indexes for all collections. Called during app startup or via scripts.
"""
from loguru import logger

from app.db.mongodb import get_database


async def create_indexes() -> None:
    """Create all MongoDB indexes required by the application."""
    db = get_database()
    logger.info("Creating MongoDB indexes on database: {}", db.name)

    # Users collection (Story 1.2)
    # _id is auto-indexed by MongoDB (stores our ULID IDs)
    await db.users.create_index("username", unique=True, name="idx_users_username")
    await db.users.create_index("email", unique=True, name="idx_users_email")
    logger.info("Created indexes: idx_users_username, idx_users_email")

    # Agents collection (Story 2.1)
    await db.agents.create_index("name", unique=True, name="idx_agents_name")
    await db.agents.create_index("status", name="idx_agents_status")
    logger.info("Created indexes: idx_agents_name, idx_agents_status")

    # Models collection (Model management)
    await db.models.create_index(
        "model_id", unique=True, name="idx_models_model_id"
    )
    await db.models.create_index("status", name="idx_models_status")
    logger.info("Created indexes: idx_models_model_id, idx_models_status")

    # Tools collection (Story 5.1 — Skill data model)
    await db.tools.create_index("name", unique=True, name="idx_tools_name")
    await db.tools.create_index("status", name="idx_tools_status")
    await db.tools.create_index("source", name="idx_tools_source")
    await db.tools.create_index("mcp_connection_id", name="idx_tools_mcp_conn_id")
    logger.info("Created indexes: idx_tools_name, idx_tools_status, idx_tools_source, idx_tools_mcp_conn_id")

    # Knowledge Bases collection (Markdown KB metadata)
    # name is NOT unique — KB uses _id (kb_ prefix) as the directory name, not name.
    await db.knowledge_bases.create_index("status", name="idx_kb_status")
    await db.knowledge_bases.create_index("owner_user_id", name="idx_kb_owner")
    logger.info("Created indexes: idx_kb_status, idx_kb_owner")

    # MCP connections collection (Story 5.3 — MCP connection management)
    await db.mcp_connections.create_index("name", unique=True, name="idx_mcp_conn_name")
    await db.mcp_connections.create_index("status", name="idx_mcp_conn_status")
    logger.info("Created indexes: idx_mcp_conn_name, idx_mcp_conn_status")

    # File refs collection (Story 10.1 — File management)
    await db.file_refs.create_index(
        [("owner_user_id", 1), ("created_at", -1)],
        name="idx_file_refs_owner_created",
    )
    await db.file_refs.create_index(
        [("sha256", 1)],
        name="idx_file_refs_sha256",
    )
    await db.file_refs.create_index(
        [("status", 1)],
        name="idx_file_refs_status",
    )
    logger.info("Created indexes: idx_file_refs_owner_created, idx_file_refs_sha256, idx_file_refs_status")

    # File usages collection (Story 10.1 — File management)
    await db.file_usages.create_index(
        [("file_id", 1)],
        name="idx_file_usages_file_id",
    )
    await db.file_usages.create_index(
        [("consumer_kind", 1), ("consumer_id", 1)],
        name="idx_file_usages_consumer",
    )
    await db.file_usages.create_index(
        [("file_id", 1), ("consumer_kind", 1), ("consumer_id", 1)],
        name="uq_file_usages_unique",
        unique=True,
    )
    logger.info("Created indexes: idx_file_usages_file_id, idx_file_usages_consumer, uq_file_usages_unique")

    # Notifications collection
    await db.notifications.create_index(
        [("user_id", 1), ("created_at", -1)],
        name="idx_notifications_user_created",
    )
    await db.notifications.create_index(
        [("user_id", 1), ("read", 1)],
        name="idx_notifications_user_read",
    )
    logger.info("Created indexes: idx_notifications_user_created, idx_notifications_user_read")

    # ── Channels ──
    await db.channel_configs.create_index(
        "owner_user_id", name="idx_channel_configs_owner"
    )
    await db.channel_configs.create_index(
        "agent_id", name="idx_channel_configs_agent"
    )
    await db.channel_configs.create_index(
        [("provider", 1), ("name", 1)], name="idx_channel_configs_provider_name"
    )
    await db.inbound_event_logs.create_index(
        [("channel_id", 1), ("platform_message_id", 1)],
        name="uq_inbound_logs_channel_msg",
        unique=True,
    )
    await db.inbound_event_logs.create_index(
        [("status", 1), ("created_at", 1)], name="idx_inbound_logs_status_time"
    )
    logger.info("Created indexes: idx_channel_configs_owner, idx_channel_configs_agent, idx_channel_configs_provider_name, uq_inbound_logs_channel_msg, idx_inbound_logs_status_time")


if __name__ == "__main__":
    import asyncio

    asyncio.run(create_indexes())

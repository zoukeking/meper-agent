"""Harness MongoDB checkpointer configuration.

Shared between the FastAPI lifespan and the Celery worker init so both
processes configure the harness checkpointer identically.
"""
from __future__ import annotations

from loguru import logger

from app.core.config import settings


def configure_mongo_checkpointer(*, log_on_error: bool = True) -> bool:
    """Configure harness checkpointer to use MongoDB.

    On success, thread state persists across restarts. On failure, the
    harness falls back to its default ``MemorySaver`` (in-process only).

    Args:
        log_on_error: If True (default), log a warning/error on failure.
            The FastAPI lifespan passes ``False`` to preserve the
            historical silent-fallback behavior; the Celery worker passes
            ``True`` for visibility.

    Returns:
        True if configured successfully, False if fell back to MemorySaver.
    """
    try:
        from agent_flow_harness import build_mongo_saver, configure_checkpointer

        from app.db.mongodb import get_mongodb_client

        saver = build_mongo_saver(
            client=get_mongodb_client().delegate,
            db_name=settings.MONGODB_DB_NAME,
        )
        configure_checkpointer(saver, overwrite=True)
        logger.debug("checkpointer_configured", db=settings.MONGODB_DB_NAME)
        return True
    except Exception as exc:
        if log_on_error:
            logger.warning("checkpointer_config_failed_fallback_to_memory", error=str(exc))
        return False

"""Base service for MongoDB-backed services.

Eliminates the ``_collection()`` boilerplate duplicated across 14+ services.
Services inherit this base and define a ``COLLECTION`` class attribute;
``_collection()`` / ``find_by_id()`` / ``count_documents()`` / ``delete_by_id()``
are provided generically.

Design: all methods are ``@staticmethod`` to match the existing convention
(services have no instance state and are called as ``XService.method()``).
"""
from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorCollection

from app.db.mongodb import get_database


class BaseMongoService:
    """Base for MongoDB-backed services with static access pattern.

    Subclasses must set ``COLLECTION``:
        class AgentService(BaseMongoService):
            COLLECTION = "agents"
    """

    COLLECTION: str = ""

    @staticmethod
    def _collection() -> AsyncIOMotorCollection:
        """Return the MongoDB collection for this service."""
        return get_database()[BaseMongoService.COLLECTION]

    @classmethod
    def _col(cls) -> AsyncIOMotorCollection:
        """Return the collection using the subclass's COLLECTION attribute."""
        return get_database()[cls.COLLECTION]

    @classmethod
    async def find_by_id(cls, doc_id: str) -> dict[str, Any] | None:
        """Find a document by its ``_id``."""
        return await cls._col().find_one({"_id": doc_id})

    @classmethod
    async def count_documents(cls, query: dict) -> int:
        """Count documents matching a query."""
        return await cls._col().count_documents(query)

    @classmethod
    async def delete_by_id(cls, doc_id: str) -> bool:
        """Delete a document by ``_id``. Returns True if deleted."""
        result = await cls._col().delete_one({"_id": doc_id})
        return result.deleted_count > 0

"""API Key business logic — CRUD, key generation, verification."""
from __future__ import annotations

import secrets
from datetime import UTC

import bcrypt
from loguru import logger

from app.core.api_key_cache import (
    cache_api_key_doc,
    get_cached_api_key_doc,
    invalidate_api_key_cache,
)
from app.core.errors import ConflictError, ValidationError
from app.db.mongodb import get_database
from app.models.api_key import ALL_SCOPES, ApiKey, ApiKeyBindings, ApiKeyStatus
from app.models.base import utc_now

# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------

_KEY_PREFIX = "af_live_"


def _generate_raw_key() -> str:
    """Generate a random 32-char URL-safe token."""
    return secrets.token_urlsafe(24)


def _make_full_key(raw: str) -> str:
    return f"{_KEY_PREFIX}{raw}"


def _hash_key(full_key: str) -> str:
    """bcrypt hash of the full key value."""
    return bcrypt.hashpw(full_key.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_key(full_key: str, key_hash: str) -> bool:
    """Verify a full key against its bcrypt hash."""
    return bcrypt.checkpw(full_key.encode("utf-8"), key_hash.encode("utf-8"))


def _extract_prefix(full_key: str) -> str:
    """Return the first 12 characters as the lookup prefix."""
    return full_key[:12]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ApiKeyService:
    """Service layer for API Key operations."""

    COLLECTION = "api_keys"

    @staticmethod
    def _collection():
        return get_database()[ApiKeyService.COLLECTION]

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------

    @staticmethod
    async def ensure_indexes() -> None:
        """Create indexes for efficient lookups."""
        col = ApiKeyService._collection()
        await col.create_index("key_prefix", name="idx_apikeys_prefix")
        await col.create_index("owner_user_id", name="idx_apikeys_owner")
        await col.create_index("status", name="idx_apikeys_status")
        logger.info("ApiKey indexes ensured")

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    @staticmethod
    async def create_api_key(
        name: str,
        owner_user_id: str,
        scopes: list[str],
        bindings: dict | None = None,
        rate_limit: int = 60,
        expires_at: str | None = None,
        user_info_url: str = "",
    ) -> tuple[dict, str]:
        """Create a new API Key.

        Returns:
            Tuple of (api_key_doc, raw_full_key).
            The full key is returned ONLY at creation time.

        Raises:
            ValidationError: If any scope is invalid.
            ConflictError: If name already exists for this owner.
        """
        # Validate scopes
        invalid = set(scopes) - set(ALL_SCOPES)
        if invalid:
            raise ValidationError(
                code="APIKEY_INVALID_SCOPES",
                message=f"无效的权限: {', '.join(sorted(invalid))}",
                details={"invalid_scopes": sorted(invalid)},
            )

        # Name uniqueness per owner
        col = ApiKeyService._collection()
        existing = await col.find_one({
            "name": name,
            "owner_user_id": owner_user_id,
            "status": ApiKeyStatus.ACTIVE.value,
        })
        if existing is not None:
            raise ConflictError(
                code="APIKEY_NAME_CONFLICT",
                message=f"API Key 名称 '{name}' 已存在",
            )

        # Generate key
        raw = _generate_raw_key()
        full_key = _make_full_key(raw)
        key_hash = _hash_key(full_key)
        key_prefix = _extract_prefix(full_key)

        bindings_model = ApiKeyBindings(**(bindings or {}))

        api_key = ApiKey(
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            owner_user_id=owner_user_id,
            scopes=scopes,
            bindings=bindings_model,
            rate_limit=rate_limit,
            expires_at=expires_at,
            user_info_url=user_info_url,
        )

        doc = {
            "_id": api_key.id,
            "name": api_key.name,
            "key_hash": api_key.key_hash,
            "key_prefix": api_key.key_prefix,
            "owner_user_id": api_key.owner_user_id,
            "scopes": api_key.scopes,
            "bindings": api_key.bindings.model_dump(),
            "rate_limit": api_key.rate_limit,
            "status": api_key.status.value,
            "expires_at": api_key.expires_at,
            "last_used_at": api_key.last_used_at,
            "user_info_url": api_key.user_info_url,
            "created_at": api_key.created_at,
            "updated_at": api_key.updated_at,
        }

        await col.insert_one(doc)

        # Populate cache (store doc without key_hash for security)
        cache_doc = {k: v for k, v in doc.items() if k != "key_hash"}
        await cache_api_key_doc(key_prefix, cache_doc)

        logger.info("api_key_created", api_key_id=api_key.id, name=name)
        return doc, full_key

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @staticmethod
    async def get_api_key(api_key_id: str) -> dict | None:
        """Get an API Key by ID. Returns doc or None."""
        return await ApiKeyService._collection().find_one({"_id": api_key_id})

    @staticmethod
    async def list_api_keys(
        owner_user_id: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        """List API Keys with pagination and optional filtering.

        Args:
            owner_user_id: Optional filter by owner.
            status: Optional status filter.
            page: Page number (1-based).
            page_size: Items per page.

        Returns:
            Tuple of (api_key_docs, total_count).
        """
        col = ApiKeyService._collection()
        filter_query: dict = {}
        if owner_user_id:
            filter_query["owner_user_id"] = owner_user_id
        if status:
            filter_query["status"] = status

        total = await col.count_documents(filter_query)
        cursor = (
            col.find(filter_query, {"key_hash": 0})
            .sort("created_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        items = await cursor.to_list(length=page_size)
        return items, total

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    @staticmethod
    async def update_api_key(
        api_key_id: str,
        name: str | None = None,
        scopes: list[str] | None = None,
        bindings: dict | None = None,
        rate_limit: int | None = None,
        expires_at: str | None = None,
        user_info_url: str | None = None,
    ) -> dict | None:
        """Update an API Key's configuration.

        Returns:
            Updated doc, or None if not found.

        Raises:
            ValidationError: If any scope is invalid.
            ConflictError: If name conflicts.
        """
        col = ApiKeyService._collection()
        doc = await col.find_one({"_id": api_key_id})
        if doc is None:
            return None

        set_fields: dict = {"updated_at": utc_now().isoformat()}

        if name is not None:
            # Check name uniqueness (exclude self)
            conflict = await col.find_one({
                "name": name,
                "_id": {"$ne": api_key_id},
                "owner_user_id": doc["owner_user_id"],
                "status": ApiKeyStatus.ACTIVE.value,
            })
            if conflict is not None:
                raise ConflictError(
                    code="APIKEY_NAME_CONFLICT",
                    message=f"API Key 名称 '{name}' 已存在",
                )
            set_fields["name"] = name

        if scopes is not None:
            invalid = set(scopes) - set(ALL_SCOPES)
            if invalid:
                raise ValidationError(
                    code="APIKEY_INVALID_SCOPES",
                    message=f"无效的权限: {', '.join(sorted(invalid))}",
                )
            set_fields["scopes"] = scopes

        if bindings is not None:
            set_fields["bindings"] = ApiKeyBindings(**bindings).model_dump()

        if rate_limit is not None:
            set_fields["rate_limit"] = rate_limit

        if expires_at is not None:
            set_fields["expires_at"] = expires_at

        if user_info_url is not None:
            set_fields["user_info_url"] = user_info_url

        await col.update_one({"_id": api_key_id}, {"$set": set_fields})

        # Invalidate cache since bindings/scopes may have changed
        await invalidate_api_key_cache(doc["key_prefix"])

        logger.info("api_key_updated", api_key_id=api_key_id)
        return await ApiKeyService.get_api_key(api_key_id)

    # ------------------------------------------------------------------
    # Revoke (soft delete)
    # ------------------------------------------------------------------

    @staticmethod
    async def revoke_api_key(api_key_id: str) -> dict | None:
        """Revoke an API Key (set status to revoked).

        Returns:
            Updated doc, or None if not found.
        """
        col = ApiKeyService._collection()
        doc = await col.find_one({"_id": api_key_id})
        if doc is None:
            return None

        now_iso = utc_now().isoformat()
        await col.update_one(
            {"_id": api_key_id},
            {"$set": {"status": ApiKeyStatus.REVOKED.value, "updated_at": now_iso}},
        )

        await invalidate_api_key_cache(doc["key_prefix"])

        logger.info("api_key_revoked", api_key_id=api_key_id)
        return await ApiKeyService.get_api_key(api_key_id)

    # ------------------------------------------------------------------
    # Verify (for authentication)
    # ------------------------------------------------------------------

    @staticmethod
    async def verify_key(full_key: str) -> dict | None:
        """Verify an API Key and return the document if valid.

        Returns:
            API Key document (without key_hash) if valid, None otherwise.
        """
        key_prefix = _extract_prefix(full_key)

        # Try cache first
        cached = await get_cached_api_key_doc(key_prefix)
        if cached is not None:
            # Cache hit — still need to verify hash against DB
            doc = await ApiKeyService._collection().find_one({"_id": cached["_id"]})
            if doc is not None and _verify_key(full_key, doc["key_hash"]):
                if not _is_key_valid(doc):
                    return None
                # Update last_used_at (fire and forget)
                _update_last_used(doc["_id"])
                # Return doc without key_hash
                return {k: v for k, v in doc.items() if k != "key_hash"}

        # Cache miss — full DB lookup
        doc = await ApiKeyService._collection().find_one({"key_prefix": key_prefix})
        if doc is None:
            return None

        if not _verify_key(full_key, doc["key_hash"]):
            return None

        if not _is_key_valid(doc):
            return None

        # Populate cache
        cache_doc = {k: v for k, v in doc.items() if k != "key_hash"}
        await cache_api_key_doc(key_prefix, cache_doc)

        # Update last_used_at
        _update_last_used(doc["_id"])

        return {k: v for k, v in doc.items() if k != "key_hash"}


def _is_key_valid(doc: dict) -> bool:
    """Check if an API Key document is valid (active + not expired)."""
    if doc.get("status") != ApiKeyStatus.ACTIVE.value:
        return False
    expires_at = doc.get("expires_at")
    if expires_at is not None:
        from datetime import datetime
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp <= utc_now():
                return False
        except (ValueError, TypeError):
            return False
    return True


def _update_last_used(api_key_id: str) -> None:
    """Update last_used_at timestamp (best-effort, no await in caller)."""
    import asyncio
    import contextlib

    async def _do():
        with contextlib.suppress(Exception):
            await ApiKeyService._collection().update_one(
                {"_id": api_key_id},
                {"$set": {"last_used_at": utc_now().isoformat()}},
            )

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do())
    except RuntimeError:
        pass

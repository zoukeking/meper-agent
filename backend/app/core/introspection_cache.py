"""Redis cache layer for user-token introspection results.

Caches ``sha256(user_token) → introspection result`` mappings so that
回调验证模式 authentication does not hit the partner's introspection
endpoint on every request.

Two-tier storage:
- Fresh key (``:fresh``) with short TTL — normal cache hit path.
- Stale key (``:stale``) with longer TTL — fallback when the partner
  introspection endpoint is unavailable. Stale results are flagged to
  the caller via ``X-User-Auth-Stale: true`` response header (AC7).

原始 user_token 永远不会被缓存 —— 只用其 sha256 hash 作 key。
"""
import hashlib
import json

from loguru import logger

from app.db.redis import get_redis_client

_CACHE_PREFIX = "extuser:introspect:"
_FRESH_SUFFIX = ":fresh"
_STALE_SUFFIX = ":stale"

# Fresh TTL matches the design doc default (60s).
_FRESH_TTL = 60
# Stale TTL: window during which a stale result is still acceptable as
# degradation fallback. Caps the worst-case staleness to 30 minutes.
_STALE_TTL = 1800


def _token_hash(user_token: str) -> str:
    """sha256 hex digest of the raw user token (one-way, never stored raw)."""
    return hashlib.sha256(user_token.encode("utf-8")).hexdigest()


async def cache_introspection(user_token: str, payload: dict) -> None:
    """Cache an introspection result by token hash.

    Writes both the fresh key (short TTL) and the stale key (long TTL),
    so a transient partner outage can degrade to stale (AC7).
    """
    token_hash = _token_hash(user_token)
    serialized = json.dumps(payload, default=str)
    try:
        redis = await get_redis_client()
        # Stale first (longer TTL) — even if fresh write fails, we still have fallback.
        await redis.set(f"{_CACHE_PREFIX}{token_hash}{_STALE_SUFFIX}", serialized, ex=_STALE_TTL)
        await redis.set(f"{_CACHE_PREFIX}{token_hash}{_FRESH_SUFFIX}", serialized, ex=_FRESH_TTL)
    except Exception:
        logger.debug("introspection_cache_set_failed", token_hash=token_hash)


async def get_cached_introspection(user_token: str) -> dict | None:
    """Return a fresh cached introspection result, or None on miss/expiry.

    The ``stale`` flag in the returned dict indicates whether the result
    came from the stale-fallback key (True) or the fresh key (False/absent).
    """
    token_hash = _token_hash(user_token)
    try:
        redis = await get_redis_client()
        raw = await redis.get(f"{_CACHE_PREFIX}{token_hash}{_FRESH_SUFFIX}")
        if raw is not None:
            payload = json.loads(raw)
            payload.setdefault("stale", False)
            return payload
    except Exception:
        logger.debug("introspection_cache_get_failed", token_hash=token_hash)
    return None


async def get_stale_introspection(user_token: str) -> dict | None:
    """Return a stale cached introspection result (fallback only), or None.

    Used by AC7 degradation path when the partner introspection endpoint
    is unreachable but Redis still has a (possibly stale) result.
    """
    token_hash = _token_hash(user_token)
    try:
        redis = await get_redis_client()
        raw = await redis.get(f"{_CACHE_PREFIX}{token_hash}{_STALE_SUFFIX}")
        if raw is not None:
            payload = json.loads(raw)
            payload["stale"] = True
            return payload
    except Exception:
        logger.debug("introspection_cache_get_stale_failed", token_hash=token_hash)
    return None


async def invalidate_introspection(user_token: str) -> None:
    """Remove both fresh and stale cached entries for a token."""
    token_hash = _token_hash(user_token)
    try:
        redis = await get_redis_client()
        await redis.delete(
            f"{_CACHE_PREFIX}{token_hash}{_FRESH_SUFFIX}",
            f"{_CACHE_PREFIX}{token_hash}{_STALE_SUFFIX}",
        )
    except Exception:
        logger.debug("introspection_cache_invalidate_failed", token_hash=token_hash)

"""End-user authentication via RFC 7662 token introspection.

When an API Key is configured with ``user_info_url``, every external
request must carry ``X-User-Token``. agent-flow forwards the token to
the partner's introspection endpoint (RFC 7662 subset), resolves a
stable ``sub``, and builds ``user_id = f"{owner}:{sub}"``.

This module is the introspection client. It owns:
- HTTP call to the partner endpoint (form-encoded POST, no auth header)
- Two-tier Redis caching (fresh + stale, see ``introspection_cache``)
- Stale-cache fallback when the partner endpoint is unreachable (AC7)
"""
from __future__ import annotations

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from app.core.errors import AppError
from app.core.introspection_cache import (
    cache_introspection,
    get_cached_introspection,
    get_stale_introspection,
)
from app.core.user_auth_state import mark_introspect_stale


class IntrospectionResult(BaseModel):
    """Parsed RFC 7662 introspection response."""

    active: bool
    sub: str = Field(default="", description="Stable partner-side user ID (required when active=true).")
    exp: int = Field(default=0, description="Token expiry, Unix seconds.")
    username: str = ""
    email: str = ""
    attrs: dict = Field(default_factory=dict)
    # Set by the cache layer to indicate a stale-cache fallback was used.
    stale: bool = False


class UserAuthService:
    """Client for partner OAuth2 token introspection (RFC 7662 subset)."""

    TIMEOUT = 3.0  # seconds; hardcoded per design doc
    USER_AGENT = "AgentFlow-Introspect/1.0"

    @staticmethod
    async def introspect(user_info_url: str, user_token: str) -> IntrospectionResult:
        """Resolve ``user_token`` against ``user_info_url`` with Redis caching.

        Raises:
            AppError(EXT_USER_SERVICE_TIMEOUT, 504) on timeout when no
                cached fallback is available.
            AppError(EXT_USER_SERVICE_UNAVAILABLE, 503) on HTTP/network
                errors when no cached fallback is available.
        """
        # 1. Fresh cache hit
        cached = await get_cached_introspection(user_token)
        if cached is not None:
            return IntrospectionResult(**cached)

        # 2. Live introspection call
        payload = await UserAuthService._call_introspect(user_info_url, user_token)
        result = IntrospectionResult(**payload)

        # 3. Cache only active results — an inactive token must not be
        # cached, otherwise revocation within the TTL window would still
        # be reported as active against a stale cache.
        if result.active:
            await cache_introspection(user_token, payload)

        return result

    @staticmethod
    async def _call_introspect(user_info_url: str, user_token: str) -> dict:
        """POST to the partner introspection endpoint with stale fallback.

        Returns the parsed JSON payload on success.

        Raises AppError on failure when no stale fallback is available.
        """
        try:
            async with httpx.AsyncClient(timeout=UserAuthService.TIMEOUT) as client:
                resp = await client.post(
                    user_info_url,
                    data={"token": user_token},  # form-encoded per RFC 7662
                    headers={
                        "User-Agent": UserAuthService.USER_AGENT,
                        "Accept": "application/json",
                    },
                )
                # RFC 7662: invalid tokens return 200 + {"active": false},
                # so we treat any non-2xx as a service-level failure.
                resp.raise_for_status()
                return resp.json()

        except httpx.TimeoutException:
            return await UserAuthService._fallback_or_raise(
                user_token,
                user_info_url,
                error_kind="timeout",
                exc_chain="httpx.TimeoutException",
            )
        except httpx.HTTPStatusError as exc:
            return await UserAuthService._fallback_or_raise(
                user_token,
                user_info_url,
                error_kind=f"http_{exc.response.status_code}",
                exc_chain=str(exc),
            )
        except httpx.HTTPError as exc:
            # Covers ConnectError, ReadError, etc.
            return await UserAuthService._fallback_or_raise(
                user_token,
                user_info_url,
                error_kind="network",
                exc_chain=type(exc).__name__,
            )

    @staticmethod
    async def _fallback_or_raise(
        user_token: str,
        user_info_url: str,
        error_kind: str,
        exc_chain: str,
    ) -> dict:
        """Try a stale-cache fallback; raise AppError if none available.

        Per AC7: when the partner endpoint is unreachable, degrade to the
        most recent cached result (if any) and flag the request as stale.
        """
        stale_payload = await get_stale_introspection(user_token)
        if stale_payload is not None:
            mark_introspect_stale()
            logger.warning(
                "introspection_stale_fallback",
                user_info_url=user_info_url,
                error_kind=error_kind,
                # token intentionally NOT logged
            )
            return stale_payload

        # No fallback available — surface an explicit error.
        logger.warning(
            "introspection_failed_no_fallback",
            user_info_url=user_info_url,
            error_kind=error_kind,
        )
        if error_kind == "timeout":
            raise AppError(
                code="EXT_USER_SERVICE_TIMEOUT",
                message="User authentication service timed out.",
                status_code=504,
            )
        raise AppError(
            code="EXT_USER_SERVICE_UNAVAILABLE",
            message="User authentication service unavailable.",
            status_code=503,
        )


# Singleton accessor (matches existing service patterns)
user_auth_service = UserAuthService()

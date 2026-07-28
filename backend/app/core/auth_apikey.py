"""API Key authentication dependency for external API routes.

Provides ``get_api_key_principal`` — a FastAPI Depends that validates
the Bearer token as an API Key (not JWT) and returns an
``ApiKeyPrincipal`` object with scopes and bindings.

When the API Key has ``user_info_url`` configured, the dependency also
requires ``X-User-Token`` and resolves the end-user identity via
RFC 7662 introspection. See
``docs/planning-artifacts/external-user-auth-design.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Header, Request

from app.core.errors import ForbiddenError, UnauthorizedError


@dataclass
class ApiKeyPrincipal:
    """Authenticated API Key identity.

    Carries the Key's scopes, resource bindings, and owner_user_id
    so that downstream route handlers can enforce authorization.

    End-user identity (callback-verification mode):
    - ``user_info_url``: empty = legacy mode (visitor_id); non-empty =
      callback-verification mode (X-User-Token required).
    - ``user_id``: resolved stable user ID. In callback-verification
      mode this is ``f"{owner}:{sub}"``. In legacy mode it stays None
      and route handlers compose it from visitor_id themselves.
    """

    key_id: str
    owner_user_id: str
    scopes: list[str] = field(default_factory=list)
    bindings: dict = field(default_factory=dict)
    rate_limit: int = 60
    user_info_url: str = ""
    user_id: str | None = None
    # Original X-User-Token (Bearer-stripped). Used to forward to MCP
    # servers in callback-verification mode. None in legacy mode.
    user_token: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def require_scope(self, scope: str) -> None:
        if not self.has_scope(scope):
            raise ForbiddenError(
                code="APIKEY_SCOPE_DENIED",
                message=f"API Key 权限不足，需要 {scope} 权限",
            )

    def can_access_agent(self, agent_id: str) -> bool:
        """Check if this key can access the given Agent. Empty bindings = all."""
        allowed = self.bindings.get("agents", [])
        if not allowed:
            return True
        return agent_id in allowed

    def can_access_workflow(self, workflow_id: str) -> bool:
        """Check if this key can access the given Workflow. Empty bindings = all."""
        allowed = self.bindings.get("workflows", [])
        if not allowed:
            return True
        return workflow_id in allowed

    def require_agent_access(self, agent_id: str) -> None:
        if not self.can_access_agent(agent_id):
            raise ForbiddenError(
                code="APIKEY_AGENT_DENIED",
                message="API Key 无权访问该 Agent",
            )

    def require_workflow_access(self, workflow_id: str) -> None:
        if not self.can_access_workflow(workflow_id):
            raise ForbiddenError(
                code="APIKEY_WORKFLOW_DENIED",
                message="API Key 无权访问该 Workflow",
            )


def _extract_bearer_token(header_value: str | None) -> str | None:
    """Extract a Bearer token from a header value, accepting both
    ``Bearer xxx`` and bare-token forms. Returns None on missing/empty.
    """
    if not header_value:
        return None
    value = header_value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value or None


async def get_api_key_principal(
    request: Request,
    authorization: str = Header(None, description="Bearer af_live_xxx"),
) -> ApiKeyPrincipal:
    """FastAPI dependency: authenticate via API Key.

    In callback-verification mode (API Key has ``user_info_url``), also
    requires ``X-User-Token`` and resolves end-user identity.

    Raises:
        UnauthorizedError: Missing/invalid API Key, missing X-User-Token,
            or introspection returned active=false.
        AppError(503/504): Introspection endpoint unreachable and no
            stale-cache fallback available (see UserAuthService).
    """
    from app.services.api_key_service import ApiKeyService
    from app.services.user_auth_service import UserAuthService

    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError(
            code="APIKEY_MISSING",
            message="Missing or malformed Authorization header",
        )

    full_key = authorization.removeprefix("Bearer ").strip()

    if not full_key.startswith("af_live_"):
        raise UnauthorizedError(
            code="APIKEY_INVALID",
            message="Invalid or expired API Key",
        )

    doc = await ApiKeyService.verify_key(full_key)
    if doc is None:
        raise UnauthorizedError(
            code="APIKEY_INVALID",
            message="Invalid or expired API Key",
        )

    principal = ApiKeyPrincipal(
        key_id=doc["_id"],
        owner_user_id=doc["owner_user_id"],
        scopes=doc.get("scopes", []),
        bindings=doc.get("bindings", {}),
        rate_limit=doc.get("rate_limit", 60),
        user_info_url=doc.get("user_info_url", "") or "",
    )

    # Callback-verification mode: resolve end-user identity.
    if principal.user_info_url:
        user_token = _extract_bearer_token(request.headers.get("X-User-Token"))
        if not user_token:
            raise UnauthorizedError(
                code="EXT_USER_TOKEN_MISSING",
                message="X-User-Token header is required for this API Key.",
            )
        result = await UserAuthService.introspect(principal.user_info_url, user_token)
        if not result.active:
            raise UnauthorizedError(
                code="EXT_USER_TOKEN_INVALID",
                message="User token is invalid or expired.",
            )
        if not result.sub:
            raise UnauthorizedError(
                code="EXT_USER_TOKEN_INVALID",
                message="Introspection response missing required 'sub' field.",
            )
        principal.user_id = f"{principal.owner_user_id}:{result.sub}"
        # Retain the original token so downstream MCP calls can forward it.
        principal.user_token = user_token

    return principal


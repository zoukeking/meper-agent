"""API Key Pydantic schemas for API request/response."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.api_key import ALL_SCOPES, ApiKeyStatus


class ApiKeyBindingsSchema(BaseModel):
    """Resource bindings for API request."""

    agents: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)


class ApiKeyCreate(BaseModel):
    """Schema for creating a new API Key."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="API Key 显示名称",
        examples=["MES 产线 A"],
    )
    scopes: list[str] = Field(
        ...,
        min_length=1,
        description=f"权限列表，可选值: {', '.join(ALL_SCOPES)}",
        examples=[["agents:invoke", "agents:read", "executions:read"]],
    )
    bindings: ApiKeyBindingsSchema = Field(
        default_factory=ApiKeyBindingsSchema,
        description="资源绑定，空列表表示不限制",
    )
    rate_limit: int = Field(
        default=60,
        ge=1,
        le=10000,
        description="每分钟请求上限",
    )
    expires_at: str | None = Field(
        default=None,
        description="过期时间（ISO 格式），null 表示永不过期",
    )
    user_info_url: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "接入方 introspection 端点 URL（RFC 7662）。"
            "null/空=兼容模式(visitor_id);有值=回调验证模式(强制 X-User-Token)"
        ),
    )


class ApiKeyUpdate(BaseModel):
    """Schema for updating an API Key."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    scopes: list[str] | None = None
    bindings: ApiKeyBindingsSchema | None = None
    rate_limit: int | None = Field(default=None, ge=1, le=10000)
    expires_at: str | None = None
    user_info_url: str | None = Field(
        default=None,
        max_length=500,
        description="接入方 introspection 端点 URL；传空串清除",
    )


class ApiKeyResponse(BaseModel):
    """API Key returned in list/detail responses (no raw key)."""

    id: str
    name: str
    key_prefix: str
    owner_user_id: str
    scopes: list[str]
    bindings: ApiKeyBindingsSchema
    rate_limit: int
    status: ApiKeyStatus
    expires_at: str | None
    last_used_at: str | None
    user_info_url: str = Field(default="", description="空=兼容模式;有值=回调验证模式")
    created_at: str
    updated_at: str


class ApiKeyCreateResponse(BaseModel):
    """Response after creating an API Key — includes the raw key (one-time)."""

    id: str
    name: str
    key: str
    key_prefix: str
    owner_user_id: str
    scopes: list[str]
    bindings: ApiKeyBindingsSchema
    rate_limit: int
    status: ApiKeyStatus
    expires_at: str | None
    user_info_url: str = Field(default="", description="空=兼容模式;有值=回调验证模式")
    created_at: str


class ApiKeyListResponse(BaseModel):
    """Paginated API Key list response."""

    items: list[ApiKeyResponse]
    total: int
    page: int
    page_size: int


class ApiKeyStatsResponse(BaseModel):
    """Aggregated call statistics for an API Key."""

    api_key_id: str
    total_requests: int
    successful: int
    failed: int
    by_endpoint: dict[str, int]
    last_used_at: str | None
    # Token consumption (from execution_logs aggregate, api_key channel).
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Distinct active end-users in the time window (callback mode only).
    unique_users: int = 0


class ApiKeyLogItem(BaseModel):
    """Single call log entry (execution_logs row, no _id)."""

    source: str = ""
    user_id: str = ""
    api_key_id: str = ""
    user_sub: str = ""
    visitor_id: str = ""
    endpoint: str = ""
    agent_id: str = ""
    session_id: str = ""
    request_id: str = ""
    status: str = ""
    status_code: int = 0
    latency_ms: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    timestamp: str = ""


class ApiKeyLogsResponse(BaseModel):
    """Paginated call log list."""

    items: list[ApiKeyLogItem]
    total: int
    page: int
    page_size: int


class ApiKeyUserStats(BaseModel):
    """Per-user aggregate (one row per active end-user)."""

    user_sub: str
    calls: int
    total_tokens: int
    last_seen_at: str = ""


class ApiKeyUsersResponse(BaseModel):
    """Active end-users for an API Key (callback-verification mode)."""

    items: list[ApiKeyUserStats]
    period_days: int

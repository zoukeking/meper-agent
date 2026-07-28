"""Agent-related Pydantic schemas for API request/response."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from app.models.agent import AgentStatus, RecommendedItem
from app.utils.sanitize import sanitize_dict, sanitize_text

# 单个 prompt_slot value 的最大字符数（兼顾详细提示词与防滥用，
# 约 2.5K–5K tokens，足够绝大多数场景）。
PROMPT_SLOT_MAX_LENGTH = 10_000
# prompt_slots 允许的 key 数量上限（对应 5 个预定义卡槽 + 容错余量）。
PROMPT_SLOT_MAX_KEYS = 20
# 卡槽 key 命名白名单（与 SLOT_SCHEMA 的 name 风格一致）。
_PROMPT_SLOT_KEY_PATTERN = r"^[a-zA-Z0-9_]+$"


class AgentCreate(BaseModel):
    """Schema for creating a new Agent.

    Only essential fields at creation time. Configure prompt slots,
    tools, workflows, and model via the update endpoint afterwards.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Agent 名称（唯一必填字段）",
        examples=["我的助手"],
    )
    description: str = Field(
        default="",
        max_length=500,
        description="Agent 简要描述",
        examples=["负责客户问答的智能助手"],
    )

    @field_validator("name", "description", mode="after")
    @classmethod
    def _sanitize_text_fields(cls, v: str) -> str:
        """后端纵深防御：清洗存储型 XSS 载荷（保留普通文本与 LLM 所需的
        代码/模板语法，详见 app.utils.sanitize）。"""
        return sanitize_text(v)


def _validate_prompt_slots(value: dict[str, str]) -> dict[str, str]:
    """Validate then sanitize prompt_slots.

    - 限制 key 数量、key 命名（``^[a-zA-Z0-9_]+$``）。
    - 限制每个 value 的最大长度（:data:`PROMPT_SLOT_MAX_LENGTH`）。
    - 清洗每个 value 的 XSS 载荷。
    """
    if not isinstance(value, dict):
        return value  # type: ignore[return-value]
    if len(value) > PROMPT_SLOT_MAX_KEYS:
        raise ValueError(
            f"prompt_slots 最多 {PROMPT_SLOT_MAX_KEYS} 个卡槽（当前 {len(value)} 个）"
        )
    for key, val in value.items():
        if not re.match(_PROMPT_SLOT_KEY_PATTERN, str(key)):
            raise ValueError(
                f"prompt_slots 的 key 只允许字母/数字/下划线（非法 key: {key!r}）"
            )
        if isinstance(val, str) and len(val) > PROMPT_SLOT_MAX_LENGTH:
            raise ValueError(
                f"prompt_slots[{key!r}] 超过最大长度 "
                f"{PROMPT_SLOT_MAX_LENGTH}（当前 {len(val)}）"
            )
    return sanitize_dict(value)


class AgentUpdate(BaseModel):
    """Schema for updating an existing Agent (full replacement via PUT).

    All fields optional except ``name``. Status is **not** settable —
    use the dedicated publish / archive endpoints instead.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Agent 名称",
    )
    description: str = Field(
        default="",
        max_length=500,
        description="Agent 简要描述",
    )
    welcome_message: str = Field(
        default="",
        max_length=2000,
        description="终端用户首屏欢迎词（Markdown，可选）",
    )
    recommended_items: list[RecommendedItem] = Field(
        default_factory=list,
        max_length=10,
        description="首屏推荐问题/操作快捷项（≤10 条）",
    )
    prompt_slots: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "提示词卡槽内容。key 只允许字母/数字/下划线，"
            f"每个 value 最长 {PROMPT_SLOT_MAX_LENGTH} 字符，"
            f"最多 {PROMPT_SLOT_MAX_KEYS} 个卡槽。"
        ),
    )
    # --- Categorized tool fields ---
    skill_ids: list[str] = Field(
        default_factory=list,
        description="绑定的 Skill 工具 ID（source=markdown）",
    )
    mcp_connection_ids: list[str] = Field(
        default_factory=list,
        description="绑定的 MCP 连接 ID",
    )
    builtin_config: list[str] = Field(
        default_factory=list,
        description="内置工具白名单（如 bash / read / write）",
    )
    workflow_ids: list[str] = Field(
        default_factory=list,
        description="绑定的工作流 ID",
    )
    custom_tool_ids: list[str] = Field(
        default_factory=list,
        description="绑定的自定义工具 ID（openapi/code/prebuilt）",
    )
    knowledge_base_ids: list[str] = Field(
        default_factory=list,
        description="绑定的知识库 ID",
    )
    default_model: str = Field(
        default="",
        description="绑定的 Model ID（model_xxx ULID 或纯模型名）",
    )
    max_retry: int = Field(
        default=3,
        ge=0,
        le=10,
        description="LLM 调用失败最大重试次数",
    )
    max_tokens: int = Field(
        default=0,
        ge=0,
        description="会话 Token 上限（累计，0 = 使用全局默认）",
    )

    @field_validator("name", "description", "welcome_message", mode="after")
    @classmethod
    def _sanitize_text_fields(cls, v: str) -> str:
        """后端纵深防御：清洗存储型 XSS 载荷。"""
        return sanitize_text(v)

    @field_validator("recommended_items", mode="after")
    @classmethod
    def _sanitize_recommended_items(cls, v: list[RecommendedItem]) -> list[RecommendedItem]:
        """逐条清洗推荐项 label / prompt 的 XSS 载荷。"""
        for item in v:
            item.label = sanitize_text(item.label)
            item.prompt = sanitize_text(item.prompt)
        return v

    @field_validator("prompt_slots", mode="after")
    @classmethod
    def _validate_prompt_slots(cls, v: dict[str, str]) -> dict[str, str]:
        """校验 key 命名/数量与每值长度，并清洗 XSS 载荷。"""
        return _validate_prompt_slots(v)


class AgentResponse(BaseModel):
    """Agent data returned in API responses."""

    id: str
    name: str
    description: str
    welcome_message: str = Field(default="")
    recommended_items: list[RecommendedItem] = Field(default_factory=list)
    prompt_slots: dict[str, str] = Field(default_factory=dict)
    skill_ids: list[str]
    mcp_connection_ids: list[str]
    builtin_config: list[str]
    workflow_ids: list[str]
    custom_tool_ids: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str]
    default_model: str = Field(default="", description="Model reference ID")
    max_retry: int = Field(default=3, description="Max LLM call retries")
    max_tokens: int = Field(default=0, description="Session token budget (0 = global default)")
    status: AgentStatus
    created_at: str
    updated_at: str


class AgentListResponse(BaseModel):
    """Paginated agent list response."""

    items: list[AgentResponse]
    total: int
    page: int
    page_size: int

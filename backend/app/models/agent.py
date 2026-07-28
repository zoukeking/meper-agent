"""Agent data model for MongoDB."""
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from pydantic.config import ConfigDict

from app.models.base import generate_id, utc_now


class AgentStatus(StrEnum):
    """Agent lifecycle status."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class RecommendedItem(BaseModel):
    """终端用户首屏推荐问题/操作快捷项。"""

    label: str = Field(..., min_length=1, max_length=100, description="按钮显示文案")
    prompt: str = Field(default="", max_length=500, description="实际发送内容；留空则用 label")


class Agent(BaseModel):
    """MongoDB agent document model.

    Follows the same pattern as ``User`` — raw Pydantic model,
    serialized to dict for MongoDB insertion/update.

    Prompt composition uses fixed slots stored directly on the Agent:
    - ``prompt_slots``: dict with keys role/task/constraints/context/output_format

    Tool configuration is split into three categories:
    - ``skill_ids``: IDs of bound Skill tools (``source="markdown"``)
    - ``mcp_connection_ids``: IDs of bound MCP connections
    - ``builtin_config``: whitelist of enabled built-in tool names

    ``tool_ids`` is deprecated but kept for backward compatibility
    with old documents.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("agent"), alias="_id")
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    welcome_message: str = Field(
        default="", max_length=2000, description="终端用户首屏欢迎词（Markdown）"
    )
    recommended_items: list[RecommendedItem] = Field(
        default_factory=list, max_length=10, description="首屏推荐问题/操作快捷项（≤10 条）"
    )
    prompt_slots: dict[str, str] = Field(default_factory=dict)
    # --- Deprecated: kept for backward compat ---
    tool_ids: list[str] = Field(default_factory=list)
    # --- New categorized fields ---
    skill_ids: list[str] = Field(default_factory=list)
    mcp_connection_ids: list[str] = Field(default_factory=list)
    custom_tools: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "自定义工具绑定列表。每项含 tool_id + user_args。"
            "示例: [{'tool_id': 'tool_xxx', 'user_args': {'token': 'enc:...', 'owner': 'myorg'}}]"
        ),
    )
    builtin_config: list[str] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    default_model: str = Field(default="", description="Model reference (model_xxx ULID or plain name)")
    max_retry: int = Field(default=3, ge=0, le=10, description="Max LLM call retries on failure")
    max_tokens: int = Field(default=0, ge=0, description="Session token budget (0 = use global DEFAULT_SESSION_MAX_TOKENS)")
    status: AgentStatus = Field(default=AgentStatus.DRAFT)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    def model_post_init(self, __context: object) -> None:
        """Backward compat: populate skill_ids from deprecated tool_ids."""
        super().model_post_init(__context)
        if not self.skill_ids and self.tool_ids:
            object.__setattr__(self, "skill_ids", list(self.tool_ids))

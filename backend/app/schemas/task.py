"""Pydantic schemas for Task CRUD operations and responses."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.task import TaskStatus
from app.schemas.common import PaginatedResponse

# ── Request Schemas ──

class TaskCreate(BaseModel):
    """Request body for creating a new Task."""

    workflow_id: str = Field(..., min_length=1, max_length=100)
    input: dict[str, Any] = Field(default_factory=dict)
    scheduled_at: datetime | None = None


class TaskIntervene(BaseModel):
    """Request body for Task intervention."""

    action: str = Field(
        ...,
        pattern=r"^(approve|reject|skip|retry|pause|resume|cancel|update_variables|rewind)$",
    )
    # Deprecated: use comment; reason kept for backward compat
    reason: str | None = None
    # comment 支持三种形态（向后兼容）：
    # - str: 纯文本（老用法）
    # - {"type": "text", "value": "..."}: 文本
    # - {"type": "json", "value": {...}}: 结构化数据，value 原样存入 variables，
    #   下游可用 {{node.comment.field}} 钻取
    comment: str | dict[str, Any] | None = None
    version: int = Field(..., ge=1)
    # rewind 专用：回退到的目标节点（必须是已执行过的节点）
    target_node_id: str | None = None
    # rewind 可选：覆盖变量池的输入值（merge 语义）
    variables: dict[str, Any] | None = None


class TaskUpdateVariables(BaseModel):
    """Request body for updating Task variables."""

    variables: dict[str, Any]
    reason: str | None = None
    version: int = Field(..., ge=1)


# ── Response Schemas ──

class TimelineEventResponse(BaseModel):
    """A single timeline event in the API response."""

    timestamp: datetime
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"


class TaskErrorResponse(BaseModel):
    """Error information in the API response."""

    node_id: str | None = None
    node_type: str | None = None
    error_message: str = ""
    error_code: str = ""
    timestamp: datetime | None = None


class CheckpointResponse(BaseModel):
    """Checkpoint information for paused Human node tasks."""

    paused_at_node: str
    completed_nodes: list[str] = Field(default_factory=list)
    variable_snapshot: dict[str, Any] = Field(default_factory=dict)
    paused_at: datetime
    human_context: dict[str, Any] = Field(default_factory=dict)
    timeout_deadline: datetime | None = None
    timeout_action: str = "fail"


class TaskResponse(BaseModel):
    """Full Task response (includes variables and timeline)."""

    id: str
    workflow_id: str
    workflow_version: str = ""
    status: TaskStatus
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    call_chain: list[str] = Field(default_factory=list)
    parent_task_id: str | None = None
    created_by: str = ""
    created_by_type: str = "user"
    version: int = 1
    timeline: list[TimelineEventResponse] = Field(default_factory=list)
    error: TaskErrorResponse | None = None
    checkpoint: CheckpointResponse | None = None
    source: str = "manual"
    trigger_id: str | None = None
    scheduled_at: datetime | None = None
    total_tokens: int = 0
    created_at: datetime
    updated_at: datetime


class TaskSummary(BaseModel):
    """Compact Task response for list queries (no variables/timeline)."""

    id: str
    workflow_id: str
    workflow_version: str = ""
    status: TaskStatus
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    parent_task_id: str | None = None
    created_by: str = ""
    created_by_type: str = "user"
    version: int = 1
    error: TaskErrorResponse | None = None
    checkpoint: CheckpointResponse | None = None
    source: str = "manual"
    trigger_id: str | None = None
    scheduled_at: datetime | None = None
    total_tokens: int = 0
    created_at: datetime
    updated_at: datetime


class TaskListResponse(PaginatedResponse[TaskSummary]):
    """Paginated Task list response."""
    items: list[TaskSummary] = Field(default_factory=list)


class TaskStatsResponse(BaseModel):
    """Concurrency and Task statistics."""

    global_running: int = 0
    global_pending: int = 0
    global_max: int = 50
    user_stats: list[dict[str, Any]] = Field(default_factory=list)


class TaskInterveneResponse(BaseModel):
    """Response after a successful intervention."""

    task_id: str
    status: TaskStatus
    version: int
    message: str = ""


# ── Node execution detail (agent trace from checkpointer) ──


class NodeTimelineEntry(BaseModel):
    """单个 timeline 条目，格式与 chat 路径的 timeline_entries 对齐。

    type 取值: user / thinking / text / tool_call / tool_result。
    """

    type: str
    content: str = ""
    tool_name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    id: str = ""


class NodeTimelineResponse(BaseModel):
    """Agent 节点执行详情响应（按需从 checkpointer thread 读取）。"""

    task_id: str
    node_id: str
    thread_id: str
    timeline: list[NodeTimelineEntry] = Field(default_factory=list)
    message_count: int = 0

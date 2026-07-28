"""Execution log model — unified per-call agent execution records.

Each document records a single agent invocation (invoke / stream / resume)
across all access channels. This collection is independent of ``sessions``
and ``messages``: deleting a session does not remove its execution history
here, so statistics remain accurate.

Channels are distinguished by ``source``:
- ``internal`` — platform users (JWT auth, frontend login)
- ``api_key``  — third-party widget / ext API callers
- ``im``       — IM channel users (Lark / DingTalk)

TTL: documents auto-expire after 365 days (see service ensure_indexes).
The ``timestamp`` field MUST be stored as a BSON date for TTL to work.
"""
from typing import Any

from pydantic import BaseModel, Field
from pydantic.config import ConfigDict

from app.models.base import generate_id, utc_now


class ExecutionLog(BaseModel):
    """One agent execution call (append-only, channel-agnostic)."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("xlog"), alias="_id")

    # ── Source / channel ──
    source: str = Field(..., description="internal | api_key | im")
    user_id: str = Field(default="", description="调用者 user_id（含通道前缀）")

    # ── Call context ──
    agent_id: str = Field(default="")
    session_id: str = Field(default="", description="关联 session（session 删除后变孤儿但不影响统计）")
    request_id: str = Field(default="")

    # ── External-specific (source=api_key) ──
    api_key_id: str = Field(default="")
    user_sub: str = Field(default="", description="终端用户 sub (回调验证模式)")
    visitor_id: str = Field(default="", description="兼容模式访客标识")
    endpoint: str = Field(default="", description="逻辑端点，如 agents:invoke:stream")

    # ── IM-specific (source=im) ──
    channel_id: str = Field(default="", description="IM 渠道 ID")

    # ── Result ──
    status: str = Field(default="success", description="success | error")
    status_code: int = Field(default=0)
    latency_ms: int = Field(default=0, description="调用耗时（毫秒）")

    # ── Token consumption ──
    total_tokens: int = Field(default=0)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    llm_calls: int = Field(default=0)

    # ── BSON date for TTL index ──
    timestamp: Any = Field(default_factory=utc_now)

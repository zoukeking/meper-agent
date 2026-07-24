"""Application-layer event schemas for the streaming adapter.

Eight Pydantic event models that the frontend SSE client consumes. These
mirror the legacy ``react_executor.run_streaming`` event dict shapes so the
chat panel needs zero changes when the harness adapter takes over.

The union :data:`AppEvent` is the discriminated sum type: every model carries
a literal ``type`` field, so ``model_dump()`` yields the exact dict the legacy
code emitted (``{"type": "...", ...}``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class _Base(BaseModel):
    """Common base — subclasses pin their own ``type`` literal."""

    model_config = {"extra": "forbid"}


class ThinkingDeltaEvent(_Base):
    """Incremental reasoning token (only emitted when thinking is enabled)."""

    type: Literal["thinking_delta"] = "thinking_delta"
    content: str


class ThinkingEvent(_Base):
    """Complete reasoning text emitted once at ``on_chat_model_end``."""

    type: Literal["thinking"] = "thinking"
    content: str


class TextDeltaEvent(_Base):
    """Incremental text token streamed from the LLM.

    Each delta is a small fragment of the assistant text produced during a
    single LLM output phase (one ``on_chat_model_stream`` chunk). The host
    concatenates them to reconstruct the full text block for display.
    """

    type: Literal["text_delta"] = "text_delta"
    content: str


class TextEvent(_Base):
    """Complete text block emitted once at ``on_chat_model_end``.

    A single LLM call may produce text before/without tool calls; this event
    carries that complete text. The name ``text`` (rather than
    ``final_answer``) reflects that the content is any assistant text block —
    a transitional remark before a tool call, a post-tool summary, or the
    terminal answer — not necessarily the final reply.
    """

    type: Literal["text"] = "text"
    content: str


class ToolCallStartEvent(_Base):
    """Streaming placeholder signalling a tool call is being generated."""

    type: Literal["tool_call_start"] = "tool_call_start"
    tool_name: str = ""


class ToolCallEvent(_Base):
    """A fully-resolved tool call (name/args/id), emitted at model end."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: str
    args: dict[str, Any]
    id: str


class ToolResultEvent(_Base):
    """The result content of a completed tool invocation."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: str
    content: str
    status: Literal["success", "error"] = "success"


class InterruptEvent(_Base):
    """Agent paused via ``interrupt()`` and is awaiting a human response.

    Emitted when the graph encounters an ``interrupt()`` call (e.g.
    ``ask_clarification``). The host should display the question to the user
    and resume the graph with ``Command(resume=answer)``.

    When ``fields`` is non-empty, the host should render a structured form
    (one input per field) instead of a single question card; the user's
    answers are aggregated as a JSON string and passed back via resume.
    Each field dict mirrors ``ClarificationField.model_dump()``.
    """

    type: Literal["interrupt"] = "interrupt"
    question: str
    clarification_type: str = "missing_info"
    context: str | None = None
    options: list[str] | None = None
    fields: list[dict[str, Any]] | None = None
    interrupt_id: str = ""


class ErrorEvent(_Base):
    """A terminal error from the LLM, a tool, or the graph itself."""

    type: Literal["error"] = "error"
    message: str
    source: Literal["llm", "tool", "graph"]


type AppEvent = (
    ThinkingDeltaEvent
    | ThinkingEvent
    | TextDeltaEvent
    | TextEvent
    | ToolCallStartEvent
    | ToolCallEvent
    | ToolResultEvent
    | InterruptEvent
    | ErrorEvent
)
"""Discriminated union of all application-layer events."""


__all__ = [
    "AppEvent",
    "ErrorEvent",
    "InterruptEvent",
    "TextDeltaEvent",
    "TextEvent",
    "ThinkingDeltaEvent",
    "ThinkingEvent",
    "ToolCallEvent",
    "ToolCallStartEvent",
    "ToolResultEvent",
]

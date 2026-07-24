"""stream_events_to_app_events — core translation logic tests.

Covers all event mapping paths: text streaming, tool calls, tool results,
interrupt detection (GraphInterrupt → InterruptEvent), error handling,
thinking (reasoning), and the streaming accumulator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage

from app.engine.harness_integration.adapters.stream_events import (
    _extract_interrupt,
    _extract_text_content,
    _extract_thinking_content,
    _StreamingAccumulator,
    stream_events_to_app_events,
)

# ---------------------------------------------------------------------------
# Helpers — simulate LangGraph event shapes
# ---------------------------------------------------------------------------


@dataclass
class _Chunk:
    """Simulates an AIMessageChunk from on_chat_model_stream."""
    content: Any = ""
    tool_call_chunks: list[Any] | None = None
    additional_kwargs: dict[str, Any] | None = None


@dataclass
class _ToolCallChunk:
    """Simulates a tool_call_chunk on an AIMessageChunk."""
    name: str | None = None
    args: str | None = None
    id: str | None = None
    index: int = 0


@dataclass
class _AIMessage:
    """Simulates an AIMessage from on_chat_model_end."""
    content: Any = ""
    tool_calls: list[dict[str, Any]] | None = None
    additional_kwargs: dict[str, Any] | None = None


@dataclass
class _ToolMessage:
    """Simulates a ToolMessage from on_tool_end."""
    content: str = ""


@dataclass
class _Interrupt:
    """Simulates langgraph.types.Interrupt."""
    value: dict[str, Any]
    id: str = "test-id"


class _GraphInterruptError(Exception):
    """Simulates langgraph.errors.GraphInterrupt."""
    def __init__(self, interrupts: tuple) -> None:
        super().__init__(interrupts)
        self.args = (interrupts,)


async def _run(events: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    """Run the adapter with mock events, return emitted event dicts."""
    emitted: list[dict[str, Any]] = []
    callback = AsyncMock(side_effect=lambda ev: emitted.append(ev.model_dump()))

    async def _aiter():
        for ev in events:
            yield ev

    await stream_events_to_app_events(_aiter(), callback, **kwargs)
    return emitted


# ---------------------------------------------------------------------------
# Text streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_delta_from_stream_chunk():
    """on_chat_model_stream with text content → text_delta event."""
    events = [{"event": "on_chat_model_stream", "data": {"chunk": _Chunk(content="hello")}}]
    emitted = await _run(events)
    assert {"type": "text_delta", "content": "hello"} in emitted


@pytest.mark.asyncio
async def test_text_complete_from_model_end():
    """on_chat_model_end with text content → text event."""
    events = [{"event": "on_chat_model_end", "data": {"output": _AIMessage(content="final answer")}}]
    emitted = await _run(events)
    assert {"type": "text", "content": "final answer"} in emitted


@pytest.mark.asyncio
async def test_text_content_as_list_of_blocks():
    """Content as [{"type": "text", "text": "..."}] is extracted correctly."""
    chunk = _Chunk(content=[{"type": "text", "text": "block text"}])
    events = [{"event": "on_chat_model_stream", "data": {"chunk": chunk}}]
    emitted = await _run(events)
    assert {"type": "text_delta", "content": "block text"} in emitted


@pytest.mark.asyncio
async def test_no_text_delta_for_empty_chunk():
    """Empty content chunk produces no events."""
    events = [{"event": "on_chat_model_stream", "data": {"chunk": _Chunk(content="")}}]
    emitted = await _run(events)
    assert emitted == []


# ---------------------------------------------------------------------------
# Tool call events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_start_from_stream_chunk():
    """First tool_call_chunk with a name → tool_call_start event."""
    chunk = _Chunk(tool_call_chunks=[_ToolCallChunk(name="write", id="tc1")])
    events = [{"event": "on_chat_model_stream", "data": {"chunk": chunk}}]
    emitted = await _run(events)
    assert {"type": "tool_call_start", "tool_name": "write"} in emitted


@pytest.mark.asyncio
async def test_tool_call_from_model_end():
    """on_chat_model_end with tool_calls → tool_call event."""
    output = _AIMessage(
        content="let me check",
        tool_calls=[{"name": "read", "args": {"path": "x"}, "id": "c1"}],
    )
    events = [{"event": "on_chat_model_end", "data": {"output": output}}]
    emitted = await _run(events)
    assert {"type": "text", "content": "let me check"} in emitted
    assert {"type": "tool_call", "tool_name": "read", "args": {"path": "x"}, "id": "c1"} in emitted


@pytest.mark.asyncio
async def test_on_tool_start_emits_nothing():
    """on_tool_start should not emit any event (tool_call comes from model_end)."""
    events = [{"event": "on_tool_start", "name": "read", "data": {}}]
    emitted = await _run(events)
    assert emitted == []


@pytest.mark.asyncio
async def test_tool_result_from_tool_end():
    """on_tool_end → tool_result event."""
    events = [{"event": "on_tool_end", "name": "write", "data": {"output": _ToolMessage(content="done")}}]
    emitted = await _run(events)
    assert {"type": "tool_result", "tool_name": "write", "content": "done", "status": "success"} in emitted


async def _collect_tool_end_events(tool_message: ToolMessage) -> list[Any]:
    """Helper: 模拟一个 on_tool_end 事件，收集发出的 AppEvent。"""
    events: list[Any] = []

    async def on_event(evt: Any) -> None:
        events.append(evt)

    fake_event = {
        "event": "on_tool_end",
        "name": "mcp__github__create_issue",
        "data": {"output": tool_message},
    }

    async def _aiter():
        yield fake_event

    await stream_events_to_app_events(_aiter(), on_event)
    return events


@pytest.mark.asyncio
async def test_tool_end_error_status_propagated():
    """ToolMessage(status=error) 应产生 status=error 的 ToolResultEvent。"""
    error_msg = ToolMessage(
        content="Error executing tool: permission denied",
        name="mcp__github__create_issue",
        tool_call_id="call_1",
        status="error",
    )
    events = await _collect_tool_end_events(error_msg)
    tool_results = [e for e in events if getattr(e, "type", None) == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].status == "error"
    assert "permission denied" in tool_results[0].content


@pytest.mark.asyncio
async def test_tool_end_success_status_default():
    """正常 ToolMessage 应产生 status=success 的 ToolResultEvent。"""
    ok_msg = ToolMessage(
        content="issue created #42",
        name="mcp__github__create_issue",
        tool_call_id="call_1",
    )
    events = await _collect_tool_end_events(ok_msg)
    tool_results = [e for e in events if getattr(e, "type", None) == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].status == "success"


@pytest.mark.asyncio
async def test_full_conversation_sequence():
    """text_delta → tool_call_start → text → tool_call → tool_result."""
    events = [
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk(content="let me")}},
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk(content=" check")}},
        {
            "event": "on_chat_model_stream",
            "data": {"chunk": _Chunk(tool_call_chunks=[_ToolCallChunk(name="read", id="c1")])},
        },
        {
            "event": "on_chat_model_end",
            "data": {
                "output": _AIMessage(
                    content="let me check",
                    tool_calls=[{"name": "read", "args": {"path": "x"}, "id": "c1"}],
                )
            },
        },
        {"event": "on_tool_start", "name": "read", "data": {}},
        {"event": "on_tool_end", "name": "read", "data": {"output": _ToolMessage(content="file content")}},
    ]
    emitted = await _run(events)

    # Check key events are present
    types = [e["type"] for e in emitted]
    assert "text_delta" in types
    assert "tool_call_start" in types
    assert "text" in types
    assert "tool_call" in types
    assert "tool_result" in types

    # Check ordering: text_delta before tool_call_start
    delta_idx = types.index("text_delta")
    start_idx = types.index("tool_call_start")
    assert delta_idx < start_idx

    # Check tool_result content
    result = next(e for e in emitted if e["type"] == "tool_result")
    assert result["content"] == "file content"


# ---------------------------------------------------------------------------
# Interrupt detection (ask_clarification)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_from_tool_error_graphinterrupt():
    """on_tool_error with GraphInterrupt → InterruptEvent (not ErrorEvent)."""
    payload = {"question": "what format?", "type": "missing_info", "context": None, "options": None}
    gi = _GraphInterruptError((_Interrupt(value=payload),))
    events = [{"event": "on_tool_error", "data": {"error": gi}}]
    emitted = await _run(events)
    interrupt_events = [e for e in emitted if e["type"] == "interrupt"]
    assert len(interrupt_events) == 1
    assert interrupt_events[0]["question"] == "what format?"
    assert interrupt_events[0]["clarification_type"] == "missing_info"


@pytest.mark.asyncio
async def test_interrupt_from_chain_end():
    """on_chain_end with __interrupt__ → InterruptEvent."""
    payload = {"question": "which approach?", "type": "approach_choice", "options": ["A", "B"]}
    intr = _Interrupt(value=payload)
    events = [{
        "event": "on_chain_end",
        "data": {"output": {"__interrupt__": [intr]}},
    }]
    emitted = await _run(events)
    interrupt_events = [e for e in emitted if e["type"] == "interrupt"]
    assert len(interrupt_events) == 1
    assert interrupt_events[0]["question"] == "which approach?"


@pytest.mark.asyncio
async def test_interrupt_form_fields_propagated():
    """表单模式：payload 中的 fields 透传到 InterruptEvent（两条事件路径）。"""
    fields = [
        {"name": "audience", "label": "受众", "field_type": "select", "allow_other": True},
    ]
    # on_tool_error 路径
    payload = {"question": "请补充", "type": "missing_info", "fields": fields}
    gi = _GraphInterruptError((_Interrupt(value=payload),))
    emitted = await _run([{"event": "on_tool_error", "data": {"error": gi}}])
    ev = next(e for e in emitted if e["type"] == "interrupt")
    assert ev["fields"] == fields

    # on_chain_end 路径
    intr = _Interrupt(value=payload)
    emitted = await _run([{
        "event": "on_chain_end",
        "data": {"output": {"__interrupt__": [intr]}},
    }])
    ev = next(e for e in emitted if e["type"] == "interrupt")
    assert ev["fields"] == fields


@pytest.mark.asyncio
async def test_tool_error_not_interrupt():
    """on_tool_error with regular exception → ErrorEvent."""
    events = [{"event": "on_tool_error", "data": {"error": RuntimeError("tool crashed")}}]
    emitted = await _run(events)
    error_events = [e for e in emitted if e["type"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["source"] == "tool"
    assert "tool crashed" in error_events[0]["message"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_error_emits_error_event():
    """on_llm_error → ErrorEvent with source=llm."""
    events = [{"event": "on_llm_error", "data": {"error": ValueError("bad key")}}]
    emitted = await _run(events)
    error_events = [e for e in emitted if e["type"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["source"] == "llm"


# ---------------------------------------------------------------------------
# Thinking / reasoning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_delta_when_enabled():
    """on_chat_model_stream with reasoning → thinking_delta (when enabled)."""
    chunk = _Chunk(
        content="answer",
        additional_kwargs={"reasoning_content": "thinking about it"},
    )
    events = [{"event": "on_chat_model_stream", "data": {"chunk": chunk}}]
    emitted = await _run(events, enable_thinking=True)
    assert {"type": "thinking_delta", "content": "thinking about it"} in emitted
    assert {"type": "text_delta", "content": "answer"} in emitted


@pytest.mark.asyncio
async def test_thinking_suppressed_when_disabled():
    """Reasoning content is suppressed when enable_thinking=False."""
    chunk = _Chunk(
        content="answer",
        additional_kwargs={"reasoning_content": "secret thoughts"},
    )
    events = [{"event": "on_chat_model_stream", "data": {"chunk": chunk}}]
    emitted = await _run(events, enable_thinking=False)
    assert not any(e["type"] == "thinking_delta" for e in emitted)
    assert {"type": "text_delta", "content": "answer"} in emitted


# ---------------------------------------------------------------------------
# Streaming accumulator
# ---------------------------------------------------------------------------


class TestStreamingAccumulator:
    def test_accumulate_and_pop_starts(self):
        acc = _StreamingAccumulator()
        chunk = _Chunk(tool_call_chunks=[_ToolCallChunk(name="write", id="tc1", index=0)])
        acc.accumulate_tool_call_chunk(chunk)
        starts = acc.pop_new_starts()
        assert len(starts) == 1
        assert starts[0]["name"] == "write"

    def test_pop_starts_idempotent(self):
        """Same index only produces one start."""
        acc = _StreamingAccumulator()
        chunk = _Chunk(tool_call_chunks=[_ToolCallChunk(name="write", index=0)])
        acc.accumulate_tool_call_chunk(chunk)
        assert len(acc.pop_new_starts()) == 1
        assert len(acc.pop_new_starts()) == 0

    def test_reset_clears_state(self):
        acc = _StreamingAccumulator()
        chunk = _Chunk(tool_call_chunks=[_ToolCallChunk(name="write", index=0)])
        acc.accumulate_tool_call_chunk(chunk)
        acc.pop_new_starts()
        acc.reset()
        assert acc.pop_new_starts() == []

    def test_multiple_indices(self):
        acc = _StreamingAccumulator()
        chunk = _Chunk(tool_call_chunks=[
            _ToolCallChunk(name="read", index=0),
            _ToolCallChunk(name="write", index=1),
        ])
        acc.accumulate_tool_call_chunk(chunk)
        starts = acc.pop_new_starts()
        assert len(starts) == 2

    def test_fragmented_args_accumulated(self):
        acc = _StreamingAccumulator()
        acc.accumulate_tool_call_chunk(
            _Chunk(tool_call_chunks=[_ToolCallChunk(args='{"pa', index=0)])
        )
        acc.accumulate_tool_call_chunk(
            _Chunk(tool_call_chunks=[_ToolCallChunk(args='th": "x"}', index=0)])
        )
        calls = acc.resolved_calls()
        assert len(calls) == 1
        assert calls[0]["args"] == '{"path": "x"}'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestExtractInterrupt:
    def test_graph_interrupt_with_question(self):
        payload = {"question": "test?", "type": "missing_info"}
        gi = _GraphInterruptError((_Interrupt(value=payload),))
        result = _extract_interrupt(gi)
        assert result == payload

    def test_graph_interrupt_without_question(self):
        """Interrupt without question field → None (not an ask_clarification)."""
        gi = _GraphInterruptError((_Interrupt(value={"error": "something"}),))
        assert _extract_interrupt(gi) is None

    def test_regular_exception(self):
        assert _extract_interrupt(ValueError("not interrupt")) is None

    def test_none(self):
        assert _extract_interrupt(None) is None


class TestExtractContent:
    def test_string_content(self):
        assert _extract_text_content(_Chunk(content="hello")) == "hello"

    def test_list_content(self):
        chunk = _Chunk(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        assert _extract_text_content(chunk) == "ab"

    def test_empty_content(self):
        assert _extract_text_content(_Chunk(content="")) == ""

    def test_thinking_from_additional_kwargs(self):
        chunk = _Chunk(additional_kwargs={"reasoning_content": "reasoning"})
        assert _extract_thinking_content(chunk) == "reasoning"

    def test_thinking_from_content_blocks(self):
        chunk = _Chunk(content=[{"type": "thinking", "thinking": "deep thought"}])
        assert _extract_thinking_content(chunk) == "deep thought"

"""astream_events → application-layer event adapter.

:func:`stream_events_to_app_events` subscribes to a LangGraph
``astream_events(version="v2")`` iterator and translates the native events into
the eight application-layer :data:`AppEvent` types consumed by the frontend SSE
client. The adapter owns no LLM / tool / IO coupling — it is pure event
plumbing, so the same code serves any backend that drives the agent graph.

Event mapping (see Story v0.1-3 §1):

* ``on_chat_model_stream`` → ``text_delta`` (text) / ``thinking_delta``
  (reasoning, only when enabled); ``tool_call_chunks`` are *accumulated*.
* ``on_chat_model_end`` → ``thinking`` + ``text`` (incl. intermediate
  text persisted before tool calls) + one ``tool_call`` per resolved call.
* ``on_tool_start`` → ``tool_call_start`` placeholder.
* ``on_tool_end`` → ``tool_result``.
* ``on_llm_error`` / ``on_tool_error`` → ``error``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .app_event import (
    ErrorEvent,
    InterruptEvent,
    TextDeltaEvent,
    TextEvent,
    ThinkingDeltaEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolCallStartEvent,
    ToolResultEvent,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from .app_event import AppEvent

logger = structlog.get_logger(__name__)

OnEventCallback = "Callable[[AppEvent], Awaitable[None]]"

# Native event kinds we translate; everything else is ignored.
_LLM_ERROR_KINDS = ("on_llm_error", "on_chat_model_error")
_TOOL_ERROR_KINDS = ("on_tool_error",)


def _extract_interrupt(error: Any) -> dict[str, Any] | None:
    """Check if *error* is a GraphInterrupt carrying an ask_clarification payload.

    LangGraph 1.2.x surfaces interrupt() inside a tool as ``on_tool_error``
    with the ``GraphInterrupt`` object in ``data["error"]``.
    ``GraphInterrupt.args[0]`` is a tuple of ``Interrupt`` objects,
    each carrying a ``value`` dict with the interrupt payload.
    """
    # GraphInterrupt stores interrupts tuple in args[0]
    raw = None
    if hasattr(error, "args") and error.args:
        raw = error.args[0]
    elif isinstance(error, tuple):
        raw = error
    if raw is None:
        return None
    # raw is typically a tuple of Interrupt objects
    items = raw if isinstance(raw, tuple) else (raw,)
    for intr in items:
        value = getattr(intr, "value", None)
        if isinstance(value, dict) and "question" in value:
            return value
    return None


async def stream_events_to_app_events(
    astream_iter: AsyncIterator[dict[str, Any]],
    on_event: Callable[[AppEvent], Awaitable[None]],
    *,
    enable_thinking: bool = False,
) -> None:
    """Translate a native ``astream_events`` stream into app-layer events.

    Args:
        astream_iter: The async iterator returned by
            ``graph.astream_events(..., version="v2")``.
        on_event: Async callback invoked once per emitted :class:`AppEvent`.
        enable_thinking: When ``False`` (default) reasoning deltas / the
            complete ``thinking`` event are suppressed even if the model
            returns reasoning content.
    """
    accumulator = _StreamingAccumulator(enable_thinking=enable_thinking)

    async for event in astream_iter:
        kind = event.get("event")
        data = event.get("data") or {}

        if kind == "on_chat_model_stream":
            chunk = data.get("chunk")
            if chunk is None:
                continue

            text = _extract_text_content(chunk)
            if text:
                await on_event(TextDeltaEvent(content=text))

            if enable_thinking:
                thinking = _extract_thinking_content(chunk)
                if thinking:
                    await on_event(ThinkingDeltaEvent(content=thinking))

            accumulator.accumulate_tool_call_chunk(chunk)
            # 检测新的 tool_call — 第一个 chunk 到达时发 tool_call_start
            #（流式占位符），前端据此显示加载动画。
            # tool_call（完整数据）仍只在 on_chat_model_end 发出。
            new_starts = accumulator.pop_new_starts()
            if new_starts:
                logger.info("emit_tool_call_starts", count=len(new_starts), names=[s.get("name", "") for s in new_starts])
            for start in new_starts:
                await on_event(ToolCallStartEvent(tool_name=start.get("name") or ""))

        elif kind == "on_chat_model_end":
            output = data.get("output")
            if output is not None:
                if enable_thinking:
                    reasoning = _extract_thinking_content(output)
                    if reasoning:
                        await on_event(ThinkingEvent(content=reasoning))
                content = _extract_text_content(output)
                if content:
                    await on_event(TextEvent(content=content))
                # 直接发出所有 tool_call 事件（不再缓冲到 on_tool_start）。
                # on_chat_model_end 是 tool_call 的唯一来源，保证每个工具调用
                # 只发出一次。LangGraph 的 astream_events(v2) 会在多个嵌套层级
                # 冒泡 on_tool_start 事件，如果从 on_tool_start 发 tool_call 就会
                # 产生重复。
                for tc in _iter_tool_calls(output):
                    await on_event(
                        ToolCallEvent(
                            tool_name=tc.get("name", ""),
                            args=tc.get("args") or {},
                            id=tc.get("id", ""),
                        )
                    )
            accumulator.reset()

        elif kind == "on_tool_start":
            # 不发任何事件。tool_call 已在 on_chat_model_end 中发出（创建了
            # 'running' 状态的条目），tool_result 在 on_tool_end 中发出。
            # LangGraph 的 astream_events(v2) 会在多个嵌套层级冒泡 on_tool_start，
            # 如果在此发事件就会产生重复条目。
            pass

        elif kind == "on_tool_end":
            output = data.get("output")
            tool_name = event.get("name") or "unknown"
            # Extract content: ToolMessage may stringify with metadata if we
            # naively str() it; use .content when available.
            if output is None:
                content = ""
            elif hasattr(output, "content"):
                content = str(output.content)
            else:
                content = str(output)
            # 检查 ToolMessage 的 status：tool_wrapper 把 ToolException 转成
            # status="error" 的 ToolMessage 回传 LLM。这里同步透传给前端，
            # 让前端能结构化区分工具成功/失败，不再靠正则嗅探文本。
            status = "error" if getattr(output, "status", None) == "error" else "success"
            await on_event(
                ToolResultEvent(
                    tool_name=tool_name,
                    content=content,
                    status=status,
                )
            )

        elif kind in _LLM_ERROR_KINDS:
            await on_event(
                ErrorEvent(message=_error_message(data), source="llm")
            )

        elif kind in _TOOL_ERROR_KINDS:
            # Check if the "error" is actually a GraphInterrupt (from
            # ask_clarification's interrupt() call inside a tool).
            # LangGraph 1.2.x surfaces this as on_tool_error with the
            # GraphInterrupt object in data["error"], rather than as
            # __interrupt__ in on_chain_end (which only ainvoke does).
            error = data.get("error")
            interrupt_payload = _extract_interrupt(error)
            if interrupt_payload is not None:
                await on_event(InterruptEvent(
                    question=interrupt_payload.get("question", ""),
                    clarification_type=interrupt_payload.get("type", "missing_info"),
                    context=interrupt_payload.get("context"),
                    options=interrupt_payload.get("options"),
                    fields=interrupt_payload.get("fields"),
                    interrupt_id="",
                ))
            else:
                await on_event(
                    ErrorEvent(message=_error_message(data), source="tool")
                )

        elif kind == "on_chain_end":
            # Detect graph-level interrupt (ask_clarification etc.).
            # LangGraph surfaces an interrupt as a top-level on_chain_end
            # whose output dict carries a ``__interrupt__`` key.
            output = data.get("output")
            if isinstance(output, dict):
                interrupts = output.get("__interrupt__")
                if interrupts:
                    for intr in interrupts:
                        payload = getattr(intr, "value", intr) or {}
                        if isinstance(payload, dict):
                            await on_event(InterruptEvent(
                                question=payload.get("question", ""),
                                clarification_type=payload.get("type", "missing_info"),
                                context=payload.get("context"),
                                options=payload.get("options"),
                                fields=payload.get("fields"),
                                interrupt_id=getattr(intr, "id", "") or "",
                            ))


# ---------------------------------------------------------------------------
# on_chat_model_end emission (shared logic)
# ---------------------------------------------------------------------------


async def _emit_model_end(
    output: Any,
    on_event: Callable[[AppEvent], Awaitable[None]],
    *,
    enable_thinking: bool,
) -> None:
    """Emit the complete thinking / text / tool-call events."""
    if output is None:
        return

    # 1. Complete thinking (only when enabled).
    if enable_thinking:
        reasoning = _extract_thinking_content(output)
        if reasoning:
            await on_event(ThinkingEvent(content=reasoning))

    # 2. Text — emitted whenever there is content, including the
    #    "intermediate text persisted" case (content + tool_calls together).
    content = _extract_text_content(output)
    if content:
        await on_event(TextEvent(content=content))

    # 3. One tool_call event per resolved call.
    for tc in _iter_tool_calls(output):
        await on_event(
            ToolCallEvent(
                tool_name=tc.get("name", ""),
                args=tc.get("args") or {},
                id=tc.get("id", ""),
            )
        )


# ---------------------------------------------------------------------------
# Streaming accumulator (tool_call_chunks → resolved calls)
# ---------------------------------------------------------------------------


class _StreamingAccumulator:
    """Accumulate ``tool_call_chunks`` across stream events.

    LangGraph streams a single tool call as multiple chunks (the ``id`` /
    ``name`` arrive early; ``args`` JSON is fragmented). This accumulates them
    per-call. When the first chunk with a ``name`` arrives for a given index,
    :meth:`pop_new_starts` returns it so the adapter can emit a
    ``tool_call_start`` event (showing a loading indicator before the full
    args are available). The resolved ``tool_call`` events come from
    ``output.tool_calls`` at ``on_chat_model_end``.
    """

    def __init__(self, *, enable_thinking: bool = False) -> None:
        self.enable_thinking = enable_thinking
        self._calls: dict[str, dict[str, Any]] = {}
        # Indices for which a tool_call_start has already been emitted.
        self._starts_emitted: set[str] = set()

    def accumulate_tool_call_chunk(self, chunk: Any) -> None:
        chunks = getattr(chunk, "tool_call_chunks", None)
        if not chunks:
            return
        logger.debug("accumulate_tool_call_chunk", count=len(chunks))
        for piece in chunks:
            index = getattr(piece, "index", 0)
            key = str(index)
            entry = self._calls.setdefault(
                key, {"name": "", "args": "", "id": ""}
            )
            name = getattr(piece, "name", None)
            if name:
                entry["name"] = name
            piece_id = getattr(piece, "id", None)
            if piece_id:
                entry["id"] = piece_id
            args = getattr(piece, "args", None)
            if args:
                entry["args"] += args

    def pop_new_starts(self) -> list[dict[str, str]]:
        """Return entries for newly-detected tool calls (for tool_call_start).

        Detects as soon as ANY chunk arrives for a given index — the ``name``
        may arrive in a later chunk. The frontend shows a loading placeholder
        immediately; the ``tool_call`` event at model-end fills in the real
        name/args. Each index is returned exactly once.
        """
        result: list[dict[str, str]] = []
        for key, entry in self._calls.items():
            if key not in self._starts_emitted:
                self._starts_emitted.add(key)
                result.append({"name": entry.get("name") or "", "id": entry.get("id", "")})
        return result

    def reset(self) -> None:
        self._calls.clear()
        self._starts_emitted.clear()

    def resolved_calls(self) -> list[dict[str, Any]]:
        """Return accumulated calls (best-effort; args kept as raw string)."""
        return list(self._calls.values())


# ---------------------------------------------------------------------------
# Content extraction helpers (str / list-of-blocks tolerant)
# ---------------------------------------------------------------------------


def _extract_text_content(message: Any) -> str:
    """Pull the answer text delta from a chunk / message.

    Handles plain-string ``content`` and list-of-blocks ``content``
    (``[{"type": "text", "text": "..."}, ...]``).
    """
    content = getattr(message, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _extract_thinking_content(message: Any) -> str:
    """Pull reasoning text from a chunk / message.

    Looks first at ``additional_kwargs.reasoning_content`` (OpenAI-style),
    then falls back to ``reasoning_content`` attribute, then to
    ``type="thinking"`` blocks in ``content`` (Anthropic-style).
    """
    additional = getattr(message, "additional_kwargs", None) or {}
    reasoning = additional.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return reasoning

    direct = getattr(message, "reasoning_content", None)
    if isinstance(direct, str) and direct:
        return direct

    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                parts.append(block.get("thinking") or "")
        joined = "".join(parts)
        if joined:
            return joined
    return ""


def _iter_tool_calls(message: Any) -> list[dict[str, Any]]:
    """Return the resolved tool_calls list from a message (empty if none)."""
    calls = getattr(message, "tool_calls", None)
    if not calls:
        return []
    return [c if isinstance(c, dict) else dict(c) for c in calls]


def _error_message(data: dict[str, Any]) -> str:
    err = data.get("error")
    if isinstance(err, BaseException):
        return str(err)
    if err is not None:
        return str(err)
    return "unknown error"


__all__ = ["OnEventCallback", "stream_events_to_app_events"]

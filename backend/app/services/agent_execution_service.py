"""Agent execution service — business orchestration for invoke/stream/resume.

Encapsulates the session management, message persistence, prompt assembly,
and harness execution that was previously inlined in the API endpoints.
The API layer (agents.py) delegates here for all execution-related flows.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from langchain_core.messages import SystemMessage
from loguru import logger

from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.engine.agent.builder import build_system_prompt
from app.schemas.execution import ExecutionRequest, ExecutionResponse, ResumeRequest
from app.services.agent_service import AgentService
from app.services.file_rendering import (
    render_attachments_block,
    render_files_by_ids,
    render_files_by_paths,
)
from app.services.message_converters import (
    extract_final_answer,
    messages_to_timeline_entries,
    safe_json,
)
from app.services.session_service import MessageService, SessionService


def _now_ms() -> int:
    """Current epoch time in milliseconds (for execution-latency timing)."""
    return int(time.time() * 1000)


class AgentExecutionService:
    """Orchestrates agent execution: session, persistence, prompt, harness."""

    # ------------------------------------------------------------------
    # Invoke (synchronous)
    # ------------------------------------------------------------------

    @staticmethod
    async def invoke(
        agent_id: str,
        body: ExecutionRequest,
        user_id: str,
        *,
        external_call_chain: list[str] | None = None,
        user_token: str | None = None,
    ) -> ExecutionResponse:
        """Execute an agent synchronously and persist the result.

        Returns an ExecutionResponse with the agent's output text.
        """
        from app.engine.harness_integration import invoke as harness_invoke

        exec_doc = await AgentService.get_agent(agent_id)
        if exec_doc is None:
            raise NotFoundError(code="AGENT_NOT_FOUND", message=f"Agent {agent_id} 不存在")

        session_id = await _resolve_session(agent_id, body, user_id)
        request_id = str(uuid.uuid4())
        call_chain = [*(external_call_chain or []), agent_id]
        start_time_ms = _now_ms()

        # Build messages (system prompt + user input with file attachments)
        system_text = await _build_system_prompt_checked(exec_doc)
        user_content = await _build_user_content(body, user_id, session_id)
        initial_messages = _assemble_messages(system_text, user_content)

        # Legacy migration records
        legacy_records = await _load_legacy_records(session_id, body.input)

        initial_state = _build_initial_state(
            agent_id, session_id, user_id, request_id, call_chain,
            external_call_chain, initial_messages,
        )

        run_error: BaseException | None = None
        try:
            result = await harness_invoke(
                exec_doc, initial_state,
                enable_thinking=body.enable_thinking,
                legacy_records=legacy_records,
                user_token=user_token,
            )
        except Exception as exc:
            run_error = exc
            result = {}
            raise
        finally:
            # Unified execution log (all channels) + ext audit log (ext only).
            await _record_execution_log(
                user_id=user_id, agent_id=agent_id, session_id=session_id,
                request_id=request_id, start_time_ms=start_time_ms,
                token_usage=result.get("usage"), error=run_error,
            )

        # Extract output + persist agent message
        output_text = extract_final_answer(result.get("messages", []))
        timeline = messages_to_timeline_entries(
            result.get("messages", []), enable_thinking=body.enable_thinking,
        )
        await MessageService.add_message(
            session_id=session_id, role="agent", timeline_entries=timeline,
        )

        return ExecutionResponse(
            output=output_text,
            execution_path=result.get("execution_path", "unknown"),
            request_id=request_id,
            agent_id=agent_id,
            session_id=session_id,
            step_count=result.get("step_count", 0),
        )

    # ------------------------------------------------------------------
    # Stream (SSE)
    # ------------------------------------------------------------------

    @staticmethod
    async def stream(
        agent_id: str,
        body: ExecutionRequest,
        user_id: str,
        *,
        external_call_chain: list[str] | None = None,
        user_token: str | None = None,
    ) -> tuple[asyncio.Queue, str, str]:
        """Start a streaming agent execution in the background.

        Returns (event_queue, request_id, session_id). The caller wraps
        the queue into a StreamingResponse. The background task pushes
        SSE-formatted events and persists the agent message on completion.
        """
        from app.engine.harness_integration import stream as harness_stream

        exec_doc = await AgentService.get_agent(agent_id)
        if exec_doc is None:
            raise NotFoundError(code="AGENT_NOT_FOUND", message=f"Agent {agent_id} 不存在")

        session_id = await _resolve_session(agent_id, body, user_id)
        request_id = str(uuid.uuid4())
        call_chain = [*(external_call_chain or []), agent_id]

        system_text = await _build_system_prompt_checked(exec_doc)

        event_queue: asyncio.Queue[str | None] = asyncio.Queue()
        collected_timeline: list[dict] = []

        async def _on_event(event: dict) -> None:
            collected_timeline.append(event)
            await event_queue.put(f"data: {safe_json(event)}\n\n")

        async def _run():
            start_time_ms = _now_ms()
            user_content = await _build_user_content(body, user_id, session_id)
            initial_messages = _assemble_messages(system_text, user_content)
            legacy_records = await _load_legacy_records(session_id, body.input)

            initial_state = _build_initial_state(
                agent_id, session_id, user_id, request_id, call_chain,
                external_call_chain, initial_messages, execution_path="react",
            )
            run_error: BaseException | None = None
            try:
                result = await harness_stream(
                    exec_doc, initial_state,
                    on_event=_on_event,
                    enable_thinking=body.enable_thinking,
                    legacy_records=legacy_records,
                    user_token=user_token,
                )
                logger.info(
                    "agent_stream_completed",
                    agent_id=agent_id, request_id=request_id,
                    step_count=result.get("step_count", 0),
                )
            except Exception as exc:
                run_error = exc
                logger.error("agent_stream_error", agent_id=agent_id, request_id=request_id, error=str(exc))
                logger.exception("agent_stream_error_traceback")
                # 走 ErrorEvent schema 保留 source 字段，前端可据此区分
                # 错误来源（llm/tool/graph），不再丢字段。
                from app.engine.harness_integration.adapters.app_event import ErrorEvent

                err_evt = ErrorEvent(
                    message=str(exc),
                    source=_classify_error_source(exc),
                ).model_dump()
                # 前端契约用 content，ErrorEvent 用 message，做字段重映射
                err_evt["content"] = err_evt.pop("message", "")
                await event_queue.put(f"data: {safe_json(err_evt)}\n\n")
                result = {}
            finally:
                await _persist_agent_message(
                    session_id, collected_timeline,
                    token_usage=result.get("usage"),
                )
                # Unified execution log (all channels) + ext audit log (ext only).
                await _record_execution_log(
                    user_id=user_id, agent_id=agent_id, session_id=session_id,
                    request_id=request_id, start_time_ms=start_time_ms,
                    token_usage=result.get("usage"), error=run_error,
                )
                await event_queue.put(
                    f"data: {safe_json({'done': True, 'request_id': request_id, 'session_id': session_id, 'usage': result.get('usage', {})})}\n\n"
                )
                await event_queue.put(None)

        asyncio.create_task(_run())
        return event_queue, request_id, session_id

    # ------------------------------------------------------------------
    # Resume (SSE, after interrupt)
    # ------------------------------------------------------------------

    @staticmethod
    async def resume(
        agent_id: str,
        body: ResumeRequest,
        user_id: str,
        *,
        user_token: str | None = None,
    ) -> tuple[asyncio.Queue, str, str]:
        """Resume an interrupted agent and stream the continued execution."""
        from app.engine.harness_integration import resume as harness_resume

        exec_doc = await AgentService.get_agent(agent_id)
        if exec_doc is None:
            raise NotFoundError(code="AGENT_NOT_FOUND", message=f"Agent {agent_id} 不存在")

        request_id = str(uuid.uuid4())
        session_id = body.session_id

        # Note: user's answer is NOT stored as a separate user message.
        # It flows through interrupt() → ToolMessage (tool_result for
        # ask_clarification) in the agent's timeline, rendered as user input
        # by the frontend. This keeps the conversation history accurate —
        # the answer belongs to the tool call, not a standalone message.

        event_queue: asyncio.Queue[str | None] = asyncio.Queue()
        collected_timeline: list[dict] = []

        async def _on_event(event: dict) -> None:
            collected_timeline.append(event)
            await event_queue.put(f"data: {safe_json(event)}\n\n")

        async def _run():
            start_time_ms = _now_ms()
            state = {
                "messages": [], "agent_id": agent_id,
                "session_id": session_id, "user_id": user_id,
            }
            run_error: BaseException | None = None
            try:
                result = await harness_resume(
                    exec_doc, state, _on_event, body.answer,
                    enable_thinking=body.enable_thinking,
                    user_token=user_token,
                )
            except Exception as exc:
                run_error = exc
                logger.error("agent_resume_error", agent_id=agent_id, error=str(exc))
                logger.exception("agent_resume_error_traceback")
                from app.engine.harness_integration.adapters.app_event import ErrorEvent

                err_evt = ErrorEvent(
                    message=str(exc),
                    source=_classify_error_source(exc),
                ).model_dump()
                err_evt["content"] = err_evt.pop("message", "")
                await event_queue.put(f"data: {safe_json(err_evt)}\n\n")
                result = {}
            finally:
                await _persist_agent_message(
                    session_id, collected_timeline,
                    extra_filter_types=("interrupt",),
                    token_usage=result.get("usage"),
                    append_to_last_agent=True,
                )
                # Unified execution log (all channels) + ext audit log (ext only).
                await _record_execution_log(
                    user_id=user_id, agent_id=agent_id, session_id=session_id,
                    request_id=request_id, start_time_ms=start_time_ms,
                    token_usage=result.get("usage"), error=run_error,
                )
                await event_queue.put(
                    f"data: {safe_json({'done': True, 'request_id': request_id, 'session_id': session_id, 'usage': result.get('usage', {})})}\n\n"
                )
                await event_queue.put(None)

        asyncio.create_task(_run())
        return event_queue, request_id, session_id


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TRANSIENT_EVENT_TYPES = ("text_delta", "thinking_delta", "tool_call_start", "interrupt")


async def _resolve_session(agent_id: str, body: ExecutionRequest, user_id: str) -> str:
    """Resolve or create a session, then persist the user message."""
    session_id = body.session_id or ""
    if not session_id:
        session_doc = await SessionService.create_session(
            user_id=user_id, agent_id=agent_id, title=body.input[:200],
        )
        session_id = session_doc["_id"]
    await MessageService.add_message(
        session_id=session_id, role="user",
        content=body.input, file_ids=body.file_ids or None,
    )
    return session_id


async def _build_system_prompt_checked(exec_doc: dict) -> str:
    """Build system prompt, raising ValidationError on slot issues."""
    try:
        return await build_system_prompt(exec_doc)
    except ValueError as exc:
        raise ValidationError(code="AGENT_PROMPT_SLOT_MISSING", message=str(exc)) from exc


async def _build_user_content(body: ExecutionRequest, user_id: str, session_id: str) -> str:
    """Embed uploaded file contents into the user message text."""
    user_content = body.input
    try:
        if body.file_ids:
            blocks = await render_files_by_ids(body.file_ids)
            if blocks:
                user_content += render_attachments_block(blocks)
        elif body.file_paths:
            from app.engine.tool.workspace import WorkspaceManager
            ws = WorkspaceManager.get_workspace(user_id, session_id)
            blocks = await render_files_by_paths(body.file_paths, ws.root)
            if blocks:
                user_content += render_attachments_block(blocks)
    except Exception:
        pass
    return user_content


def _assemble_messages(system_text: str, user_content: str) -> list:
    """Build the initial messages list (System + User)."""
    messages: list = []
    if system_text:
        messages.append(SystemMessage(content=system_text, id="sys"))
    messages.append({"role": "user", "content": user_content})
    return messages


async def _load_legacy_records(session_id: str, current_input: str) -> list[dict]:
    """Load legacy history records for thread migration (if enabled)."""
    if not session_id or not settings.MIGRATE_LEGACY_SESSIONS:
        return []
    try:
        records = await MessageService.list_messages(session_id)
        return [
            r for r in records
            if r.get("content") != current_input or r.get("role") != "user"
        ]
    except Exception:
        return []


def _build_initial_state(
    agent_id: str,
    session_id: str,
    user_id: str,
    request_id: str,
    call_chain: list[str],
    external_chain: list[str] | None,
    messages: list,
    execution_path: str = "",
) -> dict[str, Any]:
    return {
        "messages": messages,
        "agent_id": agent_id,
        "execution_path": execution_path,
        "request_id": request_id,
        "tool_results": {},
        "step_count": 0,
        "error": None,
        "call_chain": call_chain,
        "current_depth": len(external_chain or []),
        "session_id": session_id,
        "user_id": user_id,
    }


# LLM 类异常的类型名关键词（主流 LLM 库：openai / anthropic / 通义等）。
# 这些异常在模型欠费、限流、超时、鉴权失败时抛出。
_LLM_ERROR_TYPE_KEYWORDS = (
    "ratelimit", "rate_limit", "apitimeout", "authentication",
    "permissiondenied", "insufficient_quota", "quotaexceeded",
    "connection", "timeout",
)
# 错误消息中的 LLM 类关键词。
_LLM_ERROR_MSG_KEYWORDS = (
    "rate limit", "quota", "insufficient_quota", "余额不足", "欠费",
    "api key", "invalid_api_key", "authentication",
    "model_not_found", "context_length_exceeded",
)


def _classify_error_source(exc: BaseException) -> str:
    """根据异常类型/消息判定错误来源，供前端区分展示。

    返回 "llm"（模型欠费/限流/超时/鉴权）、"tool"（工具加载/配置错误）、
    或 "graph"（其余含服务端内部异常）。

    注意：按"所有错误都暴露给前端"的原则，这里只分类来源，不脱敏——
    顶层异常的 str(exc) 会原样发给前端。
    """
    type_name = type(exc).__name__.lower()
    msg = str(exc).lower()

    # LLM 类异常：匹配类型名或消息关键词
    if any(kw in type_name for kw in _LLM_ERROR_TYPE_KEYWORDS):
        return "llm"
    if any(kw in msg for kw in _LLM_ERROR_MSG_KEYWORDS):
        return "llm"

    return "graph"


async def _persist_agent_message(
    session_id: str,
    collected_timeline: list[dict],
    *,
    extra_filter_types: tuple[str, ...] = (),
    token_usage: dict | None = None,
    append_to_last_agent: bool = False,
) -> None:
    """Filter transient events and persist the agent message.

    Args:
        append_to_last_agent: If True, append to the last agent message instead
            of creating a new one. Used by resume so that tool_call and
            tool_result for ask_clarification end up in the same message.
    """
    filter_types = _TRANSIENT_EVENT_TYPES + extra_filter_types
    persistence_timeline = [
        e for e in collected_timeline
        if e.get("type") not in filter_types
    ]
    if persistence_timeline:
        try:
            if append_to_last_agent:
                await MessageService.append_to_last_agent_message(
                    session_id, persistence_timeline, token_usage=token_usage or {},
                )
            else:
                await MessageService.add_message(
                    session_id=session_id, role="agent",
                    timeline_entries=persistence_timeline,
                    token_usage=token_usage or {},
                )
            # Accumulate token usage on the session
            if token_usage and token_usage.get("total_tokens"):
                await SessionService.add_tokens(session_id, token_usage["total_tokens"])
        except Exception as exc:
            logger.error("agent_stream_persist_error", error=str(exc))


async def _record_execution_log(
    *,
    user_id: str,
    agent_id: str,
    session_id: str,
    request_id: str,
    start_time_ms: int,
    token_usage: dict | None,
    error: BaseException | None = None,
) -> None:
    """Write one unified execution_logs record for ANY agent call.

    Writes unconditionally for internal / api_key / im calls alike. Channel
    (source) is derived from ``user_id`` by the service. External-only
    fields (api_key_id / user_sub / endpoint / visitor_id) are pulled from
    the stashed ExtCallContext when present, and the context is marked
    consumed so the stats middleware fallback skips the duplicate write.

    Failure is logged but never raised — execution logging must not
    break the user-facing request flow.
    """
    import time as _time

    from app.services.execution_log_service import ExecutionLogService
    from app.services.ext_api_call_log_service import get_ext_call_context

    usage = token_usage or {}
    latency_ms = int(_time.time() * 1000) - start_time_ms
    status = "error" if error is not None else "success"

    # External calls carry api_key_id / user_sub / endpoint / visitor_id
    # in the stashed context.
    api_key_id = ""
    user_sub = ""
    visitor_id = ""
    endpoint = ""
    ctx = get_ext_call_context()
    if ctx is not None:
        api_key_id = ctx.api_key_id
        user_sub = ctx.user_sub
        visitor_id = ctx.visitor_id
        endpoint = ctx.endpoint
        ctx.consumed = True  # suppress middleware fallback duplicate

    await ExecutionLogService.write_log(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        request_id=request_id,
        api_key_id=api_key_id,
        user_sub=user_sub,
        visitor_id=visitor_id,
        endpoint=endpoint,
        status=status,
        status_code=500 if error is not None else 200,
        latency_ms=latency_ms,
        total_tokens=int(usage.get("total_tokens") or 0),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        llm_calls=int(usage.get("llm_calls") or 0),
    )


__all__ = ["AgentExecutionService"]

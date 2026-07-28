"""External API — Agent resource discovery and invocation."""
import asyncio

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.v1.ext import auth_and_rate_limit, resolve_user_id
from app.core.auth_apikey import ApiKeyPrincipal
from app.core.errors import NotFoundError
from app.models.agent import AgentStatus
from app.schemas.execution import ExecutionRequest, ResumeRequest
from app.schemas.ext_api import (
    ExtAgentCapabilities,
    ExtAgentListResponse,
    ExtAgentResponse,
    ExtInvokeRequest,
    ExtInvokeResponse,
    ExtResumeRequest,
    ExtSessionDetailResponse,
    ExtSessionListResponse,
    ExtSessionResponse,
)
from app.services.agent_execution_service import AgentExecutionService
from app.services.agent_service import AgentService
from app.services.session_service import SessionService

router = APIRouter(tags=["external-agents"])


def _doc_to_ext_response(doc: dict) -> ExtAgentResponse:
    """Convert an internal Agent document to external response format."""
    llm_config = doc.get("llm_config") or {}
    default_model = doc.get("default_model") or llm_config.get("default_model", "")

    # Resolve tool names from skill_ids (simplified — use IDs as names for now)
    from app.models.compat import resolve_skill_ids
    skill_ids = resolve_skill_ids(doc)

    return ExtAgentResponse(
        id=doc["_id"],
        name=doc["name"],
        description=doc.get("description", ""),
        capabilities=ExtAgentCapabilities(
            tools=skill_ids,
            workflow_ids=doc.get("workflow_ids", []),
        ),
        default_model=default_model,
        status=doc["status"],
        welcome_message=doc.get("welcome_message", ""),
        recommended_items=doc.get("recommended_items", []),
    )


# ---------------------------------------------------------------------------
# Resource discovery
# ---------------------------------------------------------------------------


@router.get(
    "/agents",
    response_model=ExtAgentListResponse,
    summary="List accessible Agents",
)
async def list_agents(
    page: int = 1,
    page_size: int = 20,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> ExtAgentListResponse:
    """List published Agents accessible to this API Key.

    Results are filtered by the Key's ``bindings.agents``.
    Empty bindings = all published Agents.
    """
    principal.require_scope("agents:read")

    # Fetch all published agents
    items, total = await AgentService.list_agents(
        page=page,
        page_size=page_size,
        status=AgentStatus.PUBLISHED.value,
    )

    # Filter by bindings
    allowed_ids = principal.bindings.get("agents", [])
    if allowed_ids:
        items = [d for d in items if d["_id"] in allowed_ids]
        # Recalculate total for filtered results
        if len(items) < page_size:
            total = len(items)

    return ExtAgentListResponse(
        items=[_doc_to_ext_response(d) for d in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/agents/{agent_id}",
    response_model=ExtAgentResponse,
    summary="Get Agent details",
)
async def get_agent(
    agent_id: str,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> ExtAgentResponse:
    """Get Agent details. Requires ``agents:read`` scope and binding access."""
    principal.require_scope("agents:read")
    principal.require_agent_access(agent_id)

    doc = await AgentService.get_agent(agent_id)
    if doc is None or doc.get("status") != AgentStatus.PUBLISHED.value:
        raise NotFoundError(code="AGENT_NOT_FOUND", message="Agent not found")

    return _doc_to_ext_response(doc)


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


@router.post(
    "/agents/{agent_id}/invoke",
    response_model=ExtInvokeResponse,
    summary="Invoke Agent (synchronous)",
)
async def invoke_agent(
    agent_id: str,
    body: ExtInvokeRequest,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> ExtInvokeResponse:
    """Invoke an Agent synchronously.

    The Agent processes the message and returns its text response.
    If the Agent triggers a Workflow Task, the task_ids are returned
    for the caller to poll via ``GET /ext/tasks/{task_id}``.
    """
    principal.require_scope("agents:invoke")
    principal.require_agent_access(agent_id)

    # Map external request to internal ExecutionRequest
    exec_request = ExecutionRequest(
        input=body.message,
        session_id=body.session_id,
        enable_thinking=body.enable_thinking,
        file_paths=body.file_paths,
        file_ids=body.file_ids,
    )

    result = await AgentExecutionService.invoke(
        agent_id=agent_id,
        body=exec_request,
        user_id=resolve_user_id(principal, body.visitor_id),
        user_token=principal.user_token,
    )

    # Extract task_ids from execution result (if any workflow was triggered)
    # The ExecutionResponse doesn't currently include task_ids — for now return empty.
    # TODO: enhance ExecutionResponse to include task_ids from agent execution.
    return ExtInvokeResponse(
        session_id=result.session_id,
        request_id=result.request_id,
        reply=result.output,
        task_ids=[],
        files=[],
    )


@router.post(
    "/agents/{agent_id}/invoke/stream",
    summary="Invoke Agent (SSE stream)",
)
async def stream_agent(
    agent_id: str,
    body: ExtInvokeRequest,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> StreamingResponse:
    """Invoke an Agent and stream results via Server-Sent Events."""
    principal.require_scope("agents:invoke")
    principal.require_agent_access(agent_id)

    exec_request = ExecutionRequest(
        input=body.message,
        session_id=body.session_id,
        enable_thinking=body.enable_thinking,
        file_paths=body.file_paths,
        file_ids=body.file_ids,
    )

    event_queue, request_id, session_id = await AgentExecutionService.stream(
        agent_id=agent_id,
        body=exec_request,
        user_id=resolve_user_id(principal, body.visitor_id),
        user_token=principal.user_token,
    )

    async def _event_stream():
        task = asyncio.current_task()
        try:
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                yield item
        finally:
            if task and not task.done():
                task.cancel()

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Request-Id": request_id,
            "X-Session-Id": session_id,
        },
    )


@router.post(
    "/agents/{agent_id}/invoke/resume",
    summary="Resume interrupted Agent (SSE stream)",
)
async def resume_agent(
    agent_id: str,
    body: ExtResumeRequest,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> StreamingResponse:
    """Resume an Agent that was interrupted (ask_clarification)."""
    principal.require_scope("agents:invoke")
    principal.require_agent_access(agent_id)

    resume_request = ResumeRequest(
        session_id=body.session_id,
        answer=body.answer,
        enable_thinking=body.enable_thinking,
    )

    event_queue, request_id, session_id = await AgentExecutionService.resume(
        agent_id=agent_id,
        body=resume_request,
        user_id=resolve_user_id(principal, body.visitor_id),
        user_token=principal.user_token,
    )

    async def _event_stream():
        task = asyncio.current_task()
        try:
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                yield item
        finally:
            if task and not task.done():
                task.cancel()

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Request-Id": request_id,
            "X-Session-Id": session_id,
        },
    )


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


@router.post(
    "/agents/{agent_id}/sessions",
    status_code=201,
    response_model=ExtSessionResponse,
    summary="Create a session (external)",
)
async def create_session(
    agent_id: str,
    visitor_id: str | None = Query(
        None, description="访客 ID（兼容模式必填，回调验证模式忽略）"
    ),
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> ExtSessionResponse:
    """Create a new session for the current end-user.

    The session is attributed via ``resolve_user_id`` (legacy:
    ``{owner}:{visitor_id}``; callback: ``{owner}:{sub}``), matching how
    ``invoke`` attributes sessions so subsequent calls find it.
    """
    principal.require_scope("agents:invoke")
    principal.require_agent_access(agent_id)

    user_id = resolve_user_id(principal, visitor_id)
    doc = await SessionService.create_session(
        user_id=user_id,
        agent_id=agent_id,
        title="",
    )
    return ExtSessionResponse(
        id=doc["_id"],
        title=doc.get("title", ""),
        created_at=doc.get("created_at", ""),
        updated_at=doc.get("updated_at", ""),
        message_count=doc.get("message_count", 0),
    )


@router.get(
    "/agents/{agent_id}/sessions",
    response_model=ExtSessionListResponse,
    summary="List visitor sessions",
)
async def list_visitor_sessions(
    agent_id: str,
    visitor_id: str | None = Query(None, description="访客 ID（兼容模式必填，回调验证模式忽略）"),
    page: int = 1,
    page_size: int = 20,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> ExtSessionListResponse:
    """List sessions for the current end-user.

    Sessions are keyed by ``user_id``:
    - Legacy mode: ``{owner_user_id}:{visitor_id}``
    - Callback-verification mode: ``{owner_user_id}:{sub}`` (visitor_id ignored)
    """
    principal.require_scope("agents:invoke")
    principal.require_agent_access(agent_id)

    user_id = resolve_user_id(principal, visitor_id)

    items, total = await SessionService.list_sessions(
        user_id=user_id,
        agent_id=agent_id,
        page=page,
        page_size=page_size,
    )

    return ExtSessionListResponse(
        items=[
            ExtSessionResponse(
                id=doc["_id"],
                title=doc.get("title", ""),
                created_at=doc.get("created_at", ""),
                updated_at=doc.get("updated_at", ""),
                message_count=doc.get("message_count", 0),
            )
            for doc in items
        ],
        total=total,
    )


@router.get(
    "/sessions/{session_id}",
    summary="Get session detail with messages",
)
async def get_session_detail(
    session_id: str,
    visitor_id: str | None = Query(None, description="访客 ID（兼容模式必填，回调验证模式忽略）"),
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> "ExtSessionDetailResponse":
    """Get session detail including all messages.

    Verifies that the session belongs to the current end-user.
    """
    principal.require_scope("agents:invoke")

    user_id = resolve_user_id(principal, visitor_id)

    # Get session and verify ownership
    session_doc = await SessionService.get_session(session_id)
    if session_doc is None or session_doc.get("user_id") != user_id:
        raise NotFoundError(code="SESSION_NOT_FOUND", message="会话不存在")

    # Get messages
    from app.services.session_service import MessageService
    messages = await MessageService.list_messages(session_id)

    from app.schemas.ext_api import ExtMessageResponse, ExtSessionDetailResponse
    return ExtSessionDetailResponse(
        id=session_doc["_id"],
        title=session_doc.get("title", ""),
        created_at=session_doc.get("created_at", ""),
        updated_at=session_doc.get("updated_at", ""),
        messages=[
            ExtMessageResponse(
                id=msg["_id"],
                role=msg["role"],
                content=msg.get("content", ""),
                timeline_entries=msg.get("timeline_entries", []),
                created_at=msg.get("created_at", ""),
            )
            for msg in messages
        ],
    )


@router.delete(
    "/sessions/{session_id}",
    summary="Delete a visitor session",
)
async def delete_session(
    session_id: str,
    visitor_id: str | None = Query(None, description="访客 ID（兼容模式必填，回调验证模式忽略）"),
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> JSONResponse:
    """Delete a session and all its messages.

    Verifies that the session belongs to the current end-user.
    """
    principal.require_scope("agents:invoke")

    user_id = resolve_user_id(principal, visitor_id)

    # Verify ownership before deleting
    session_doc = await SessionService.get_session(session_id)
    if session_doc is None or session_doc.get("user_id") != user_id:
        raise NotFoundError(code="SESSION_NOT_FOUND", message="会话不存在")

    await SessionService.delete_session(session_id)
    return JSONResponse({"ok": True})

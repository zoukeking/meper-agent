"""Agent API endpoints — CRUD operations + execution routing for Agent lifecycle."""
import json

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse

from app.core.errors import NotFoundError
from app.core.security import get_current_user, require_any_role
from app.models.agent import AgentStatus
from app.models.compat import resolve_skill_ids
from app.schemas.agent import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    AgentUpdate,
)
from app.schemas.execution import (
    ExecutionRequest,
    ExecutionResponse,
    PreviewRequest,
    PreviewResponse,
    ResumeRequest,
)
from app.schemas.user import UserResponse
from app.services.agent_execution_service import AgentExecutionService
from app.services.agent_service import AgentService

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    dependencies=[Depends(get_current_user)],
)


def _doc_to_response(doc: dict) -> AgentResponse:
    """Convert a raw MongoDB document to AgentResponse."""
    llm_config = doc.get("llm_config") or {}
    default_model = doc.get("default_model") or llm_config.get("default_model", "")
    max_retry = doc.get("max_retry") if "max_retry" in doc else llm_config.get("max_retry", 3)
    max_tokens = doc.get("max_tokens", 0)

    return AgentResponse(
        id=doc["_id"],
        name=doc["name"],
        description=doc.get("description", ""),
        welcome_message=doc.get("welcome_message", ""),
        recommended_items=doc.get("recommended_items", []),
        prompt_slots=doc.get("prompt_slots", {}),
        skill_ids=resolve_skill_ids(doc),
        mcp_connection_ids=doc.get("mcp_connection_ids", []),
        builtin_config=doc.get("builtin_config", []),
        workflow_ids=doc.get("workflow_ids", []),
        custom_tool_ids=[b.get("tool_id", "") for b in (doc.get("custom_tools") or []) if b.get("tool_id")],
        knowledge_base_ids=doc.get("knowledge_base_ids", []),
        default_model=default_model,
        max_retry=max_retry,
        max_tokens=max_tokens,
        status=AgentStatus(doc["status"]),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def _parse_call_chain(x_call_chain: str | None) -> list[str]:
    """Parse the optional external call chain from the X-Call-Chain header."""
    if not x_call_chain:
        return []
    try:
        parsed = json.loads(x_call_chain)
        if isinstance(parsed, list):
            return [str(e) for e in parsed if isinstance(e, str)]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=AgentListResponse,
    summary="List all Agents",
)
async def list_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    name: str | None = Query(None),
    status: str | None = Query(
        None,
        description="Filter by status (draft/published/archived). "
        'Defaults to "published". Use "all" to return every status.',
    ),
    _: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> AgentListResponse:
    # Default to published so external consumers (workflow editor, etc.)
    # only see production-ready agents. Management pages pass "all".
    effective_status = None if status == "all" else (status or "published")
    items, total = await AgentService.list_agents(
        page=page, page_size=page_size,
        name=name, status=effective_status,
    )
    return AgentListResponse(
        items=[_doc_to_response(doc) for doc in items],
        total=total, page=page, page_size=page_size,
    )


@router.post(
    "",
    response_model=AgentResponse,
    status_code=201,
    summary="Create a new Agent",
)
async def create_agent(
    body: AgentCreate,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> AgentResponse:
    # 新建 Agent 默认启用全部文件类内建工具(bash/read/write/glob/grep)。
    # 白名单语义不变:这里只是给创建入口一个"默认全选"的初值。
    # 注意默认值收敛在端点层而非 model/schema,避免 duplicate_agent 复制
    # 老 Agent 时把空 builtin_config 意外填满(违背"不迁移老数据"的意图)。
    from app.engine.harness_integration.context import DEFAULT_BUILTIN_CONFIG

    doc = await AgentService.create_agent(
        name=body.name,
        description=body.description,
        builtin_config=list(DEFAULT_BUILTIN_CONFIG),
    )
    return _doc_to_response(doc)


@router.get("/{agent_id}", response_model=AgentResponse, summary="Get Agent details")
async def get_agent(
    agent_id: str,
    _: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> AgentResponse:
    doc = await AgentService.get_agent(agent_id)
    if doc is None:
        raise NotFoundError(code="AGENT_NOT_FOUND", message=f"Agent {agent_id} 不存在")
    return _doc_to_response(doc)


@router.put("/{agent_id}", response_model=AgentResponse, summary="Update an Agent")
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> AgentResponse:
    doc = await AgentService.update_agent(
        agent_id=agent_id,
        name=body.name,
        description=body.description,
        prompt_slots=body.prompt_slots,
        skill_ids=body.skill_ids,
        mcp_connection_ids=body.mcp_connection_ids,
        builtin_config=body.builtin_config,
        workflow_ids=body.workflow_ids,
        custom_tool_ids=body.custom_tool_ids,
        knowledge_base_ids=body.knowledge_base_ids,
        default_model=body.default_model,
        max_retry=body.max_retry,
        max_tokens=body.max_tokens,
        welcome_message=body.welcome_message,
        recommended_items=[item.model_dump() for item in body.recommended_items],
    )
    if doc is None:
        raise NotFoundError(code="AGENT_NOT_FOUND", message=f"Agent {agent_id} 不存在")
    return _doc_to_response(doc)


@router.post("/{agent_id}/publish", response_model=AgentResponse, summary="Publish an Agent")
async def publish_agent(
    agent_id: str,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> AgentResponse:
    doc = await AgentService.publish_agent(agent_id)
    if doc is None:
        raise NotFoundError(code="AGENT_NOT_FOUND", message=f"Agent {agent_id} 不存在")
    return _doc_to_response(doc)


@router.post("/{agent_id}/archive", response_model=AgentResponse, summary="Archive an Agent")
async def archive_agent(
    agent_id: str,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> AgentResponse:
    doc = await AgentService.archive_agent(agent_id)
    if doc is None:
        raise NotFoundError(code="AGENT_NOT_FOUND", message=f"Agent {agent_id} 不存在")
    return _doc_to_response(doc)


@router.post(
    "/{agent_id}/duplicate",
    response_model=AgentResponse,
    status_code=201,
    summary="Duplicate an Agent",
)
async def duplicate_agent(
    agent_id: str,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> AgentResponse:
    doc = await AgentService.duplicate_agent(agent_id)
    return _doc_to_response(doc)


@router.delete("/{agent_id}", status_code=204, summary="Delete an Agent")
async def delete_agent(
    agent_id: str,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> None:
    deleted = await AgentService.delete_agent(agent_id)
    if not deleted:
        raise NotFoundError(code="AGENT_NOT_FOUND", message=f"Agent {agent_id} 不存在")


# ---------------------------------------------------------------------------
# Preview (dry-run, no LLM call)
# ---------------------------------------------------------------------------

@router.post(
    "/{agent_id}/preview",
    response_model=PreviewResponse,
    summary="Preview Agent prompt & tools (dry-run)",
)
async def preview_agent(
    agent_id: str,
    body: PreviewRequest | None = None,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> PreviewResponse:
    from app.engine.agent.builder import preview_agent as _preview_agent
    from app.schemas.execution import ToolPreview

    if body is None:
        body = PreviewRequest()

    doc = await AgentService.get_agent(agent_id)
    if doc is None:
        raise NotFoundError(code="AGENT_NOT_FOUND", message=f"Agent {agent_id} 不存在")

    result = await _preview_agent(
        agent=doc, user_input=body.input, enable_thinking=body.enable_thinking,
    )
    return PreviewResponse(
        agent_id=agent_id,
        agent_name=doc.get("name", ""),
        model=result["model"],
        system_prompt=result["system_prompt"],
        messages=result["messages"],
        tools=[ToolPreview(**t) for t in result["tools"]],
        tool_summary=result["tool_summary"],
    )


# ---------------------------------------------------------------------------
# Execution endpoints (delegate to AgentExecutionService)
# ---------------------------------------------------------------------------

@router.post(
    "/{agent_id}/invoke",
    response_model=ExecutionResponse,
    summary="Invoke an Agent (sync)",
)
async def invoke_agent(
    agent_id: str,
    body: ExecutionRequest,
    x_call_chain: str | None = Header(None, alias="X-Call-Chain"),
    user: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> ExecutionResponse:
    """Invoke an Agent synchronously."""
    return await AgentExecutionService.invoke(
        agent_id, body, user.id,
        external_call_chain=_parse_call_chain(x_call_chain),
    )


@router.post(
    "/{agent_id}/stream",
    summary="Invoke an Agent (SSE stream)",
)
async def stream_agent(
    agent_id: str,
    body: ExecutionRequest,
    x_call_chain: str | None = Header(None, alias="X-Call-Chain"),
    user: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> StreamingResponse:
    """Invoke an Agent and stream results via Server-Sent Events."""
    import asyncio

    event_queue, request_id, session_id = await AgentExecutionService.stream(
        agent_id, body, user.id,
        external_call_chain=_parse_call_chain(x_call_chain),
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
    "/{agent_id}/resume",
    summary="Resume an interrupted Agent (SSE stream)",
)
async def resume_agent(
    agent_id: str,
    body: ResumeRequest,
    user: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> StreamingResponse:
    """Resume an agent paused via interrupt (ask_clarification)."""
    import asyncio

    event_queue, request_id, session_id = await AgentExecutionService.resume(
        agent_id, body, user.id,
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

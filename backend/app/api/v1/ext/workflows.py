"""External API — Workflow resource discovery and invocation."""
from fastapi import APIRouter, Depends

from app.api.v1.ext import auth_and_rate_limit
from app.core.auth_apikey import ApiKeyPrincipal
from app.core.errors import NotFoundError, ValidationError
from app.models.workflow import WorkflowStatus
from app.schemas.ext_api import (
    ExtWorkflowDetailResponse,
    ExtWorkflowInvokeRequest,
    ExtWorkflowInvokeResponse,
    ExtWorkflowListResponse,
    ExtWorkflowResponse,
)
from app.services.task_service import TaskService
from app.services.workflow_service import WorkflowService

router = APIRouter(tags=["external-workflows"])


def _doc_to_ext_summary(doc: dict) -> ExtWorkflowResponse:
    """Convert a Workflow document to external summary format."""
    from app.services.workflow_service import _extract_input_schema

    nodes = doc.get("nodes", [])
    return ExtWorkflowResponse(
        id=doc["_id"],
        name=doc["name"],
        description=doc.get("description", ""),
        input_schema=_extract_input_schema(nodes),
        status=doc["status"],
        version=doc.get("version", 1),
    )


def _doc_to_ext_detail(doc: dict) -> ExtWorkflowDetailResponse:
    """Convert a Workflow document to external detail format."""
    from app.services.workflow_service import _extract_input_schema

    nodes = doc.get("nodes", [])
    return ExtWorkflowDetailResponse(
        id=doc["_id"],
        name=doc["name"],
        description=doc.get("description", ""),
        input_schema=_extract_input_schema(nodes),
        status=doc["status"],
        version=doc.get("version", 1),
        nodes=nodes,
        edges=doc.get("edges", []),
        tags=doc.get("tags", []),
    )


# ---------------------------------------------------------------------------
# Resource discovery
# ---------------------------------------------------------------------------


@router.get(
    "/workflows",
    response_model=ExtWorkflowListResponse,
    summary="List accessible Workflows",
)
async def list_workflows(
    page: int = 1,
    page_size: int = 20,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> ExtWorkflowListResponse:
    """List published Workflows accessible to this API Key.

    Results are filtered by the Key's ``bindings.workflows``.
    Empty bindings = all published Workflows.
    """
    principal.require_scope("workflows:read")

    items, total = await WorkflowService.list(
        page=page,
        page_size=page_size,
        status=WorkflowStatus.PUBLISHED,
    )

    # Filter by bindings
    allowed_ids = principal.bindings.get("workflows", [])
    if allowed_ids:
        items = [d for d in items if d["_id"] in allowed_ids]
        if len(items) < page_size:
            total = len(items)

    return ExtWorkflowListResponse(
        items=[_doc_to_ext_summary(d) for d in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/workflows/{workflow_id}",
    response_model=ExtWorkflowDetailResponse,
    summary="Get Workflow details",
)
async def get_workflow(
    workflow_id: str,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> ExtWorkflowDetailResponse:
    """Get Workflow details including nodes and edges.
    Requires ``workflows:read`` scope and binding access.
    """
    principal.require_scope("workflows:read")
    principal.require_workflow_access(workflow_id)

    doc = await WorkflowService.get(workflow_id)
    if doc is None or doc.get("status") != WorkflowStatus.PUBLISHED.value:
        raise NotFoundError(code="WORKFLOW_NOT_FOUND", message="Workflow not found")

    return _doc_to_ext_detail(doc)


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


@router.post(
    "/workflows/{workflow_id}/invoke",
    response_model=ExtWorkflowInvokeResponse,
    status_code=201,
    summary="Invoke Workflow (async)",
)
async def invoke_workflow(
    workflow_id: str,
    body: ExtWorkflowInvokeRequest,
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> ExtWorkflowInvokeResponse:
    """Trigger a Workflow execution asynchronously.

    Returns a ``task_id`` that can be polled via ``GET /ext/tasks/{task_id}``.
    If ``callback_url`` is provided, a webhook will be sent on completion.
    """
    principal.require_scope("workflows:invoke")
    principal.require_workflow_access(workflow_id)

    # Verify workflow exists and is published
    doc = await WorkflowService.get(workflow_id)
    if doc is None or doc.get("status") != WorkflowStatus.PUBLISHED.value:
        raise NotFoundError(code="WORKFLOW_NOT_FOUND", message="Workflow not found")

    # Validate input against schema
    from app.services.workflow_service import _extract_input_schema

    input_schema = _extract_input_schema(doc.get("nodes", []))
    if input_schema:
        _validate_input(body.input, input_schema)

    # Build ext metadata for webhook scoping and callback
    ext_metadata: dict = {"ext_api_key_id": principal.key_id}
    if body.callback_url:
        ext_metadata["ext_callback_url"] = body.callback_url

    # Create task.
    # NOTE: Workflow path keeps owner_user_id as created_by for now —
    # ExtWorkflowInvokeRequest has no visitor_id/user_token field. End-user
    # isolation for workflows (using principal.user_id from callback-verification
    # mode) is deferred to Story P3 (call log + token stats).
    task_doc = await TaskService.create_task(
        workflow_id=workflow_id,
        input_data=body.input,
        created_by=principal.owner_user_id,
        created_by_type="api_key",
        ext_metadata=ext_metadata,
    )

    return ExtWorkflowInvokeResponse(
        task_id=task_doc["_id"],
        status=task_doc.get("status", "pending"),
        workflow_id=workflow_id,
        workflow_version=doc.get("version", 1),
    )


def _validate_input(input_data: dict, input_schema: dict) -> None:
    """Validate input data against the workflow's input schema.

    Raises ValidationError if validation fails.
    """
    required = input_schema.get("required", [])
    properties = input_schema.get("properties", {})

    missing = [k for k in required if k not in input_data]
    if missing:
        raise ValidationError(
            code="INPUT_VALIDATION_FAILED",
            message=f"Input validation failed: missing required fields: {missing}",
        )

    # Type check for provided fields
    for key, value in input_data.items():
        if key in properties:
            expected_type = properties[key].get("type")
            if expected_type and not _check_type(value, expected_type):
                raise ValidationError(
                    code="INPUT_VALIDATION_FAILED",
                    message=f"Input validation failed: field '{key}' expected type '{expected_type}'",
                )


def _check_type(value: object, expected_type: str) -> bool:
    """Check if a value matches the expected JSON schema type."""
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    expected = type_map.get(expected_type)
    if expected is None:
        return True  # unknown type, skip check
    return isinstance(value, expected)

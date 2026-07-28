"""Admin API endpoints — user management CRUD (admin-only, requires JWT + admin role)."""
from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user, require_role
from app.models.user import UserRole, UserStatus
from app.schemas.user import (
    PasswordResetRequest,
    PasswordResetResponse,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
)
from app.services.user_service import UserService

router = APIRouter(
    tags=["users"],
    dependencies=[Depends(get_current_user)],
)


def _doc_to_user_response(doc: dict) -> UserResponse:
    """Convert a MongoDB user document to UserResponse (without permissions)."""
    return UserResponse(
        id=doc["_id"],
        username=doc["username"],
        email=doc["email"],
        role=doc["role"],
        status=doc["status"],
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        last_login_at=doc.get("last_login_at"),
    )


@router.get(
    "/users",
    response_model=UserListResponse,
    summary="List all users (admin)",
    responses={
        403: {"description": "Forbidden — admin role required"},
    },
)
async def list_users(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=200, description="Items per page"),
    username: str | None = Query(None, description="Filter by username (substring)"),
    role: str | None = Query(None, description="Filter by role name"),
    status: UserStatus | None = Query(None, description="Filter by status"),
    _: UserResponse = Depends(require_role(UserRole.ADMIN)),
) -> UserListResponse:
    """List all users with pagination and optional filtering. (AC1)"""
    items, total = await UserService.list_users(
        page=page,
        page_size=page_size,
        username=username,
        role=role,
        status=status.value if status else None,
    )

    users = [_doc_to_user_response(doc) for doc in items]
    return UserListResponse(items=users, total=total, page=page, page_size=page_size)


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=201,
    summary="Create a new user (admin)",
    responses={
        403: {"description": "Forbidden — admin role required"},
        404: {"description": "Role not found"},
        409: {"description": "Username or email conflict"},
        422: {"description": "Validation error"},
    },
)
async def create_user(
    body: UserCreate,
    _: UserResponse = Depends(require_role(UserRole.ADMIN)),
) -> UserResponse:
    """Create a new user with specified role. (AC2)"""
    doc = await UserService.create_user_by_admin(
        username=body.username,
        email=body.email,
        password=body.password,
        role=body.role,
    )
    return _doc_to_user_response(doc)


@router.patch(
    "/users/{user_id}",
    response_model=UserResponse,
    summary="Update a user (admin)",
    responses={
        403: {"description": "Forbidden — admin role required"},
        404: {"description": "User not found"},
        422: {"description": "Validation error"},
    },
)
async def update_user(
    user_id: str,
    body: UserUpdate,
    current_user: UserResponse = Depends(require_role(UserRole.ADMIN)),
) -> UserResponse:
    """Partially update a user's role and/or status. (AC3)"""
    updates: dict = {}
    if body.role is not None:
        updates["role"] = body.role
    if body.status is not None:
        updates["status"] = body.status.value if isinstance(body.status, UserStatus) else body.status

    doc = await UserService.update_user(
        user_id=user_id,
        updates=updates,
        current_user_id=current_user.id,
    )
    if doc is None:
        from app.core.errors import NotFoundError

        raise NotFoundError(
            code="USER_NOT_FOUND",
            message=f"用户 {user_id} 不存在",
        )

    return _doc_to_user_response(doc)


@router.delete(
    "/users/{user_id}",
    status_code=204,
    summary="Delete a user (admin)",
    responses={
        403: {"description": "Forbidden — admin role required"},
        404: {"description": "User not found"},
        422: {"description": "Cannot delete self or last admin"},
    },
)
async def delete_user(
    user_id: str,
    current_user: UserResponse = Depends(require_role(UserRole.ADMIN)),
) -> None:
    """Delete a user by ID. (AC4)"""
    deleted = await UserService.delete_user(
        user_id=user_id,
        current_user_id=current_user.id,
    )
    if not deleted:
        from app.core.errors import NotFoundError

        raise NotFoundError(
            code="USER_NOT_FOUND",
            message=f"用户 {user_id} 不存在",
        )


@router.post(
    "/users/{user_id}/reset-password",
    response_model=PasswordResetResponse,
    summary="Reset a user's password (admin)",
    responses={
        403: {"description": "Forbidden — admin role required"},
        404: {"description": "User not found"},
        422: {"description": "Weak password"},
    },
)
async def reset_password(
    user_id: str,
    body: PasswordResetRequest,
    _: UserResponse = Depends(require_role(UserRole.ADMIN)),
) -> PasswordResetResponse:
    """Reset a user's password. (AC5)"""
    ok = await UserService.reset_password(
        user_id=user_id,
        new_password=body.new_password,
    )
    if not ok:
        from app.core.errors import NotFoundError

        raise NotFoundError(
            code="USER_NOT_FOUND",
            message=f"用户 {user_id} 不存在",
        )

    return PasswordResetResponse(message="密码已重置")


# ---------------------------------------------------------------------------
# Execution statistics — cross-channel agent-execution overview (admin)
# ---------------------------------------------------------------------------

@router.get(
    "/execution-stats",
    summary="Execution stats grouped by access channel (admin)",
    responses={403: {"description": "Forbidden — admin role required"}},
)
async def get_execution_stats(
    start: str | None = Query(None, description="ISO datetime (inclusive lower bound)"),
    end: str | None = Query(None, description="ISO datetime (exclusive upper bound)"),
    date: str | None = Query(None, description="ISO date for a single day (overrides start/end)"),
    _: UserResponse = Depends(require_role(UserRole.ADMIN)),
) -> dict:
    """Aggregate agent-execution stats across three access channels:
    ``internal`` (platform users), ``api_key`` (third-party widget/ext),
    and ``im`` (IM channels).

    Use ``date`` for a single-day view, or ``start``/``end`` for a range.
    Omit all three for all-time totals.
    """
    from app.services.execution_stats_service import ExecutionStatsService

    return await ExecutionStatsService.get_stats(start=start, end=end, date=date)


@router.get(
    "/execution-logs",
    summary="Execution log detail (admin)",
    responses={403: {"description": "Forbidden — admin role required"}},
)
async def list_execution_logs(
    source: str | None = Query(None, description="Filter by channel: internal | api_key | im"),
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    session_id: str | None = Query(None, description="Filter by session ID"),
    start: str | None = Query(None, description="ISO datetime (inclusive lower bound)"),
    end: str | None = Query(None, description="ISO datetime (exclusive upper bound)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=200, description="Items per page"),
    _: UserResponse = Depends(require_role(UserRole.ADMIN)),
) -> dict:
    """Paginated execution-log detail across all channels (admin).

    Each record is one agent invocation (invoke/stream/resume), independent
    of session lifecycle — deleting a session does not remove its log here.
    """
    from app.services.execution_log_service import ExecutionLogService

    items, total = await ExecutionLogService.list_logs(
        source=source,
        agent_id=agent_id,
        session_id=session_id,
        start=start,
        end=end,
        page=page,
        page_size=page_size,
    )
    return {"items": items, "total": total, "page": page, "page_size": page_size}

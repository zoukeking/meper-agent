"""File management API endpoints — upload, download, list, delete."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.core.errors import NotFoundError
from app.core.security import get_current_user, require_any_role
from app.models.file_library import FileConsumerKind, FileRef
from app.schemas.file_library import (
    FileRefListResponse,
    FileRefResponse,
    FileUsageResponse,
)
from app.schemas.user import UserResponse
from app.services.file_service import FileService
from app.services.file_storage import LocalFileStorage
from app.utils.http import build_content_disposition

router = APIRouter(
    prefix="/files",
    tags=["files"],
    dependencies=[Depends(get_current_user)],
)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


def get_file_service() -> FileService:
    """FileService 依赖注入。"""
    storage = LocalFileStorage()
    return FileService(storage=storage)


async def _get_owner_file(
    svc: FileService, file_id: str, owner_user_id: str
) -> FileRef:
    """获取文件并验证所有权，不存在或非 owner 返回 404。

    Story 4-15: 如果文件的 owner_user_id 是 "agent"（chat agent 创建），
    允许任何已认证用户访问（这些文件是 task 的公共产物）。
    """
    file_ref = await svc.get(file_id)
    if file_ref is None:
        raise NotFoundError(
            code="FILE_NOT_FOUND", message=f"文件 {file_id} 不存在"
        )
    # agent 创建的文件允许任何用户访问
    if file_ref.owner_user_id != "agent" and file_ref.owner_user_id != owner_user_id:
        raise NotFoundError(
            code="FILE_NOT_FOUND", message=f"文件 {file_id} 不存在"
        )
    return file_ref


# ------------------------------------------------------------------
# POST /files — 上传文件
# ------------------------------------------------------------------


@router.post(
    "",
    response_model=FileRefResponse,
    status_code=201,
    summary="Upload a file",
    responses={413: {"description": "File too large"}},
)
async def upload_file(
    file: UploadFile = File(...),
    origin_kind: str = Query("user_library", description="Origin consumer kind"),
    origin_id: str | None = Query(None, description="Origin consumer ID"),
    svc: FileService = Depends(get_file_service),
    current_user: UserResponse = Depends(
        require_any_role("admin", "developer", "operator", "viewer")
    ),
) -> FileRefResponse:
    """上传文件到用户文件库。

    接受 multipart/form-data，文件大小不超过 50MB。
    自动创建 USER_LIBRARY 类型的 FileUsage 记录。
    """
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件大小超过限制（最大 {MAX_FILE_SIZE // 1024 // 1024}MB）",
        )

    mime_type = file.content_type or "application/octet-stream"
    filename = file.filename or "unnamed"

    # 解析 origin_kind
    try:
        consumer_kind = FileConsumerKind(origin_kind)
    except ValueError:
        consumer_kind = FileConsumerKind.USER_LIBRARY

    # origin_id 默认使用当前用户 ID
    effective_origin_id = origin_id or current_user.id

    file_ref = await svc.create(
        data=data,
        filename=filename,
        mime_type=mime_type,
        owner_user_id=current_user.id,
        origin_kind=consumer_kind,
        origin_id=effective_origin_id,
    )

    # 自动创建 USER_LIBRARY 使用记录
    await svc.add_usage(
        file_id=file_ref.id,
        consumer_kind=FileConsumerKind.USER_LIBRARY,
        consumer_id=current_user.id,
    )

    return FileRefResponse(**file_ref.model_dump(by_alias=True))


# ------------------------------------------------------------------
# GET /files — 文件列表
# ------------------------------------------------------------------


@router.get(
    "",
    response_model=FileRefListResponse,
    summary="List user files",
)
async def list_files(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=200, description="Items per page"),
    status: str = Query("active", description="Filter by status (active/trashed)"),
    svc: FileService = Depends(get_file_service),
    current_user: UserResponse = Depends(get_current_user),
) -> FileRefListResponse:
    """查询当前用户的文件列表（分页）。"""
    files, total = await svc.list_by_owner(
        owner_user_id=current_user.id,
        page=page,
        page_size=page_size,
        status=status,
    )
    items = [FileRefResponse(**f.model_dump(by_alias=True)) for f in files]
    return FileRefListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


# ------------------------------------------------------------------
# GET /files/{file_id} — 文件详情
# ------------------------------------------------------------------


@router.get(
    "/{file_id}",
    response_model=FileRefResponse,
    summary="Get file details",
    responses={404: {"description": "File not found"}},
)
async def get_file(
    file_id: str,
    svc: FileService = Depends(get_file_service),
    current_user: UserResponse = Depends(get_current_user),
) -> FileRefResponse:
    """获取文件详情。"""
    file_ref = await _get_owner_file(svc, file_id, current_user.id)
    return FileRefResponse(**file_ref.model_dump(by_alias=True))


# ------------------------------------------------------------------
# GET /files/{file_id}/download — 文件下载
# ------------------------------------------------------------------


@router.get(
    "/{file_id}/download",
    summary="Download file content",
    responses={404: {"description": "File not found"}},
)
async def download_file(
    file_id: str,
    svc: FileService = Depends(get_file_service),
    current_user: UserResponse = Depends(get_current_user),
) -> Response:
    """下载文件内容。"""
    file_ref = await _get_owner_file(svc, file_id, current_user.id)

    try:
        data = await svc._storage.load(file_ref.storage_key)
    except FileNotFoundError:
        raise NotFoundError(
            code="FILE_CONTENT_NOT_FOUND", message="文件内容不存在"
        ) from None

    return Response(
        content=data,
        media_type=file_ref.mime_type,
        headers={"Content-Disposition": build_content_disposition(file_ref.name)},
    )


# ------------------------------------------------------------------
# DELETE /files/{file_id} — 文件删除
# ------------------------------------------------------------------


@router.delete(
    "/{file_id}",
    summary="Delete a file",
    response_model=None,
    responses={
        204: {"description": "File deleted"},
        404: {"description": "File not found"},
    },
)
async def delete_file(
    file_id: str,
    force: bool = Query(False, description="Force delete (remove physical file + usages)"),
    svc: FileService = Depends(get_file_service),
    current_user: UserResponse = Depends(get_current_user),
) -> Response | FileRefResponse:
    """删除文件。

    - 默认（force=false）：软删除，将状态标记为 trashed
    - force=true：硬删除，物理删除文件 + 级联删除所有 usage
    """
    await _get_owner_file(svc, file_id, current_user.id)

    if force:
        # 硬删除（级联删除 usage + 物理文件 + DB）
        deleted = await svc.delete(file_id, force=True)
        if not deleted:
            raise NotFoundError(
                code="FILE_NOT_FOUND", message=f"文件 {file_id} 不存在"
            )
        return Response(status_code=204)
    else:
        # 软删除
        deleted = await svc.delete(file_id, force=False)
        if not deleted:
            raise NotFoundError(
                code="FILE_NOT_FOUND", message=f"文件 {file_id} 不存在"
            )
        updated_ref = await svc.get(file_id)
        return FileRefResponse(**updated_ref.model_dump(by_alias=True))


# ------------------------------------------------------------------
# POST /files/{file_id}/restore — 从回收站恢复
# ------------------------------------------------------------------


@router.post(
    "/{file_id}/restore",
    response_model=FileRefResponse,
    summary="Restore file from trash",
    responses={404: {"description": "File not found"}},
)
async def restore_file(
    file_id: str,
    svc: FileService = Depends(get_file_service),
    current_user: UserResponse = Depends(get_current_user),
) -> FileRefResponse:
    """从回收站恢复文件（status: trashed → active）。"""
    file_ref = await _get_owner_file(svc, file_id, current_user.id)
    if file_ref.status != "trashed":
        # 已经是 active，直接返回
        return FileRefResponse(**file_ref.model_dump(by_alias=True))

    updated = await svc.update_status(file_id, "active")
    if not updated:
        raise NotFoundError(
            code="FILE_NOT_FOUND", message=f"文件 {file_id} 不存在"
        )
    updated_ref = await svc.get(file_id)
    return FileRefResponse(**updated_ref.model_dump(by_alias=True))


# ------------------------------------------------------------------
# POST /files/trash/empty — 清空回收站
# ------------------------------------------------------------------


@router.post(
    "/trash/empty",
    summary="Empty trash",
    responses={200: {"description": "Trash emptied"}},
)
async def empty_trash(
    svc: FileService = Depends(get_file_service),
    current_user: UserResponse = Depends(
        require_any_role("admin", "developer", "operator", "viewer")
    ),
) -> dict:
    """清空当前用户的回收站。

    硬删除所有 status=trashed 且 usage_count=0 的文件。
    """
    # 查询当前用户所有 trashed 文件
    trashed_files, _ = await svc.list_by_owner(
        owner_user_id=current_user.id,
        page=1,
        page_size=10000,
        status="trashed",
    )

    deleted_count = 0
    for file_ref in trashed_files:
        has_refs = await svc.has_usages(file_ref.id)
        if not has_refs:
            result = await svc.delete(file_ref.id, force=True)
            if result:
                deleted_count += 1

    return {"deleted_count": deleted_count}


# ------------------------------------------------------------------
# POST /files/cleanup — 过期 Usage 清理
# ------------------------------------------------------------------


@router.post(
    "/cleanup",
    summary="Cleanup expired usages",
    responses={200: {"description": "Expired usages cleaned up"}},
)
async def cleanup_expired_usages(
    svc: FileService = Depends(get_file_service),
    current_user: UserResponse = Depends(require_any_role("admin")),
) -> dict:
    """清理过期的 FileUsage 记录（管理员端）。

    删除所有 expires_at < now 的 usage 记录，
    清理后若文件无引用且 status=trashed 则一并硬删除。
    """
    deleted_count = await svc.cleanup_expired_usages()
    return {"deleted_count": deleted_count}


# ------------------------------------------------------------------
# GET /files/{file_id}/usages — 使用记录查询
# ------------------------------------------------------------------


@router.get(
    "/{file_id}/usages",
    response_model=list[FileUsageResponse],
    summary="List file usages",
    responses={404: {"description": "File not found"}},
)
async def list_file_usages(
    file_id: str,
    svc: FileService = Depends(get_file_service),
    current_user: UserResponse = Depends(get_current_user),
) -> list[FileUsageResponse]:
    """查询文件的所有使用记录。"""
    await _get_owner_file(svc, file_id, current_user.id)
    usages = await svc.list_usages(file_id)
    return [FileUsageResponse(**u.model_dump(by_alias=True)) for u in usages]

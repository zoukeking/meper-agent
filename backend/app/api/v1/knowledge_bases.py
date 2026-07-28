"""KnowledgeBase API endpoints — Markdown KB CRUD + .md file management."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from loguru import logger

from app.core.security import get_current_user, require_permission
from app.schemas.knowledge_base import (
    KbFileResponse,
    KbFileTreeNode,
    KbFileTreeResponse,
    KbFileUpdate,
    KbUploadErrorItem,
    KbUploadResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseListResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
)
from app.schemas.user import UserResponse
from app.services.kb_service import KnowledgeBaseService

router = APIRouter(
    prefix="/knowledge-bases",
    tags=["knowledge-bases"],
    dependencies=[Depends(get_current_user)],
)


def _doc_to_response(doc: dict) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=doc["_id"],
        name=doc.get("name", ""),
        description=doc.get("description", ""),
        owner_user_id=doc.get("owner_user_id", ""),
        status=doc.get("status", "active"),
        file_count=doc.get("file_count", 0),
        total_size=doc.get("total_size", 0),
        created_at=doc.get("created_at", ""),
        updated_at=doc.get("updated_at", ""),
    )


def _dict_to_node(d: dict) -> KbFileTreeNode:
    children = d.get("children")
    return KbFileTreeNode(
        key=d["key"],
        title=d["title"],
        is_leaf=d.get("is_leaf", True),
        children=[_dict_to_node(c) for c in children] if children else None,
        size=d.get("size", 0),
    )


@router.post(
    "",
    response_model=KnowledgeBaseResponse,
    status_code=201,
    summary="Create a knowledge base",
    responses={403: {"description": "Forbidden — knowledge:write required"}},
)
async def create_kb(
    body: KnowledgeBaseCreate,
    user: UserResponse = Depends(require_permission("knowledge:write")),
) -> KnowledgeBaseResponse:
    """Create a new (empty) Markdown knowledge base."""
    doc = await KnowledgeBaseService.create_kb(
        name=body.name,
        description=body.description,
        owner_user_id=user.id,
    )
    return _doc_to_response(doc)


@router.get(
    "",
    response_model=KnowledgeBaseListResponse,
    summary="List knowledge bases",
    responses={403: {"description": "Forbidden — knowledge:read required"}},
)
async def list_kbs(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=200, description="Items per page"),
    name: str | None = Query(None, description="Filter by name (substring)"),
    status: str | None = Query(None, description="Filter by status"),
    _: UserResponse = Depends(require_permission("knowledge:read")),
) -> KnowledgeBaseListResponse:
    items, total = await KnowledgeBaseService.list_kbs(
        page=page, page_size=page_size, name=name, status=status
    )
    return KnowledgeBaseListResponse(
        items=[_doc_to_response(d) for d in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{kb_id}",
    response_model=KnowledgeBaseResponse,
    summary="Get knowledge base details",
    responses={
        403: {"description": "Forbidden — knowledge:read required"},
        404: {"description": "Knowledge base not found"},
    },
)
async def get_kb(
    kb_id: str,
    _: UserResponse = Depends(require_permission("knowledge:read")),
) -> KnowledgeBaseResponse:
    from app.core.errors import NotFoundError

    doc = await KnowledgeBaseService.get_kb(kb_id)
    if doc is None:
        raise NotFoundError(code="KB_NOT_FOUND", message=f"知识库 {kb_id} 不存在")
    return _doc_to_response(doc)


@router.put(
    "/{kb_id}",
    response_model=KnowledgeBaseResponse,
    summary="Update a knowledge base",
    responses={
        403: {"description": "Forbidden — knowledge:write required"},
        404: {"description": "Knowledge base not found"},
    },
)
async def update_kb(
    kb_id: str,
    body: KnowledgeBaseUpdate,
    _: UserResponse = Depends(require_permission("knowledge:write")),
) -> KnowledgeBaseResponse:
    from app.core.errors import NotFoundError

    doc = await KnowledgeBaseService.update_kb(
        kb_id, name=body.name, description=body.description
    )
    if doc is None:
        raise NotFoundError(code="KB_NOT_FOUND", message=f"知识库 {kb_id} 不存在")
    return _doc_to_response(doc)


@router.delete(
    "/{kb_id}",
    status_code=204,
    summary="Delete a knowledge base",
    responses={
        403: {"description": "Forbidden — knowledge:write required"},
        404: {"description": "Knowledge base not found"},
        409: {"description": "Knowledge base is referenced by one or more Agents"},
    },
)
async def delete_kb(
    kb_id: str,
    _: UserResponse = Depends(require_permission("knowledge:write")),
) -> None:
    from app.core.errors import NotFoundError

    deleted = await KnowledgeBaseService.delete_kb(kb_id)
    if not deleted:
        raise NotFoundError(code="KB_NOT_FOUND", message=f"知识库 {kb_id} 不存在")


@router.get(
    "/{kb_id}/files",
    response_model=KbFileTreeResponse,
    summary="Get knowledge base file tree",
    responses={
        403: {"description": "Forbidden — knowledge:read required"},
        404: {"description": "Knowledge base not found"},
    },
)
async def get_kb_files(
    kb_id: str,
    _: UserResponse = Depends(require_permission("knowledge:read")),
) -> KbFileTreeResponse:
    from app.core.errors import NotFoundError

    tree = await KnowledgeBaseService.get_kb_files(kb_id)
    if tree is None:
        raise NotFoundError(code="KB_NOT_FOUND", message=f"知识库 {kb_id} 不存在")
    return KbFileTreeResponse(kb_id=kb_id, files=[_dict_to_node(d) for d in tree])


@router.get(
    "/{kb_id}/files/{file_path:path}",
    response_model=KbFileResponse,
    summary="Get a knowledge base file's content",
    responses={
        403: {"description": "Forbidden — knowledge:read required"},
        404: {"description": "Knowledge base or file not found"},
    },
)
async def get_kb_file_content(
    kb_id: str,
    file_path: str,
    _: UserResponse = Depends(require_permission("knowledge:read")),
) -> KbFileResponse:
    from app.core.errors import NotFoundError

    data = await KnowledgeBaseService.get_kb_file_content(kb_id, file_path)
    if data is None:
        raise NotFoundError(
            code="FILE_NOT_FOUND",
            message=f"文件 {file_path} 在知识库 {kb_id} 中不存在",
        )
    return KbFileResponse(
        path=data["path"], content=data["content"], size=data.get("size", 0)
    )


@router.put(
    "/{kb_id}/files/{file_path:path}",
    response_model=KbFileResponse,
    summary="Update a knowledge base file's content",
    responses={
        403: {"description": "Forbidden — knowledge:write required"},
        404: {"description": "Knowledge base or file not found"},
    },
)
async def update_kb_file(
    kb_id: str,
    file_path: str,
    body: KbFileUpdate,
    _: UserResponse = Depends(require_permission("knowledge:write")),
) -> KbFileResponse:
    from app.core.errors import NotFoundError

    data = await KnowledgeBaseService.update_kb_file(kb_id, file_path, body.content)
    if data is None:
        raise NotFoundError(
            code="FILE_NOT_FOUND",
            message=f"文件 {file_path} 在知识库 {kb_id} 中不存在",
        )
    return KbFileResponse(
        path=data["path"], content=data["content"], size=data.get("size", 0)
    )


@router.delete(
    "/{kb_id}/files/{file_path:path}",
    status_code=204,
    summary="Delete a file from a knowledge base",
    responses={
        403: {"description": "Forbidden — knowledge:write required"},
        404: {"description": "Knowledge base or file not found"},
    },
)
async def delete_kb_file(
    kb_id: str,
    file_path: str,
    _: UserResponse = Depends(require_permission("knowledge:write")),
) -> None:
    from app.core.errors import NotFoundError

    ok = await KnowledgeBaseService.delete_kb_file(kb_id, file_path)
    if not ok:
        # Distinguish KB-missing from file-missing.
        if await KnowledgeBaseService.get_kb(kb_id) is None:
            raise NotFoundError(code="KB_NOT_FOUND", message=f"知识库 {kb_id} 不存在")
        raise NotFoundError(
            code="FILE_NOT_FOUND",
            message=f"文件 {file_path} 在知识库 {kb_id} 中不存在",
        )


@router.post(
    "/{kb_id}/documents",
    response_model=KbUploadResponse,
    summary="Upload .md file(s) into a knowledge base",
    responses={
        403: {"description": "Forbidden — knowledge:write required"},
        404: {"description": "Knowledge base not found"},
    },
)
async def upload_documents(
    kb_id: str,
    files: list[UploadFile] = File(
        ..., description="Markdown 文件（支持多文件/文件夹上传，保留相对路径）"
    ),
    _: UserResponse = Depends(require_permission("knowledge:write")),
) -> KbUploadResponse:
    """Upload one or more ``.md`` files into the KB directory.

    Folder upload is supported: the browser sends relative paths in
    ``filename`` (e.g. ``notes/api.md``), which are preserved on disk.
    """
    payload: list[tuple[str, bytes]] = []
    for f in files:
        rel = (f.filename or "").replace("\\", "/").lstrip("/")
        if not rel:
            continue
        raw = await f.read()
        payload.append((rel, raw))

    result = await KnowledgeBaseService.upload_files(kb_id, payload)
    return KbUploadResponse(
        created=result["created"],
        errors=[KbUploadErrorItem(**e) for e in result["errors"]],
    )

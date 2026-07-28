"""External API — session file upload/download (API Key authenticated).

Mirrors ``/v1/sessions/*/files`` but authenticates via API Key (plus an
optional ``X-User-Token`` in callback-verification mode). Ownership is
resolved through :func:`resolve_user_id`, so legacy (``visitor_id``) and
callback (``X-User-Token`` sub) modes attribute files identically —
matching how ``/v1/ext/agents/*/invoke`` attributes sessions.

File-handling helpers (size limit, extension whitelist, filename
sanitization, path-traversal defense) are imported verbatim from the
internal sessions module to keep both surfaces in lockstep.
"""
import io
import mimetypes
import zipfile
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.api.v1.ext import auth_and_rate_limit, resolve_user_id
from app.api.v1.sessions import (
    ALLOWED_UPLOAD_EXTENSIONS,
    MAX_FILE_SIZE,
    _get_file_service,
    _msg_to_response,
    _resolve_input_filename,
    _sanitize_upload_filename,
)
from app.core.auth_apikey import ApiKeyPrincipal
from app.core.errors import NotFoundError
from app.engine.tool.workspace import Workspace, WorkspaceManager
from app.models.file_library import FileConsumerKind
from app.schemas.file_library import FileRefResponse
from app.schemas.session import ChatFileUploadResponse
from app.services.file_service import FileService
from app.services.session_service import MessageService, SessionService
from app.utils.http import build_content_disposition
from app.utils.sanitize import sanitize_text

router = APIRouter(tags=["external-session-files"])


async def _verify_ext_session_ownership(
    session_id: str,
    principal: ApiKeyPrincipal,
    visitor_id: str | None,
) -> Workspace:
    """Verify the session belongs to the resolved end-user; return workspace.

    ``user_id`` comes from :func:`resolve_user_id` (legacy:
    ``{owner}:{visitor_id}``; callback: ``{owner}:{sub}``), matching how
    ``/v1/ext/agents/*/invoke`` attributes sessions.
    """
    user_id = resolve_user_id(principal, visitor_id)
    session_doc = await SessionService.get_session(session_id)
    if session_doc is None or session_doc.get("user_id") != user_id:
        raise NotFoundError(code="SESSION_NOT_FOUND", message="会话不存在")
    return WorkspaceManager.get_workspace(user_id, session_id)


@router.post(
    "/sessions/{session_id}/files/upload",
    status_code=201,
    response_model=ChatFileUploadResponse,
    summary="Upload a file to a chat session (external)",
    responses={413: {"description": "File too large"}},
)
async def upload_chat_file(
    session_id: str,
    visitor_id: str | None = Query(
        None, description="访客 ID（兼容模式必填，回调验证模式忽略）"
    ),
    file: UploadFile = File(...),
    content: str = Form(""),
    svc: FileService = Depends(_get_file_service),
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> ChatFileUploadResponse:
    """Upload a file to a chat session via the external API.

    Creates a FileRef owned by the resolved end-user, copies the file to
    the workspace ``input/`` dir, and optionally attaches it to a user
    message when ``content`` is provided.
    """
    principal.require_scope("agents:invoke")

    ws = await _verify_ext_session_ownership(session_id, principal, visitor_id)
    user_id = resolve_user_id(principal, visitor_id)

    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件大小超过限制（最大 {MAX_FILE_SIZE // 1024 // 1024}MB）",
        )

    mime_type = file.content_type or "application/octet-stream"
    filename = _sanitize_upload_filename(file.filename or "unnamed")

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"不支持的文件类型 {ext or '(无扩展名)'}。"
                "仅允许图片/文档/常见代码与文本文件。"
            ),
        )

    # SVG 是常见的 XSS 载荷载体（内嵌 <script>/onload），入库前定向清洗。
    if ext == ".svg":
        with suppress(Exception):
            data = sanitize_text(data.decode("utf-8", errors="replace")).encode(
                "utf-8"
            )

    file_ref = await svc.create(
        data=data,
        filename=filename,
        mime_type=mime_type,
        owner_user_id=user_id,
        origin_kind=FileConsumerKind.SESSION_MESSAGE,
        origin_id=session_id,
    )

    await svc.add_usage(
        file_id=file_ref.id,
        consumer_kind=FileConsumerKind.SESSION_MESSAGE,
        consumer_id=session_id,
    )

    input_path = _resolve_input_filename(ws.input_dir, filename)
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(data)

    message_response = None
    if content:
        msg_doc = await MessageService.add_message(
            session_id=session_id,
            role="user",
            content=content,
            file_ids=[file_ref.id],
        )
        message_response = _msg_to_response(msg_doc)

    return ChatFileUploadResponse(
        file=FileRefResponse(**file_ref.model_dump(by_alias=True)),
        message=message_response,
        workspace_path=str(input_path.relative_to(ws.input_dir)),
    )


@router.get(
    "/sessions/{session_id}/files",
    summary="List output files for a session (external)",
)
async def list_session_files(
    session_id: str,
    visitor_id: str | None = Query(
        None, description="访客 ID（兼容模式必填，回调验证模式忽略）"
    ),
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> list[dict]:
    """List all files in the session's output/ directory."""
    principal.require_scope("agents:invoke")
    ws = await _verify_ext_session_ownership(session_id, principal, visitor_id)
    return WorkspaceManager.list_output_files(ws)


@router.get(
    "/sessions/{session_id}/files.zip",
    summary="Download all output files as ZIP (external)",
)
async def download_session_files_zip(
    session_id: str,
    visitor_id: str | None = Query(
        None, description="访客 ID（兼容模式必填，回调验证模式忽略）"
    ),
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> StreamingResponse:
    """Download all files in the session's output/ as a ZIP archive."""
    principal.require_scope("agents:invoke")
    ws = await _verify_ext_session_ownership(session_id, principal, visitor_id)

    files = WorkspaceManager.list_output_files(ws)
    if not files:
        raise NotFoundError(
            code="NO_FILES",
            message="Session has no output files",
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in files:
            file_abs = ws.output_dir / entry["path"]
            if file_abs.is_file():
                zf.write(file_abs, entry["path"])

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="session-{session_id}-output.zip"',
        },
    )


@router.get(
    "/sessions/{session_id}/files/{file_path:path}",
    summary="Download a single output file (external)",
)
async def download_session_file(
    session_id: str,
    file_path: str,
    visitor_id: str | None = Query(
        None, description="访客 ID（兼容模式必填，回调验证模式忽略）"
    ),
    principal: ApiKeyPrincipal = Depends(auth_and_rate_limit),
) -> StreamingResponse:
    """Download a single file from the session's output/ directory."""
    principal.require_scope("agents:invoke")
    ws = await _verify_ext_session_ownership(session_id, principal, visitor_id)

    resolved = WorkspaceManager.safe_resolve_path(ws.output_dir, file_path)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        raise NotFoundError(
            code="FILE_NOT_FOUND",
            message=f"File '{file_path}' not found in session output",
        )

    content_type, _ = mimetypes.guess_type(str(resolved))
    content_type = content_type or "application/octet-stream"

    return StreamingResponse(
        open(resolved, "rb"),
        media_type=content_type,
        headers={
            "Content-Disposition": build_content_disposition(resolved.name),
            "Content-Length": str(resolved.stat().st_size),
        },
    )

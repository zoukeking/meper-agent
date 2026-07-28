"""Session API endpoints — chat conversation management and file downloads."""
import io
import mimetypes
import os
import zipfile
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.core.errors import NotFoundError
from app.core.security import get_current_user, require_any_role
from app.engine.tool.workspace import Workspace, WorkspaceManager
from app.models.file_library import FileConsumerKind
from app.schemas.file_library import FileRefResponse
from app.schemas.session import (
    ChatFileUploadResponse,
    MessageResponse,
    SessionCreate,
    SessionDetailResponse,
    SessionListResponse,
    SessionResponse,
)
from app.schemas.user import UserResponse
from app.services.file_service import FileService
from app.services.file_storage import LocalFileStorage
from app.services.session_service import MessageService, SessionService
from app.utils.http import build_content_disposition
from app.utils.sanitize import sanitize_text

router = APIRouter(
    prefix="/sessions",
    tags=["sessions"],
    dependencies=[Depends(get_current_user)],
)


def _doc_to_response(doc: dict) -> SessionResponse:
    """Convert a raw MongoDB document to SessionResponse."""
    return SessionResponse(
        _id=doc["_id"],
        user_id=doc["user_id"],
        agent_id=doc["agent_id"],
        title=doc.get("title", ""),
        status=doc.get("status", "active"),
        message_count=doc.get("message_count", 0),
        total_tokens=doc.get("total_tokens", 0),
        created_at=doc.get("created_at", ""),
        updated_at=doc.get("updated_at", ""),
    )


def _msg_to_response(doc: dict) -> MessageResponse:
    """Convert a raw MongoDB document to MessageResponse.

    Fallback: legacy agent messages may have ``content`` but no
    ``timeline_entries`` (pre-v2 schema). Synthesize a text entry so
    the frontend can render them.
    """
    role = doc.get("role", "")
    timeline = doc.get("timeline_entries") or []
    content = doc.get("content", "")
    # Legacy fallback: agent message with content but no timeline
    if not timeline and role == "agent" and content:
        timeline = [{"type": "text", "content": content}]
    return MessageResponse(
        _id=doc["_id"],
        session_id=doc["session_id"],
        role=role,
        content=content,
        timeline_entries=timeline,
        file_ids=doc.get("file_ids", []),
        files=[],  # Populated separately when needed
        token_usage=doc.get("token_usage") or {},
        created_at=doc.get("created_at", ""),
    )


@router.post(
    "",
    status_code=201,
    summary="Create a new session",
)
async def create_session(
    body: SessionCreate,
    user: UserResponse = Depends(get_current_user),
) -> SessionResponse:
    """Create a new chat session for the current user."""
    doc = await SessionService.create_session(
        user_id=user.id,
        agent_id=body.agent_id,
        title=body.title or "",
    )
    return _doc_to_response(doc)


@router.get(
    "",
    summary="List user sessions",
)
async def list_sessions(
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: UserResponse = Depends(get_current_user),
) -> SessionListResponse:
    """List sessions for the current user, optionally filtered by agent."""
    items, total = await SessionService.list_sessions(
        user_id=user.id,
        agent_id=agent_id,
        page=page,
        page_size=page_size,
    )
    return SessionListResponse(
        items=[_doc_to_response(d) for d in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{session_id}",
    summary="Get session detail with messages",
)
async def get_session(
    session_id: str,
    user: UserResponse = Depends(get_current_user),
) -> SessionDetailResponse:
    """Get a session and all its messages."""
    session_doc = await SessionService.get_session(session_id)
    if session_doc is None:
        raise NotFoundError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} 不存在",
        )

    # Verify ownership
    if session_doc["user_id"] != user.id:
        raise NotFoundError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} 不存在",
        )

    messages = await MessageService.list_messages(session_id)
    msg_responses = []
    for m in messages:
        mr = _msg_to_response(m)
        # Populate file details if file_ids present
        file_ids = m.get("file_ids", [])
        if file_ids:
            file_svc = _get_file_service()
            files = []
            for fid in file_ids:
                fref = await file_svc.get(fid)
                if fref:
                    files.append(FileRefResponse(**fref.model_dump(by_alias=True)))
            mr.files = files
        msg_responses.append(mr)
    return SessionDetailResponse(
        session=_doc_to_response(session_doc),
        messages=msg_responses,
    )


@router.delete(
    "/{session_id}",
    status_code=204,
    summary="Delete a session",
)
async def delete_session(
    session_id: str,
    user: UserResponse = Depends(get_current_user),
) -> None:
    """Delete a session and all its messages."""
    session_doc = await SessionService.get_session(session_id)
    if session_doc is None:
        raise NotFoundError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} 不存在",
        )

    if session_doc["user_id"] != user.id:
        raise NotFoundError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} 不存在",
        )

    await SessionService.delete_session(session_id)


# ---------------------------------------------------------------------------
# Chat file upload
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# 会话上传允许的文件扩展名白名单（默认拒绝可执行/危险类型如
# .exe/.sh/.bat/.php/.jar 等）。按用途分组便于阅读。
ALLOWED_UPLOAD_EXTENSIONS: frozenset[str] = frozenset(
    {
        # 图片
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
        # 文档
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".md", ".csv", ".rtf",
        # 代码与文本（供 Agent/工作流解析）
        ".json", ".yaml", ".yml", ".xml", ".html", ".htm",
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
        ".c", ".cpp", ".h", ".sql", ".sh", ".ini", ".toml",
    }
)


def _get_file_service() -> FileService:
    """FileService 依赖注入。"""
    return FileService(storage=LocalFileStorage())


def _sanitize_upload_filename(filename: str) -> str:
    """Return a safe base filename, stripping any path components.

    防御路径穿越：剥离客户端文件名中的目录前缀（``../``、绝对路径
    等），只保留最终文件名部分。空或仅含路径分隔符的输入回退为
    ``unnamed``。
    """
    if not filename:
        return "unnamed"
    # basename 处理 ``../``、反斜杠、绝对路径前缀
    name = os.path.basename(filename.replace("\\", "/"))
    name = Path(name).name  # 兜底：再次取纯文件名
    name = name.strip()
    if not name or name in (".", ".."):
        return "unnamed"
    return name


def _resolve_input_filename(input_dir, filename: str) -> Path:
    """Resolve filename conflicts by adding numeric suffix.

    路径安全：通过 :meth:`WorkspaceManager.safe_resolve_path` 校验目标
    落在 ``input_dir`` 之内，防止路径穿越越界写入。
    """
    target = WorkspaceManager.safe_resolve_path(Path(input_dir), filename)
    if target is None:
        raise HTTPException(
            status_code=400,
            detail="非法的文件名（路径越界）",
        )
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    counter = 1
    while target.exists():
        candidate = Path(input_dir) / f"{stem}_{counter}{suffix}"
        resolved = WorkspaceManager.safe_resolve_path(Path(input_dir), candidate.name)
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail="非法的文件名（路径越界）",
            )
        target = resolved
        counter += 1
    return target


@router.post(
    "/{session_id}/files/upload",
    status_code=201,
    response_model=ChatFileUploadResponse,
    summary="Upload a file to chat",
    responses={413: {"description": "File too large"}},
)
async def upload_chat_file(
    session_id: str,
    file: UploadFile = File(...),
    content: str = Form(""),
    svc: FileService = Depends(_get_file_service),
    user: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> ChatFileUploadResponse:
    """Upload a file to a chat session.

    Creates a FileRef, copies to workspace input/ dir,
    and optionally creates a user message with the file attached.
    """
    # Verify session ownership
    ws = await _verify_session_ownership(session_id, user.id)

    # Read file data
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"文件大小超过限制（最大 {MAX_FILE_SIZE // 1024 // 1024}MB）",
        )

    mime_type = file.content_type or "application/octet-stream"
    # 清洗文件名：剥离路径前缀，防止路径穿越（../ 越界写入）。
    filename = _sanitize_upload_filename(file.filename or "unnamed")

    # 文件类型白名单：拒绝可执行/危险扩展名（.exe/.sh/.bat 等）。
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
        with suppress(Exception):  # 兜底：无法解码则原样保留
            data = sanitize_text(
                data.decode("utf-8", errors="replace")
            ).encode("utf-8")

    # Create FileRef in file library
    file_ref = await svc.create(
        data=data,
        filename=filename,
        mime_type=mime_type,
        owner_user_id=user.id,
        origin_kind=FileConsumerKind.SESSION_MESSAGE,
        origin_id=session_id,
    )

    # Create FileUsage
    await svc.add_usage(
        file_id=file_ref.id,
        consumer_kind=FileConsumerKind.SESSION_MESSAGE,
        consumer_id=session_id,
    )

    # Copy physical file to workspace input/ directory
    input_path = _resolve_input_filename(ws.input_dir, filename)
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(data)

    # Optionally create a user message
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


# ---------------------------------------------------------------------------
# File download endpoints
# ---------------------------------------------------------------------------


async def _verify_session_ownership(session_id: str, user_id: str) -> Workspace:
    """Verify session exists and belongs to user, return workspace."""
    session_doc = await SessionService.get_session(session_id)
    if session_doc is None:
        raise NotFoundError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} 不存在",
        )
    if session_doc["user_id"] != user_id:
        raise NotFoundError(
            code="SESSION_NOT_FOUND",
            message=f"Session {session_id} 不存在",
        )

    return WorkspaceManager.get_workspace(user_id, session_id)


@router.get(
    "/{session_id}/files",
    summary="List output files for a session",
)
async def list_session_files(
    session_id: str,
    user: UserResponse = Depends(get_current_user),
) -> list[dict]:
    """List all files in the session's output/ directory.

    Returns a list of file entries with path, size, and modified timestamp.
    """
    ws = await _verify_session_ownership(session_id, user.id)

    from app.engine.tool.workspace import WorkspaceManager

    return WorkspaceManager.list_output_files(ws)


@router.get(
    "/{session_id}/files.zip",
    summary="Download all output files as ZIP",
)
async def download_session_files_zip(
    session_id: str,
    user: UserResponse = Depends(get_current_user),
) -> StreamingResponse:
    """Download all files in the session's output/ as a ZIP archive."""
    ws = await _verify_session_ownership(session_id, user.id)

    from app.engine.tool.workspace import WorkspaceManager

    files = WorkspaceManager.list_output_files(ws)
    if not files:
        raise NotFoundError(
            code="NO_FILES",
            message="Session has no output files",
        )

    # Build ZIP in memory (sufficient for MVP scale)
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
    "/{session_id}/files/{file_path:path}",
    summary="Download a single output file",
)
async def download_session_file(
    session_id: str,
    file_path: str,
    user: UserResponse = Depends(get_current_user),
) -> StreamingResponse:
    """Download a single file from the session's output/ directory."""
    ws = await _verify_session_ownership(session_id, user.id)


    from app.engine.tool.workspace import WorkspaceManager

    resolved = WorkspaceManager.safe_resolve_path(ws.output_dir, file_path)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        raise NotFoundError(
            code="FILE_NOT_FOUND",
            message=f"File '{file_path}' not found in session output",
        )

    # Determine content type
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


@router.delete(
    "/{session_id}/files/{file_path:path}",
    summary="Delete a single output file",
)
async def delete_session_file(
    session_id: str,
    file_path: str,
    user: UserResponse = Depends(get_current_user),
) -> list[dict]:
    """Delete a single file from the session's output/ directory."""
    ws = await _verify_session_ownership(session_id, user.id)

    from app.engine.tool.workspace import WorkspaceManager

    resolved = WorkspaceManager.safe_resolve_path(ws.output_dir, file_path)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        raise NotFoundError(
            code="FILE_NOT_FOUND",
            message=f"File '{file_path}' not found in session output",
        )

    resolved.unlink()
    return WorkspaceManager.list_output_files(ws)

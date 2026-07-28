"""Tool API endpoints — Markdown Skill upload + custom tool CRUD for the unified tool pool."""
from __future__ import annotations

import contextlib
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from loguru import logger
from pydantic import BaseModel, Field

from app.core.security import get_current_user, require_any_role
from app.engine.tool.skill_fs import list_skill_files
from app.schemas.tool import (
    BuiltinToolResponse,
    SkillFileResponse,
    SkillFileTreeNode,
    SkillFileTreeResponse,
    SkillFileUpdate,
    ToolListResponse,
    ToolResponse,
    ToolUpdate,
    ToolUploadErrorItem,
    ToolUploadResponse,
)
from app.schemas.user import UserResponse
from app.services.tool_service import MAX_DIRECTORY_SIZE, MAX_FILE_SIZE, ToolService

router = APIRouter(
    prefix="/tools",
    tags=["tools"],
    dependencies=[Depends(get_current_user)],
)


def _basetool_to_response(tool: Any, *, configurable: bool = True) -> BuiltinToolResponse:
    """Serialize a LangChain BaseTool into a BuiltinToolResponse.

    Shared by the builtin and app endpoints so both return the same shape.
    """
    params: dict[str, Any] = {}
    if hasattr(tool, "args_schema") and tool.args_schema:
        with contextlib.suppress(Exception):
            params = tool.args_schema.model_json_schema()
    return BuiltinToolResponse(
        name=tool.name,
        description=tool.description or "",
        parameters=params,
        configurable=configurable,
    )


@router.get(
    "/builtin",
    response_model=list[BuiltinToolResponse],
    summary="List built-in tools",
    responses={403: {"description": "Forbidden — viewer+ role required"}},
)
async def list_builtin_tools(
    _: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> list[BuiltinToolResponse]:
    """Return the built-in tools actually injected at runtime.

    These are harness's BUILTIN_TOOLS, filtered to the subset that
    ``resolve_harness_context`` injects (see ``_INJECTED_BUILTIN_TOOL_NAMES``).
    The returned set always matches what an Agent can actually call.
    """
    from agent_flow_harness import BUILTIN_TOOLS

    from app.engine.harness_integration.context import (
        _CONFIGURABLE_BUILTIN_TOOL_NAMES,
        _INJECTED_BUILTIN_TOOL_NAMES,
    )

    results: list[BuiltinToolResponse] = []
    for name in _INJECTED_BUILTIN_TOOL_NAMES:
        tool = BUILTIN_TOOLS.get(name)
        if tool is None:
            continue
        results.append(
            _basetool_to_response(
                tool, configurable=name in _CONFIGURABLE_BUILTIN_TOOL_NAMES
            )
        )
    return results


@router.get(
    "/app",
    response_model=list[BuiltinToolResponse],
    summary="List app-level tools (always-on task/workflow tools)",
    responses={403: {"description": "Forbidden — viewer+ role required"}},
)
async def list_app_tools(
    _: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> list[BuiltinToolResponse]:
    """Return the app-level tools (task/workflow management).

    These tools are always available to every Agent and are not
    configurable (``configurable=false``). They are shown in the tool
    center for discoverability but cannot be toggled off.
    """
    from app.engine.agent.workflow_executor import _TASK_TOOLS

    return [_basetool_to_response(tool, configurable=False) for tool in _TASK_TOOLS]


@router.get(
    "/prebuilt",
    response_model=list[dict],
    summary="List prebuilt tools (platform-registered)",
    responses={403: {"description": "Forbidden — viewer+ role required"}},
)
async def list_prebuilt_tools(
    _: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> list[dict]:
    """Return the list of prebuilt tools registered in TOOL_REGISTRY.

    Prebuilt tools are platform-level integrations (Wikipedia, Web Search,
    etc.) registered at startup via CommunityTool protocol.
    """
    from agent_flow_harness import TOOL_REGISTRY

    tools = []
    for entry in TOOL_REGISTRY.list_community_tools():
        info: dict[str, Any] = {
            "name": entry.name,
            "description": entry.description,
            "enabled_by_default": entry.enabled_by_default,
        }
        try:
            info["config_schema"] = entry.config_schema.model_json_schema()
        except Exception:
            info["config_schema"] = {}
        tools.append(info)
    return tools


def _doc_to_response(doc: dict) -> ToolResponse:
    """Convert a raw MongoDB document to ToolResponse.

    Files are read from disk (Skill filesystem) rather than the
    ``files`` field which is no longer stored in MongoDB.
    """
    name = doc.get("name", "")
    disk_files = list_skill_files(name) if name else []

    files = [
        SkillFileResponse(
            path=f["path"],
            content="",  # Content not loaded for list views
            size=f.get("size", 0),
        )
        for f in disk_files
    ]
    return ToolResponse(
        id=doc["_id"],
        name=doc["name"],
        description=doc.get("description", ""),
        input_schema=doc.get("input_schema", {}),
        output_schema=doc.get("output_schema", {}),
        instructions=doc.get("instructions", ""),
        source=doc.get("source", "markdown"),
        source_file=doc.get("source_file", ""),
        mcp_connection_id=doc.get("mcp_connection_id", ""),
        version=doc.get("version", 1),
        tags=doc.get("tags", []),
        files=files,
        created_at=doc.get("created_at", ""),
        updated_at=doc.get("updated_at", ""),
    )


class CustomToolCreate(BaseModel):
    """Request body for creating a custom tool (openapi / code / prebuilt)."""

    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    source: str = Field(..., description="openapi | code | prebuilt")
    user_args_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="用户参数 schema（Agent 绑定时填入）。sensitive=true 加密。",
    )
    llm_args_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="LLM 参数 schema（运行时 LLM 填入）。",
    )
    endpoint: dict[str, Any] = Field(default_factory=dict)
    code: str = Field(default="")
    prebuilt_name: str = Field(default="")


@router.post(
    "",
    response_model=ToolResponse,
    status_code=201,
    summary="Create a custom tool (OpenAPI / Code / Prebuilt)",
    responses={
        403: {"description": "Forbidden — developer+ role required"},
        409: {"description": "Tool name conflict"},
    },
)
async def create_custom_tool(
    body: CustomToolCreate,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> ToolResponse:
    """Create a custom tool from user configuration (no file upload needed).

    Supports three source types:
    - ``openapi``: HTTP endpoint with template-based URL/headers
    - ``code``: User-defined Python code executed in sandbox
    - ``prebuilt``: References a prebuilt tool from the tool registry
    """
    doc = await ToolService.create_custom_tool(
        name=body.name,
        description=body.description,
        source=body.source,
        user_args_schema=body.user_args_schema,
        llm_args_schema=body.llm_args_schema,
        endpoint=body.endpoint,
        code=body.code,
        prebuilt_name=body.prebuilt_name,
    )
    return ToolResponse(**doc)


@router.post(
    "/upload",
    response_model=ToolUploadResponse,
    summary="Upload Markdown Skill file(s) or directory",
    responses={
        403: {"description": "Forbidden — developer+ role required"},
        413: {"description": "File too large (>1MB) or directory too large (>10MB)"},
    },
)
async def upload_tools(
    files: list[UploadFile] = File(..., description="Skill Markdown 文件（支持多文件/文件夹上传）"),
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> ToolUploadResponse:
    """Upload one or more Markdown Skill files to register tools. (AC3)

    Supports two modes:

    1. **Directory mode**: Files grouped by directory name (e.g., ``my-skill/SKILL.md``).
       Each directory must contain a ``SKILL.md`` entry point. All files in the
       directory are stored as a single Tool document with ``files`` field populated.

    2. **Single-file mode**: Standalone ``.md`` files (legacy compatibility).

    Each file/directory is parsed independently — successful ones register
    tools, failed ones are collected into ``errors``.  Name conflicts
    are reported as errors (no overwrite).
    """
    from app.core.errors import ConflictError, ValidationError
    from app.engine.tool.skill_parser import (
        SkillParseError,
        parse_skill_directory,
        parse_skill_markdown,
    )

    # Group files by directory prefix
    dir_groups: dict[str, dict[str, str]] = defaultdict(dict)
    single_files: list[tuple[str, UploadFile]] = []

    for f in files:
        filename = f.filename or ""
        if not filename:
            continue

        parts = filename.split("/")
        if len(parts) >= 2 and parts[0]:
            # Directory mode: my-skill/SKILL.md
            dir_name = parts[0]
            dir_groups[dir_name][filename] = ""  # Will fill after reading
            single_files.append((filename, f))
        else:
            # Single file mode
            single_files.append((filename, f))

    # Read all file contents
    file_contents: dict[str, str] = {}
    for filename, f in single_files:
        try:
            content_bytes = await f.read()
        except Exception as exc:  # pragma: no cover - defensive
            file_contents[filename] = f"__read_error__: {exc}"
            continue

        if len(content_bytes) > MAX_FILE_SIZE:
            file_contents[filename] = f"__size_error__: {len(content_bytes)}"
            continue

        try:
            file_contents[filename] = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            file_contents[filename] = f"__decode_error__: {exc}"

    created: list[ToolResponse] = []
    errors: list[ToolUploadErrorItem] = []

    # Process directory groups
    processed_dirs: set[str] = set()
    for filename, _content in file_contents.items():
        parts = filename.split("/")
        if len(parts) < 2 or not parts[0]:
            continue

        dir_name = parts[0]
        if dir_name in processed_dirs:
            continue

        # Collect all files for this directory
        dir_files = {
            fn: file_contents[fn]
            for fn in file_contents
            if fn.startswith(f"{dir_name}/")
        }

        # Separate problem files (read/size/decode) from valid text files.
        # Problem files are reported per-file and skipped; the remaining
        # valid files still form the Skill (graceful skip, not whole-dir fail).
        valid_files: dict[str, str] = {}
        for fn, c in dir_files.items():
            if c.startswith("__read_error__"):
                errors.append(ToolUploadErrorItem(filename=fn, error=c.replace("__read_error__: ", "")))
            elif c.startswith("__size_error__"):
                errors.append(
                    ToolUploadErrorItem(
                        filename=fn,
                        error=f"文件过大（>1MB）：{c.replace('__size_error__: ', '')} bytes",
                    )
                )
            elif c.startswith("__decode_error__"):
                errors.append(
                    ToolUploadErrorItem(
                        filename=fn,
                        error="非 UTF-8 文本文件（可能是二进制），已跳过",
                    )
                )
            else:
                valid_files[fn] = c

        # Check total directory size (valid files only)
        total_bytes = sum(len(c.encode("utf-8")) for c in valid_files.values())
        if total_bytes > MAX_DIRECTORY_SIZE:
            errors.append(
                ToolUploadErrorItem(
                    filename=dir_name,
                    error=f"目录总大小（{total_bytes} bytes）超过上限（{MAX_DIRECTORY_SIZE} bytes）",
                )
            )
            processed_dirs.add(dir_name)
            continue

        # Parse directory
        try:
            parsed_dir = parse_skill_directory(valid_files, dir_name)
            doc = await ToolService.create_tool_from_directory(parsed_dir, dir_name)
            created.append(_doc_to_response(doc))
            processed_dirs.add(dir_name)
        except SkillParseError as exc:
            errors.append(ToolUploadErrorItem(filename=dir_name, error=exc.detail))
            processed_dirs.add(dir_name)
        except (ConflictError, ValidationError) as exc:
            errors.append(ToolUploadErrorItem(filename=dir_name, error=str(exc)))
            processed_dirs.add(dir_name)
        except Exception as exc:
            logger.error("tool_directory_upload_failed", dir_name=dir_name, error=str(exc))
            errors.append(ToolUploadErrorItem(filename=dir_name, error=f"创建失败: {exc}"))
            processed_dirs.add(dir_name)

    # Process single files (non-directory)
    for filename, content in file_contents.items():
        if "/" in filename:  # Skip files already processed as directories
            continue

        if content.startswith("__read_error__"):
            errors.append(ToolUploadErrorItem(filename=filename, error=content.replace("__read_error__: ", "")))
            continue
        if content.startswith("__size_error__"):
            errors.append(
                ToolUploadErrorItem(
                    filename=filename,
                    error=f"文件过大（>1MB）：{content.replace('__size_error__: ', '')} bytes",
                )
            )
            continue
        if content.startswith("__decode_error__"):
            errors.append(
                ToolUploadErrorItem(
                    filename=filename,
                    error="非 UTF-8 文本文件（可能是二进制），无法解析",
                )
            )
            continue

        try:
            parsed = parse_skill_markdown(content, filename)
        except SkillParseError as exc:
            errors.append(ToolUploadErrorItem(filename=filename, error=exc.detail))
            continue
        except UnicodeDecodeError as exc:
            errors.append(
                ToolUploadErrorItem(filename=filename, error=f"编码错误（非 UTF-8）：{exc}")
            )
            continue

        try:
            doc = await ToolService.create_tool_from_parsed(parsed, source_file=filename)
            created.append(_doc_to_response(doc))
        except ConflictError:
            errors.append(
                ToolUploadErrorItem(
                    filename=filename,
                    error=f"工具名称 '{parsed.name}' 已被占用",
                )
            )
        except Exception as exc:
            logger.error("tool_upload_failed", filename=filename, error=str(exc))
            errors.append(ToolUploadErrorItem(filename=filename, error=f"创建失败: {exc}"))

    logger.info(
        "tools_uploaded",
        total_files=len(files),
        created_count=len(created),
        error_count=len(errors),
        directory_count=len(processed_dirs),
    )

    return ToolUploadResponse(created=created, errors=errors)


@router.get(
    "",
    response_model=ToolListResponse,
    summary="List tools",
    responses={403: {"description": "Forbidden — viewer+ role required"}},
)
async def list_tools(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=200, description="Items per page"),
    name: str | None = Query(None, description="Filter by name (substring)"),
    source: str | None = Query(None, description="Filter by source (markdown / mcp / builtin)"),
    mcp_connection_id: str | None = Query(None, description="Filter by MCP connection ID"),
    _: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> ToolListResponse:
    """List all tools with pagination and optional filtering. (AC4)"""
    items, total = await ToolService.list_tools(
        page=page,
        page_size=page_size,
        name=name,
        source=source,
        mcp_connection_id=mcp_connection_id,
    )

    tools = [_doc_to_response(doc) for doc in items]
    return ToolListResponse(
        items=tools, total=total, page=page, page_size=page_size
    )


@router.get(
    "/{tool_id}",
    response_model=ToolResponse,
    summary="Get tool details",
    responses={
        403: {"description": "Forbidden — viewer+ role required"},
        404: {"description": "Tool not found"},
    },
)
async def get_tool(
    tool_id: str,
    _: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> ToolResponse:
    """Get a Tool by its ID. (AC4)"""
    from app.core.errors import NotFoundError

    doc = await ToolService.get_tool(tool_id)
    if doc is None:
        raise NotFoundError(
            code="TOOL_NOT_FOUND",
            message=f"工具 {tool_id} 不存在",
        )

    return _doc_to_response(doc)


@router.get(
    "/{tool_id}/files",
    response_model=SkillFileTreeResponse,
    summary="Get tool file tree",
    responses={
        403: {"description": "Forbidden — viewer+ role required"},
        404: {"description": "Tool not found"},
    },
)
async def get_tool_files(
    tool_id: str,
    _: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> SkillFileTreeResponse:
    """Get the file tree structure for a directory-based Tool.

    Returns a hierarchical tree of files/directories.  Legacy single-file
    tools return an empty list.
    """
    from app.core.errors import NotFoundError

    tree_nodes = await ToolService.get_tool_files(tool_id)
    if tree_nodes is None:
        raise NotFoundError(
            code="TOOL_NOT_FOUND",
            message=f"工具 {tool_id} 不存在",
        )

    # Convert dicts to Pydantic nodes
    def _dict_to_node(d: dict) -> SkillFileTreeNode:
        children = d.get("children")
        return SkillFileTreeNode(
            key=d["key"],
            title=d["title"],
            is_leaf=d.get("is_leaf", True),
            children=[_dict_to_node(c) for c in children] if children else None,
            size=d.get("size", 0),
        )

    nodes = [_dict_to_node(d) for d in tree_nodes]
    return SkillFileTreeResponse(tool_id=tool_id, files=nodes)


@router.get(
    "/{tool_id}/files/{file_path:path}",
    summary="Get tool file content",
    responses={
        403: {"description": "Forbidden — viewer+ role required"},
        404: {"description": "Tool or file not found"},
    },
)
async def get_tool_file_content(
    tool_id: str,
    file_path: str,
    _: UserResponse = Depends(require_any_role("admin", "developer", "operator", "viewer")),
) -> SkillFileResponse:
    """Get a single file's content from a Tool.

    Supports viewing and editing individual files within a Skill directory.
    """
    from app.core.errors import NotFoundError

    file_data = await ToolService.get_tool_file_content(tool_id, file_path)
    if file_data is None:
        raise NotFoundError(
            code="FILE_NOT_FOUND",
            message=f"文件 {file_path} 在工具 {tool_id} 中不存在",
        )

    return SkillFileResponse(
        path=file_data["path"],
        content=file_data["content"],
        size=file_data.get("size", 0),
    )


@router.put(
    "/{tool_id}/files/{file_path:path}",
    response_model=SkillFileResponse,
    summary="Update tool file content",
    responses={
        403: {"description": "Forbidden — developer+ role required"},
        404: {"description": "Tool or file not found"},
    },
)
async def update_tool_file(
    tool_id: str,
    file_path: str,
    body: SkillFileUpdate,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> SkillFileResponse:
    """Update a single file's content in a Tool.

    If the updated file is ``SKILL.md``, the Tool's name/description/
    instructions are also updated by re-parsing the frontmatter.
    """
    from app.core.errors import NotFoundError

    updated = await ToolService.update_tool_file(tool_id, file_path, body.content)
    if updated is None:
        raise NotFoundError(
            code="FILE_NOT_FOUND",
            message=f"文件 {file_path} 在工具 {tool_id} 中不存在",
        )

    return SkillFileResponse(
        path=updated["path"],
        content=updated["content"],
        size=updated.get("size", 0),
    )


@router.put(
    "/{tool_id}",
    response_model=ToolResponse,
    summary="Update a tool",
    responses={
        403: {"description": "Forbidden — developer+ role required"},
        404: {"description": "Tool not found"},
    },
)
async def update_tool(
    tool_id: str,
    body: ToolUpdate,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> ToolResponse:
    """Update a Tool's editable fields (tags). Auto-increments version. (AC5)"""
    from app.core.errors import NotFoundError

    doc = await ToolService.update_tool(
        tool_id=tool_id,
        tags=body.tags,
    )
    if doc is None:
        raise NotFoundError(
            code="TOOL_NOT_FOUND",
            message=f"工具 {tool_id} 不存在",
        )

    return _doc_to_response(doc)


@router.delete(
    "/{tool_id}",
    status_code=204,
    summary="Delete a tool",
    responses={
        403: {"description": "Forbidden — developer+ role required"},
        404: {"description": "Tool not found"},
        409: {"description": "Tool is referenced by one or more Agents"},
    },
)
async def delete_tool(
    tool_id: str,
    _: UserResponse = Depends(require_any_role("admin", "developer")),
) -> None:
    """Delete a Tool by ID. Checks for Agent references. (AC5)"""
    from app.core.errors import NotFoundError

    deleted = await ToolService.delete_tool(tool_id)
    if not deleted:
        raise NotFoundError(
            code="TOOL_NOT_FOUND",
            message=f"工具 {tool_id} 不存在",
        )

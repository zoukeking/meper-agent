"""KnowledgeBase-related Pydantic schemas for API request/response."""
from __future__ import annotations

from pydantic import BaseModel, Field


class KbFileResponse(BaseModel):
    """A single .md file in a KB directory."""

    path: str
    content: str
    size: int = 0


class KbFileUpdate(BaseModel):
    """Request body for updating a single KB file's content."""

    content: str = Field(..., min_length=1, description="新的文件内容")


class KbFileTreeNode(BaseModel):
    """A node in the KB file tree (file or directory)."""

    key: str = Field(..., description="唯一标识（相对路径）")
    title: str = Field(..., description="显示名称")
    is_leaf: bool = Field(default=True)
    children: list[KbFileTreeNode] | None = Field(default=None)
    size: int = Field(default=0, description="文件大小（仅文件节点有效）")


class KbFileTreeResponse(BaseModel):
    """Response for KB file tree endpoint."""

    kb_id: str
    files: list[KbFileTreeNode]


class KnowledgeBaseResponse(BaseModel):
    """KnowledgeBase data returned in API responses."""

    id: str
    name: str
    description: str = ""
    owner_user_id: str = ""
    status: str = "active"
    file_count: int = 0
    total_size: int = 0
    created_at: str
    updated_at: str


class KnowledgeBaseListResponse(BaseModel):
    """Paginated knowledge base list response."""

    items: list[KnowledgeBaseResponse]
    total: int
    page: int
    page_size: int


class KnowledgeBaseCreate(BaseModel):
    """Schema for creating a new KnowledgeBase (POST)."""

    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class KnowledgeBaseUpdate(BaseModel):
    """Schema for updating an existing KnowledgeBase (PUT)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class KbUploadErrorItem(BaseModel):
    """Single file error in an upload batch."""

    filename: str
    error: str


class KbUploadResponse(BaseModel):
    """Batch upload response — per-file results.

    KB files are not separate DB entities (they live on the FS), so
    ``created`` is a list of relative paths written, not full objects.
    """

    created: list[str] = Field(default_factory=list, description="成功写入的相对路径")
    errors: list[KbUploadErrorItem] = Field(default_factory=list)

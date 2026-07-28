"""KnowledgeBase data model for MongoDB — Markdown knowledge base metadata.

Each KB is a directory of ``.md`` files on the filesystem (managed by
``engine/tool/kb_fs.py``); MongoDB stores only metadata. File contents
do NOT live here (unlike ``Tool.files``), because KB docs can be large
and numerous, and the single source of truth for the agent is the FS.

Agents bind KBs via ``Agent.knowledge_base_ids`` and explore them at
runtime through the ``kb_glob`` / ``kb_grep`` / ``kb_read`` tools.
"""
from pydantic import BaseModel, Field
from pydantic.config import ConfigDict

from app.models.base import generate_id, utc_now


class KnowledgeBase(BaseModel):
    """MongoDB knowledge_base document model.

    Follows the same pattern as ``Tool`` — raw Pydantic model serialized
    to dict for MongoDB insertion/update.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(default_factory=lambda: generate_id("kb"), alias="_id")
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    owner_user_id: str = Field(default="")
    status: str = Field(default="active", description="active / archived")
    # Cached stats — refreshed by kb_service.recompute_stats after FS changes.
    file_count: int = Field(default=0)
    total_size: int = Field(default=0)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())

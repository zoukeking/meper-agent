"""KnowledgeBase business logic — CRUD + Markdown file management.

Tree-style KB: each KB is a directory of ``.md`` files on disk
(:mod:`app.engine.tool.kb_fs`); MongoDB stores only metadata. Agents
bind KBs via ``Agent.knowledge_base_ids``.
"""
from __future__ import annotations

import re

from loguru import logger

from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.mongodb import get_database
from app.engine.tool import kb_fs
from app.models.base import generate_id, utc_now
from app.services.tool_service import ToolService


class KnowledgeBaseService:
    """Service layer for KnowledgeBase operations."""

    COLLECTION = "knowledge_bases"

    @staticmethod
    def _collection():
        return get_database()[KnowledgeBaseService.COLLECTION]

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def create_kb(
        name: str, description: str, owner_user_id: str = ""
    ) -> dict:
        """Create a KB record + its on-disk directory."""
        now = utc_now().isoformat()
        doc = {
            "_id": generate_id("kb"),
            "name": name,
            "description": description,
            "owner_user_id": owner_user_id,
            "status": "active",
            "file_count": 0,
            "total_size": 0,
            "created_at": now,
            "updated_at": now,
        }
        await KnowledgeBaseService._collection().insert_one(doc)
        kb_fs.ensure_kb_dir(doc["_id"])
        logger.info("kb_created", kb_id=doc["_id"], name=name)
        return doc

    @staticmethod
    async def get_kb(kb_id: str) -> dict | None:
        return await KnowledgeBaseService._collection().find_one({"_id": kb_id})

    @staticmethod
    async def list_kbs(
        page: int = 1,
        page_size: int = 20,
        name: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict], int]:
        col = KnowledgeBaseService._collection()
        q: dict = {}
        if name:
            q["name"] = {"$regex": re.escape(name), "$options": "i"}
        if status:
            q["status"] = status
        total = await col.count_documents(q)
        cursor = (
            col.find(q)
            .sort("updated_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        items = await cursor.to_list(length=page_size)
        return items, total

    @staticmethod
    async def update_kb(
        kb_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> dict | None:
        col = KnowledgeBaseService._collection()
        if await col.find_one({"_id": kb_id}) is None:
            return None
        set_fields: dict = {"updated_at": utc_now().isoformat()}
        if name is not None:
            set_fields["name"] = name
        if description is not None:
            set_fields["description"] = description
        await col.update_one({"_id": kb_id}, {"$set": set_fields})
        return await KnowledgeBaseService.get_kb(kb_id)

    @staticmethod
    async def delete_kb(kb_id: str) -> bool:
        """Delete a KB. Refuses if any Agent references it.

        Raises ConflictError if referenced; returns True if deleted,
        False if not found.
        """
        col = KnowledgeBaseService._collection()
        existing = await col.find_one({"_id": kb_id})
        if existing is None:
            return False

        agents_col = get_database()["agents"]
        cursor = agents_col.find({"knowledge_base_ids": kb_id}, {"name": 1})
        referencing = await cursor.to_list(length=100)
        if referencing:
            names = [a.get("name", a.get("_id", "")) for a in referencing]
            raise ConflictError(
                code="KB_IN_USE",
                message=(
                    f"知识库 '{existing.get('name')}' 正在被以下 Agent 引用，"
                    f"无法删除：{', '.join(names)}"
                ),
                details={"agent_names": names},
            )

        result = await col.delete_one({"_id": kb_id})
        if result.deleted_count > 0:
            kb_fs.delete_kb_dir(kb_id)
            logger.info("kb_deleted", kb_id=kb_id)
            return True
        return False

    # ------------------------------------------------------------------
    # File operations (scan / read / write / delete on FS)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_kb_files(kb_id: str) -> list[dict] | None:
        if await KnowledgeBaseService._collection().find_one({"_id": kb_id}) is None:
            return None
        files = kb_fs.list_kb_files(kb_id)
        return ToolService._build_file_tree(files)

    @staticmethod
    async def get_kb_file_content(kb_id: str, rel_path: str) -> dict | None:
        if await KnowledgeBaseService._collection().find_one({"_id": kb_id}) is None:
            return None
        content = kb_fs.read_kb_file(kb_id, rel_path)
        if content is None:
            return None
        return {
            "path": rel_path,
            "content": content,
            "size": len(content.encode("utf-8")),
        }

    @staticmethod
    async def update_kb_file(
        kb_id: str, rel_path: str, new_content: str
    ) -> dict | None:
        col = KnowledgeBaseService._collection()
        if await col.find_one({"_id": kb_id}) is None:
            return None
        # Edit only — file must already exist (creation goes through upload).
        if kb_fs.read_kb_file(kb_id, rel_path) is None:
            return None
        try:
            kb_fs.write_kb_file(kb_id, rel_path, new_content)
        except ValueError as exc:
            raise ValidationError(code="KB_INVALID_PATH", message=str(exc))
        await col.update_one({"_id": kb_id}, {"$set": {"updated_at": utc_now().isoformat()}})
        await KnowledgeBaseService.recompute_stats(kb_id)
        return {
            "path": rel_path,
            "content": new_content,
            "size": len(new_content.encode("utf-8")),
        }

    @staticmethod
    async def delete_kb_file(kb_id: str, rel_path: str) -> bool:
        col = KnowledgeBaseService._collection()
        if await col.find_one({"_id": kb_id}) is None:
            return False
        ok = kb_fs.delete_kb_file(kb_id, rel_path)
        if ok:
            await col.update_one({"_id": kb_id}, {"$set": {"updated_at": utc_now().isoformat()}})
            await KnowledgeBaseService.recompute_stats(kb_id)
        return ok

    @staticmethod
    async def upload_files(
        kb_id: str, files: list[tuple[str, bytes]]
    ) -> dict:
        """Write a batch of ``(rel_path, raw_bytes)`` .md files into the KB.

        Returns ``{"created": [rel_path...], "errors": [{filename, error}...]}``.
        """
        col = KnowledgeBaseService._collection()
        if await col.find_one({"_id": kb_id}) is None:
            raise NotFoundError(
                code="KB_NOT_FOUND", message=f"知识库 {kb_id} 不存在"
            )

        created: list[str] = []
        errors: list[dict] = []
        max_file = settings.KB_MAX_FILE_SIZE

        for rel_path, raw in files:
            if not rel_path:
                continue
            # Normalize Windows separators + strip leading slash.
            rel_path = rel_path.replace("\\", "/").lstrip("/")
            segments = rel_path.split("/")
            if not rel_path.lower().endswith(".md"):
                errors.append({"filename": rel_path, "error": "仅支持 .md 文件"})
                continue
            if rel_path.startswith("/") or ".." in segments:
                errors.append({"filename": rel_path, "error": "非法路径"})
                continue
            if len(raw) > max_file:
                errors.append(
                    {"filename": rel_path, "error": f"文件过大（>{max_file} bytes）"}
                )
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                errors.append({"filename": rel_path, "error": "非 UTF-8 文本文件"})
                continue
            try:
                kb_fs.write_kb_file(kb_id, rel_path, content)
                created.append(rel_path)
            except ValueError:
                errors.append({"filename": rel_path, "error": "非法路径（路径穿越）"})
            except Exception as exc:
                errors.append({"filename": rel_path, "error": f"写入失败: {exc}"})

        await KnowledgeBaseService.recompute_stats(kb_id)
        await col.update_one({"_id": kb_id}, {"$set": {"updated_at": utc_now().isoformat()}})
        logger.info(
            "kb_files_uploaded",
            kb_id=kb_id,
            created_count=len(created),
            error_count=len(errors),
        )
        return {"created": created, "errors": errors}

    @staticmethod
    async def recompute_stats(kb_id: str) -> dict:
        file_count, total_size = kb_fs.compute_stats(kb_id)
        await KnowledgeBaseService._collection().update_one(
            {"_id": kb_id},
            {"$set": {"file_count": file_count, "total_size": total_size}},
        )
        return {"file_count": file_count, "total_size": total_size}

"""Knowledge Base filesystem — read / write / delete KB .md files on disk.

Each KB lives under ``{KB_CONTAINER_DIR}/{kb_id}/`` as a tree of ``.md``
files (tree-style KB: no index, no chunking — agents explore it at
runtime via kb_glob/kb_grep/kb_read). MongoDB keeps only metadata; file
content lives here on the filesystem.

Unlike ``skill_fs.py`` there is **no materialize** step — KB files are
mutated in place (upload/edit/delete touch the FS directly), because the
DB never stores file content for KBs.

Layout::

    KB_DIR/
      kb_xxx/
        README.md
        notes/
          api.md
          deploy.md
"""
from __future__ import annotations

import shutil
from pathlib import Path

from loguru import logger

from app.core.config import settings
from app.engine.tool.workspace import WorkspaceManager


def _kb_dir() -> Path:
    """Return the configured Knowledge Base root directory.

    Reads ``KB_CONTAINER_DIR`` from settings; defaults to
    ``~/.agent-flow/knowledge_bases/``.
    """
    return Path(settings.KB_CONTAINER_DIR).expanduser()


def get_kb_base_path(kb_id: str) -> Path:
    """Return the absolute path for a KB directory.

    Does **not** validate that the path exists.
    """
    return _kb_dir() / kb_id


def ensure_kb_dir(kb_id: str) -> Path:
    """Create the KB directory if missing and return it."""
    base = get_kb_base_path(kb_id)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _resolve(kb_id: str, rel_path: str) -> Path | None:
    """Resolve a relative path inside a KB, guarding against traversal."""
    return WorkspaceManager.safe_resolve_path(get_kb_base_path(kb_id), rel_path)


def read_kb_file(kb_id: str, rel_path: str) -> str | None:
    """Read a single file from the KB directory (UTF-8 text or None)."""
    full = _resolve(kb_id, rel_path)
    if full is None or not full.is_file():
        return None
    try:
        return full.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("kb_file_read_error", kb_id=kb_id, rel_path=rel_path, error=str(exc))
        return None


def write_kb_file(kb_id: str, rel_path: str, content: str) -> Path:
    """Write (create or overwrite) a file inside the KB directory.

    Raises ValueError if rel_path escapes the KB base (traversal).
    """
    full = _resolve(kb_id, rel_path)
    if full is None:
        raise ValueError(f"path escapes kb base: {rel_path}")
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def delete_kb_file(kb_id: str, rel_path: str) -> bool:
    """Delete a single file from the KB directory."""
    full = _resolve(kb_id, rel_path)
    if full is None or not full.is_file():
        return False
    full.unlink()
    return True


def delete_kb_dir(kb_id: str) -> bool:
    """Remove an entire KB directory from disk."""
    base = get_kb_base_path(kb_id)
    if not base.exists():
        return False
    shutil.rmtree(base)
    logger.info("kb_dir_deleted", kb_id=kb_id, path=str(base))
    return True


def list_kb_files(kb_id: str) -> list[dict]:
    """Scan the KB directory and return ``.md`` file entries for the tree view.

    Returns a list of dicts with ``path`` (relative) and ``size`` keys,
    suitable for ``_build_file_tree`` (same shape as ``list_skill_files``).
    """
    base = get_kb_base_path(kb_id)
    if not base.is_dir():
        return []
    entries: list[dict] = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() != ".md":
            continue
        rel = p.relative_to(base)
        entries.append({"path": str(rel), "size": p.stat().st_size})
    return entries


def compute_stats(kb_id: str) -> tuple[int, int]:
    """Return (file_count, total_size) over ``.md`` files in the KB."""
    files = list_kb_files(kb_id)
    total = sum(f["size"] for f in files)
    return len(files), total

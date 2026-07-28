"""Knowledge Base tools — kb_glob / kb_grep / kb_read (closure-injected).

Mirrors the harness ``SkillManager`` pattern: a stateful manager
constructed once per ``resolve_harness_context`` call, closure-capturing
the KB directories the agent is bound to, producing three StructuredTools
for the LLM.

Unlike the harness sandbox glob/grep (which operate on the session
workspace via ContextVar), these tools read KB directories directly from
the backend process filesystem — they do NOT depend on the sandbox, so
they work identically in local-dev and container modes.

Security:
    - Only the KB directories passed to the constructor are readable.
    - Every path is resolved through ``WorkspaceManager.safe_resolve_path``.
    - Only ``.md`` files are matched / read.
"""
from __future__ import annotations

import re
from pathlib import Path

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.core.config import settings
from app.engine.tool.workspace import WorkspaceManager

# ── args schemas (module-level; descriptions are per-instance) ─────────


class _KbGlobArgs(BaseModel):
    pattern: str = Field("**/*.md", description="glob 模式，默认列出所有 .md")
    kb_id: str | None = Field(None, description="限定单个知识库；省略则跨所有绑定知识库")


class _KbGrepArgs(BaseModel):
    pattern: str = Field(..., description="正则表达式")
    kb_id: str | None = Field(None, description="限定单个知识库；省略则跨所有绑定知识库")


class _KbReadArgs(BaseModel):
    path: str = Field(..., description="知识库内相对路径，或 kb_glob/kb_grep 返回的 'kb_id/路径' 形式")
    kb_id: str | None = Field(None, description="当 path 不含 kb 前缀时显式指定")


class KbManager:
    """Build KB-bound tools for a single agent resolve.

    Args:
        kb_roots: mapping of ``kb_id -> base path``. Only these KBs are
            readable by the produced tools.
    """

    def __init__(self, kb_roots: dict[str, Path]):
        self._kb_roots = kb_roots

    # ── path resolution ────────────────────────────────────────────────

    def _resolve(self, kb_id: str, rel_path: str) -> Path | None:
        base = self._kb_roots.get(kb_id)
        if base is None:
            return None
        return WorkspaceManager.safe_resolve_path(base, rel_path)

    def _parse_target(self, path: str, kb_id: str | None) -> tuple[str, str] | None:
        """Resolve ``(kb_id, rel_path)`` from tool args.

        Accepts either an explicit ``kb_id`` + relative path, or a
        compound ``{kb_id}/{rel}`` path (as returned by kb_glob/kb_grep).
        Bare paths resolve against the only KB when exactly one is bound.
        """
        if kb_id:
            return (kb_id, path) if kb_id in self._kb_roots else None
        parts = path.split("/", 1)
        if len(parts) == 2 and parts[0] in self._kb_roots:
            return parts[0], parts[1]
        if len(self._kb_roots) == 1:
            return next(iter(self._kb_roots)), path
        return None

    def _target_kb_ids(self, kb_id: str | None) -> list[str]:
        if kb_id and kb_id in self._kb_roots:
            return [kb_id]
        return list(self._kb_roots)

    # ── operations ─────────────────────────────────────────────────────

    def glob(self, pattern: str, kb_id: str | None = None) -> str:
        max_n = settings.KB_GLOB_MAX_RESULTS
        results: list[str] = []
        done = False
        for kid in self._target_kb_ids(kb_id):
            if done:
                break
            base = self._kb_roots[kid]
            try:
                iterator = base.glob(pattern)
            except Exception:
                continue
            for p in iterator:
                if not p.is_file() or p.suffix.lower() != ".md":
                    continue
                results.append(f"{kid}/{p.relative_to(base)}")
                if len(results) >= max_n:
                    done = True
                    break
        if not results:
            return "(no matches)"
        out = "\n".join(results)
        if done:
            out += f"\n... [truncated at {max_n} results]"
        return out

    def grep(self, pattern: str, kb_id: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"Error: invalid regex: {exc}"
        max_files = settings.KB_GREP_MAX_FILES
        max_matches = settings.KB_GREP_MAX_MATCHES
        matches: list[str] = []
        scanned = 0
        done = False
        for kid in self._target_kb_ids(kb_id):
            if done:
                break
            base = self._kb_roots[kid]
            for p in base.rglob("*"):
                if not p.is_file() or p.suffix.lower() != ".md":
                    continue
                scanned += 1
                if scanned > max_files:
                    break
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = p.relative_to(base)
                for lineno, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append(f"{kid}/{rel}:{lineno}: {line}")
                        if len(matches) >= max_matches:
                            done = True
                            break
                if done:
                    break
        if not matches:
            return "(no matches)"
        out = "\n".join(matches)
        if done:
            out += f"\n... [truncated at {max_matches} matches]"
        return out

    def read(self, path: str, kb_id: str | None = None) -> str:
        target = self._parse_target(path, kb_id)
        if target is None:
            return "Error: path not available in bound knowledge bases. Call kb_glob first to list files."
        kid, rel = target
        full = self._resolve(kid, rel)
        if full is None or not full.is_file():
            return f"Error: file not found: {path}"
        if full.suffix.lower() != ".md":
            return "Error: only .md files are readable."
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Error reading file: {exc}"
        if len(content.encode("utf-8")) > settings.KB_READ_MAX_BYTES:
            return content[: settings.KB_READ_MAX_BYTES] + "\n... [truncated]"
        return content

    # ── tool factory ───────────────────────────────────────────────────

    def make_tools(self) -> list[StructuredTool]:
        kb_hint = ", ".join(self._kb_roots) or "none"

        async def _glob_coro(pattern: str, kb_id: str | None = None) -> str:
            return self.glob(pattern, kb_id)

        async def _grep_coro(pattern: str, kb_id: str | None = None) -> str:
            return self.grep(pattern, kb_id)

        async def _read_coro(path: str, kb_id: str | None = None) -> str:
            return self.read(path, kb_id)

        glob_tool = StructuredTool.from_function(
            _glob_coro,
            name="kb_glob",
            description=(
                "列出知识库中匹配 glob 模式的 Markdown 文件，返回 'kb_id/相对路径' 列表。"
                f" 可用知识库: {kb_hint}"
            ),
            args_schema=_KbGlobArgs,
            coroutine=_glob_coro,
        )
        grep_tool = StructuredTool.from_function(
            _grep_coro,
            name="kb_grep",
            description=(
                "在知识库 .md 文件中搜索正则表达式，返回 'kb_id/路径:行号: 行内容'。"
                f" 可用知识库: {kb_hint}"
            ),
            args_schema=_KbGrepArgs,
            coroutine=_grep_coro,
        )
        read_tool = StructuredTool.from_function(
            _read_coro,
            name="kb_read",
            description="读取知识库中一个 .md 文件的完整内容（单次最多约 16KB）。",
            args_schema=_KbReadArgs,
            coroutine=_read_coro,
        )
        return [glob_tool, grep_tool, read_tool]

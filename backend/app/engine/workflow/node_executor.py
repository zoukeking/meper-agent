"""Node executor base class and implementations (Strategy pattern).

Each node type has a corresponding executor that implements ``execute()``.
The ``WorkflowEngine`` selects the appropriate executor based on node type.
"""
from __future__ import annotations

import asyncio
import operator as _operator
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class NodeResult:
    """Result of executing a single workflow node."""

    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    error_code: str = ""
    # For gateway nodes: the selected branch target node ID
    selected_branch: str | None = None


class BaseNodeExecutor(ABC):
    """Abstract base for all workflow node executors.

    Subclasses must implement ``execute()``.
    """

    def __init__(self, node_id: str, node_config: dict[str, Any]) -> None:
        self.node_id = node_id
        self.node_config = node_config

    @abstractmethod
    async def execute(self, variables: dict[str, Any]) -> NodeResult:
        """Execute the node and return the result.

        Args:
            variables: Current variable pool (read-only snapshot).

        Returns:
            NodeResult with success/failure and output data.
        """
        ...


# ── Start ──


class StartNodeExecutor(BaseNodeExecutor):
    """Initialise variable pool with Task input.

    Config::

        {
            "output_variables": [
                {
                    "name": "query",
                    "type": "text",
                    "constraints": {"required": true, "default_value": ""},
                    ...
                },
                ...
            ],
            "input_mapping": { "var_name": "{{ input.field }}" }  # optional override
        }

    ``required`` / ``default_value`` are read from ``constraints``
    (matching the frontend ``VariableListEditor`` schema).  Top-level
    ``required`` / ``default`` keys are still accepted for backward
    compatibility with legacy data.
    """

    async def execute(self, variables: dict[str, Any]) -> NodeResult:
        logger.debug(
            "node_start",
            node_id=self.node_id,
            input_keys=list(variables.keys()),
            has_input_mapping=bool(self.node_config.get("input_mapping")),
        )

        output: dict[str, Any] = {}

        # 1. If input_mapping is explicitly configured, use it (takes precedence)
        input_mapping = self.node_config.get("input_mapping", {})
        if input_mapping:
            from app.engine.workflow.expression import ExpressionEngine

            engine = ExpressionEngine(variables)
            output = engine.resolve_dict(input_mapping)
            return NodeResult(success=True, output=output)

        # 2. Otherwise, initialize variables from output_variables definition
        output_variables = self.node_config.get("output_variables", [])
        task_input = variables.get("input")
        if not isinstance(task_input, dict):
            task_input = {}

        if isinstance(output_variables, list) and output_variables:
            missing_required: list[str] = []

            for var_def in output_variables:
                if not isinstance(var_def, dict):
                    continue
                name = var_def.get("name", "")
                if not name:
                    continue

                var_type = var_def.get("type", "text")

                # Read required/default from `constraints` (frontend schema)
                # with top-level fallback for legacy data.
                constraints = var_def.get("constraints") if isinstance(var_def.get("constraints"), dict) else {}
                raw_required = constraints.get("required", var_def.get("required"))
                is_required = bool(raw_required) if raw_required is not None else False
                default_val = constraints.get("default_value", var_def.get("default"))

                # Use input value if provided, else fall back to default.
                if name in task_input:
                    value = task_input[name]
                elif default_val not in (None, ""):
                    value = default_val
                else:
                    value = None

                # File type: delegate to file_validator for resolution.
                if var_type == "file":
                    if value in (None, ""):
                        # No file provided — handled by required check below
                        pass
                    else:
                        from app.engine.workflow.file_validator import (
                            validate_file_variable,
                        )

                        resolved, ferror = await validate_file_variable(value, var_def)
                        if ferror:
                            return NodeResult(
                                success=False,
                                output={},
                                error_message=f"Start 节点文件变量 '{name}' 验证失败: {ferror}",
                            )
                        output[name] = resolved
                        continue

                # Required validation: None or empty string counts as missing.
                if is_required and value in (None, ""):
                    missing_required.append(name)
                else:
                    output[name] = value

            if missing_required:
                return NodeResult(
                    success=False,
                    output={},
                    error_message=(
                        f"Start 节点必填变量缺失: {', '.join(missing_required)}。"
                        f"请在 dispatch_workflow 的 params 中补充这些字段。"
                    ),
                )

            return NodeResult(success=True, output=output)

        # 3. Fallback: pass through all task input
        return NodeResult(success=True, output={"input": task_input})


# ── End ──


class EndNodeExecutor(BaseNodeExecutor):
    """Summarise output and signal completion.

    Config: ``{"output_mapping": { "result": "{{ node_id.field }}" }}``
    """

    async def execute(self, variables: dict[str, Any]) -> NodeResult:
        logger.debug("node_end", node_id=self.node_id)
        output_mapping = self.node_config.get("output_mapping", {})

        if output_mapping:
            from app.engine.workflow.expression import ExpressionEngine

            engine = ExpressionEngine(variables)
            resolved = engine.resolve_dict(output_mapping)
            return NodeResult(success=True, output=resolved)

        return NodeResult(success=True, output={"status": "completed"})


# ── Agent ──


class AgentNodeExecutor(BaseNodeExecutor):
    """Invoke an Agent to perform reasoning/actions.

    Config::

        {
            "agent_id": "agent_xxx",
            "system_prompt_override": "...",  # optional
            "input_prompt": "{{ ... }}",       # prompt template
            "temperature": 0.7                 # optional
        }
    """

    async def execute(self, variables: dict[str, Any]) -> NodeResult:
        agent_id = self.node_config.get("agent_id", "")

        # ── Story 4-15: Read task identity from system variables ──
        # System variables (task_id, user_id) are bound to the VariablePool
        # at task creation time by WorkflowEngine. They are available to all
        # nodes via variables["system"]["task_id"] etc. This is more robust
        # than constructor injection because it works for both initial
        # execution and checkpoint resume without special-casing.
        sys_vars = variables.get("system", {}) or {}
        task_id = sys_vars.get("task_id", "")

        logger.debug(
            "node_agent_start",
            node_id=self.node_id,
            agent_id=agent_id,
            task_id=task_id,
            has_input_mapping=bool(self.node_config.get("input_mapping")),
        )

        if not agent_id:
            return NodeResult(success=False, output={}, error_message="agent_id 未配置")

        user_id = sys_vars.get("user_id", "")
        if not task_id or not user_id:
            missing = []
            if not task_id:
                missing.append("system.task_id")
            if not user_id:
                missing.append("system.user_id")
            return NodeResult(
                success=False,
                output={},
                error_message=(
                    "AgentNodeExecutor 缺少执行身份: "
                    f"{', '.join(missing)}（无法定位 task workspace）"
                ),
            )

        from app.engine.workflow.expression import ExpressionEngine

        engine = ExpressionEngine(variables)

        # input_query → user message（查询）
        # resolve_str 保证返回 str：input 经 dispatch_workflow 落库时可能是
        # bool/int/dict（LLM 给的 JSON 原生类型），裸 resolve 会返回原类型，
        # 下游 .strip() 会抛 AttributeError。统一用 resolve_str 归一化。
        input_query = self.node_config.get("input_query", "")
        resolved_query = engine.resolve_str(input_query) if input_query else ""

        # input_prompt → context 卡槽覆盖（注入系统提示，不作为 user message）
        # 向后兼容：也读取 slot_values.context
        input_prompt = self.node_config.get("input_prompt", "")
        resolved_context = engine.resolve_str(input_prompt) if input_prompt else ""
        if not resolved_context:
            legacy_slot_ctx = self.node_config.get("slot_values", {}).get("context", "")
            resolved_context = engine.resolve_str(legacy_slot_ctx) if legacy_slot_ctx else ""

        try:
            from langchain_core.messages import SystemMessage

            from app.db.mongodb import get_database

            db = get_database()
            agent_doc = await db["agents"].find_one({"_id": agent_id})
            if agent_doc is None:
                return NodeResult(
                    success=False,
                    output={},
                    error_message=f"Agent {agent_id} 不存在",
                )

            # 仅允许覆盖 context 卡槽（role/task/constraints/output_format 由 Agent 自身决定）
            context_overrides = {"context": resolved_context} if resolved_context else None

            temperature = self.node_config.get("temperature")
            if temperature is not None:
                agent_doc["temperature_override"] = temperature

            # harness 的 invoke 内部会自己 build graph,这里不需要构造。

            # Build system prompt via slot renderer (context override + variable pool)
            from app.engine.agent.slot_renderer import render_system_prompt_full

            system_text = await render_system_prompt_full(
                agent_doc,
                node_slot_overrides=context_overrides,
                variable_pool=variables,
            )
        except Exception as exc:
            # 展开 ExceptionGroup 以显示真正原因
            detail = str(exc)
            if hasattr(exc, "exceptions"):
                sub_errors = "; ".join(str(e) for e in exc.exceptions)  # type: ignore[attr-defined]
                detail = f"{exc} — 子异常: {sub_errors}"
            logger.error("node_agent_setup_failed", node_id=self.node_id, error=detail)
            return NodeResult(
                success=False,
                output={},
                error_message=f"Agent 初始化失败: {detail}",
            )

        # Execute with timeout protection and retry
        timeout_ms = self.node_config.get("timeout_ms", 300000)
        max_retry = self.node_config.get("max_retry", 0)
        retry_delay_ms = self.node_config.get("retry_delay_ms", 2000)

        # thread_id 固定为 {task_id}_{node_id}，使 checkpoint 可按 task+node 反查，
        # 供前端按需读取 agent 节点的完整执行过程（thinking/tool_call/tool_result）。
        # 重试时由 API 层清除 LangGraph checkpointer 中该 thread 的旧数据，
        # 避免残留消息导致 "multiple non-consecutive system messages"。
        import time as _time

        _thread_id = f"{task_id}_{self.node_id}"

        # ── 恢复路径：若 checkpoint 中有 agent_thread_id，说明此 agent 节点
        # 之前被取消（interrupt 挂起），用 Command(resume) 续接 REACT 循环，
        # 完整上下文（messages/tool结果/step_count）从 MongoDB checkpointer 恢复。
        resume_thread_id = sys_vars.get("resume_agent_thread_id", "")
        # 恢复时文件扫描不用 node_start_ts 过滤——agent 在上次执行（被取消前）
        # 已写入 output/ 的文件 mtime 早于本次恢复时间，会被错误过滤。
        # sha256 去重已保证不会重复注册。
        is_resume = bool(resume_thread_id)

        # 组装初始消息：system prompt（含工具声明 + context 卡槽注入）+ 用户查询
        # 注意：input_prompt 已经通过 context 卡槽注入系统提示，不作为独立 user message
        # 只有 input_query 作为 user message
        # strip() 防止空白字符串逃过 truthiness 检查导致 LLM API 报
        # "messages: at least one message is required"（空 SystemMessage 不算有效消息）
        stripped_system = system_text.strip() if system_text else ""
        stripped_query = resolved_query.strip() if resolved_query else ""

        initial_messages: list = []
        if stripped_system:
            initial_messages.append(SystemMessage(content=stripped_system))

        if stripped_query:
            initial_messages.append({"role": "user", "content": stripped_query})

        # 兜底：没有任何有效消息时 LLM API 会报 "at least one message is required"
        if not initial_messages:
            logger.warning(
                "node_agent_empty_messages",
                node_id=self.node_id,
                agent_id=agent_id,
                input_query=input_query,
                variables=variables,
            )
            initial_messages.append({
                "role": "user",
                "content": f"请根据你的系统提示执行任务（input_query='{input_query}' 解析结果为空）",
            })

        # ── Story 4-15: Set up task workspace context for Agent tools ──
        # Without this, builtin tools like write_to_output / read / write
        # / bash have no workspace to write to and fall back to PROJECT_ROOT.
        from app.engine.agent.builtin_tools import (
            reset_workspace_context,
        )
        from app.engine.tool.workspace import WorkspaceManager

        task_workspace = WorkspaceManager.create_task_workspace(
            user_id, task_id,
        )
        # harness 路径:workspace 注入由 invoke 内部的 resolve_harness_context
        # 接管(传入 task_workspace),无需手动 set_workspace_context。
        workspace_token = None
        node_start_ts = _time.time()
        logger.info(
            "agent_node_workspace_set",
            node_id=self.node_id,
            task_id=task_id,
            user_id=user_id,
            workspace_root=str(task_workspace.root),
        )

        last_error: str | None = None
        # 记录被取消时的 agent thread_id，供 engine 保存到 checkpoint
        self.agent_thread_id: str = ""

        try:
            for attempt in range(1 + max_retry):
                try:
                    # 取消检查器：每轮 REACT 循环在 compress_node 检查 task 状态，
                    # 若已 CANCELLED 则 interrupt() 优雅挂起，完整上下文存入 checkpointer。
                    async def _cancel_checker() -> bool:
                        from app.db.mongodb import get_database

                        doc = await get_database()["tasks"].find_one(
                            {"_id": task_id}, {"status": 1},
                        )
                        return bool(doc and doc.get("status") == "cancelled")

                    if resume_thread_id:
                        # 恢复被取消的 agent：用 Command(resume) 续接 REACT 循环
                        from app.engine.harness_integration import resume_agent

                        result = await asyncio.wait_for(
                            resume_agent(
                                agent_doc,
                                {
                                    "messages": initial_messages,
                                    "session_id": resume_thread_id,
                                    "user_id": user_id,
                                    "agent_id": agent_id,
                                },
                                thread_id=resume_thread_id,
                                resume_value="continue",
                                workspace=task_workspace,
                                cancel_checker=_cancel_checker,
                            ),
                            timeout=timeout_ms / 1000,
                        )
                        # 恢复成功后清除标志，后续重试走正常 invoke
                        resume_thread_id = ""
                    else:
                        from app.engine.harness_integration import invoke

                        result = await asyncio.wait_for(
                            invoke(
                                agent_doc,
                                {
                                    "messages": initial_messages,
                                    "session_id": _thread_id,
                                    "user_id": user_id,
                                    "agent_id": agent_id,
                                },
                                workspace=task_workspace,
                                cancel_checker=_cancel_checker,
                            ),
                            timeout=timeout_ms / 1000,
                        )

                    # ── 检测 LangGraph interrupt（取消挂起）──
                    # interrupt() 后 ainvoke 返回的 state 带 __interrupt__ 键。
                    # 此时 agent 的完整上下文已存入 MongoDB checkpointer（按 _thread_id），
                    # 记录 thread_id 供 engine 保存到 checkpoint，恢复时用 Command(resume) 续接。
                    if isinstance(result, dict) and result.get("__interrupt__"):
                        self.agent_thread_id = _thread_id
                        return NodeResult(
                            success=False,
                            output={},
                            error_message="",
                            error_code="AGENT_INTERRUPTED",
                        )
                    output_content = ""
                    if result.get("messages"):
                        last_msg = result["messages"][-1]
                        # 兼容 LangChain AIMessage（.content）和 dict（["content"] / .get("content")）
                        if isinstance(last_msg, dict):
                            output_content = last_msg.get("content", str(last_msg))
                        elif hasattr(last_msg, "content"):
                            output_content = last_msg.content
                        else:
                            output_content = str(last_msg)

                    # ── 提取 Agent 工具生成的文件引用（Story 4-15）──
                    # 1) MCP / artifact-based (legacy path)
                    mcp_files = self._extract_files_from_messages(
                        result.get("messages") or [],
                    )
                    # 2) 扫描 task workspace output/ 中新生成的文件并注册到
                    #    file_library（覆盖内置工具真实行为：write_to_output
                    #    等通过 contextvars 落到本 task 专属 output/）。
                    registered_files = await self._register_task_output_files(
                        task_workspace, node_start_ts,
                        task_id=task_id, user_id=user_id,
                        register_all=is_resume,
                    )

                    # 合并去重（registered 优先，保留最新的 file_id/大小/路径）
                    files_output = self._merge_file_outputs(
                        mcp_files, registered_files,
                    )

                    return NodeResult(
                        success=True,
                        output={
                            "response": output_content,
                            "agent_id": agent_id,
                            "files": files_output,
                            # Token usage from harness (execution.py puts mw.summary
                            # into result["usage"]); surfaced for timeline + task total.
                            "usage": result.get("usage") or {},
                        },
                    )
                except TimeoutError:
                    last_error = f"Agent 执行超时 ({timeout_ms}ms)"
                    logger.warning("node_agent_timeout", node_id=self.node_id, attempt=attempt + 1)
                except Exception as exc:
                    last_error = f"Agent 执行失败: {exc}"
                    logger.error("node_agent_failed", node_id=self.node_id, error=str(exc), attempt=attempt + 1)

                if attempt < max_retry:
                    logger.info("agent_retry", node_id=self.node_id, attempt=attempt + 1, max_retry=max_retry)
                    await asyncio.sleep(retry_delay_ms / 1000)
        finally:
            # Always reset the contextvar so other coroutines in the same
            # event loop don't accidentally inherit this task's workspace.
            if workspace_token is not None:
                reset_workspace_context(workspace_token)

        return NodeResult(
            success=False,
            output={},
            error_message=last_error or "Agent 执行失败",
        )

    @staticmethod
    def _merge_file_outputs(
        mcp_files: list[dict[str, Any]],
        registered_files: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge MCP-artifact files and task-workspace-registered files.

        Both sources may report the same file; registered files win because
        they have authoritative ``file_id``/``storage_key`` values from
        file_library. Deduplication is by ``file_id`` when present,
        otherwise by ``name``.
        """
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        merged: list[dict[str, Any]] = []

        for entry in registered_files:
            fid = entry.get("file_id", "")
            name = entry.get("name", "")
            if fid and fid in seen_ids:
                continue
            if not fid and name in seen_names:
                continue
            merged.append(entry)
            if fid:
                seen_ids.add(fid)
            if name:
                seen_names.add(name)

        for entry in mcp_files:
            fid = entry.get("file_id", "")
            name = entry.get("name", "")
            if fid and fid in seen_ids:
                continue
            if not fid and name in seen_names:
                continue
            merged.append(entry)
            if fid:
                seen_ids.add(fid)
            if name:
                seen_names.add(name)

        return merged

    async def _register_task_output_files(
        self,
        task_workspace: Any,
        node_start_ts: float,
        *,
        task_id: str,
        user_id: str,
        register_all: bool = False,
    ) -> list[dict[str, Any]]:
        """Register files newly created in the task workspace to file_library.

        Story 4-15: After the Agent graph finishes, we walk
        ``task_workspace.output_dir`` and register every file with an
        mtime > ``node_start_ts`` to ``file_library`` as
        ``origin_kind='workflow_run'`` / ``origin_id=task_id``. Files
        already registered with the same ``sha256`` for this task are
        skipped (deduplication).

        Returns:
            A list of dicts with ``file_id``, ``name``, ``size``,
            ``mime_type``, ``storage_key`` — the canonical output shape
            downstream nodes consume via ``{{ agent_node.files[i].file_id }}``.
        """
        if not task_workspace.output_dir.exists():
            return []

        # Local import to avoid top-level cycle (file_service depends on
        # db, which loads settings — safe at runtime, not at import time).
        from app.models.file_library import FileConsumerKind
        from app.services.file_service import FileService
        from app.services.file_storage import LocalFileStorage

        file_service = FileService(LocalFileStorage())

        # Pre-fetch known sha256s for this task to avoid registering the
        # same file twice (e.g. on node retry).
        existing_cursor = file_service._file_refs().find(
            {
                "origin_kind": FileConsumerKind.WORKFLOW_RUN.value,
                "origin_id": task_id,
            },
            {"sha256": 1, "_id": 0},
        )
        existing_docs = await existing_cursor.to_list(length=None)
        seen_sha256: set[str] = {doc["sha256"] for doc in existing_docs if doc.get("sha256")}

        registered: list[dict[str, Any]] = []
        import hashlib
        import mimetypes

        for path in sorted(task_workspace.output_dir.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                # File was removed between rglob and stat — skip.
                continue
            if not register_all and stat.st_mtime < node_start_ts:
                # Pre-existing file from an earlier run; do not re-register.
                # 恢复场景(register_all=True)跳过此过滤——agent 在上次执行
                # （被取消前）已写入的文件 mtime 早于本次恢复时间。
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                logger.warning(
                    "agent_node_read_output_failed",
                    node_id=self.node_id,
                    path=str(path),
                    error=str(exc),
                )
                continue

            sha256 = hashlib.sha256(data).hexdigest()
            if sha256 in seen_sha256:
                continue

            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            try:
                fref = await file_service.create(
                    data=data,
                    filename=path.name,
                    mime_type=mime_type,
                    owner_user_id=user_id,
                    origin_kind=FileConsumerKind.WORKFLOW_RUN,
                    origin_id=task_id,
                )
            except Exception as exc:  # noqa: BLE001 — surface but don't crash node
                logger.error(
                    "agent_node_register_file_failed",
                    node_id=self.node_id,
                    path=str(path),
                    error=str(exc),
                )
                continue

            seen_sha256.add(sha256)
            registered.append({
                "file_id": fref.id,
                "name": fref.name,
                "size": fref.size,
                "mime_type": fref.mime_type,
                "storage_key": fref.storage_key,
            })
            logger.info(
                "agent_node_file_registered",
                node_id=self.node_id,
                task_id=task_id,
                file_id=fref.id,
                name=fref.name,
                size=fref.size,
            )

        return registered

    def _extract_files_from_messages(self, messages: list) -> list[dict[str, Any]]:
        """Scan LangGraph messages for tool-emitted file references.

        Tools may attach a ``files`` array to their ToolMessage result. We
        normalise each entry to a stable ``{file_id, name, mime_type, size}``
        shape and de-duplicate by ``file_id`` so downstream consumers see a
        single canonical list regardless of how many tool calls emitted the
        same file.
        """
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()

        for msg in messages:
            payload: Any = None
            if isinstance(msg, dict):
                payload = msg.get("artifact") or msg.get("additional_kwargs")
                if payload is None and isinstance(msg.get("content"), (dict, list)):
                    payload = msg.get("content")
            else:
                if getattr(msg, "artifact", None) is not None:
                    payload = msg.artifact
                elif getattr(msg, "additional_kwargs", None):
                    payload = msg.additional_kwargs

            if payload is None:
                continue

            if isinstance(payload, list):
                file_entries = [p for p in payload if isinstance(p, dict) and p.get("file_id")]
            elif isinstance(payload, dict) and isinstance(payload.get("files"), list):
                file_entries = [p for p in payload["files"] if isinstance(p, dict) and p.get("file_id")]
            else:
                continue

            for entry in file_entries:
                file_id = str(entry.get("file_id", ""))
                if not file_id or file_id in seen:
                    continue
                seen.add(file_id)
                # size may be int or numeric string; coerce defensively
                try:
                    size_val = int(entry.get("size", 0) or 0)
                except (TypeError, ValueError):
                    size_val = 0
                collected.append({
                    "file_id": file_id,
                    "name": str(entry.get("name", "")),
                    "mime_type": str(entry.get("mime_type", "application/octet-stream")),
                    "size": size_val,
                })

        return collected


# ── Tool ──


class ToolNodeExecutor(BaseNodeExecutor):
    """Invoke a registered tool from the tool pool.

    Config::

        {
            "tool_id": "tool_xxx",
            "params": { "key": "{{ node.field }}" },  # resolved at runtime
            "timeout_ms": 30000,
            "retry_policy": { "max_retries": 3, "backoff_ms": 1000 }
        }
    """

    async def execute(self, variables: dict[str, Any]) -> NodeResult:
        tool_id = self.node_config.get("tool_id", "")
        if not tool_id:
            return NodeResult(success=False, output={}, error_message="tool_id 未配置")

        # Resolve params from variables
        from app.engine.workflow.expression import ExpressionEngine

        engine = ExpressionEngine(variables)
        raw_params = self.node_config.get("params", {})
        resolved_params = engine.resolve_dict(raw_params) if isinstance(raw_params, dict) else raw_params

        try:
            # Fetch tool from MongoDB
            from app.db.mongodb import get_database

            db = get_database()
            tool_doc = await db["tools"].find_one({"$or": [{"_id": tool_id}, {"name": tool_id}]})
            if tool_doc is None:
                return NodeResult(
                    success=False,
                    output={},
                    error_message=f"Tool {tool_id} 不存在",
                )

            # Execute based on tool source type
            source = tool_doc.get("source", "markdown")

            if source == "mcp":
                # MCP tool — invoke via MCP client
                return await self._execute_mcp_tool(tool_doc, resolved_params)
            else:
                # Markdown/Skill tool — return instructions as context
                return NodeResult(
                    success=True,
                    output={
                        "tool_name": tool_doc.get("name", ""),
                        "tool_description": tool_doc.get("description", ""),
                        "instructions": tool_doc.get("instructions", ""),
                        "params": resolved_params,
                        "note": "工具的完整执行由 Agent 推理循环处理",
                    },
                )
        except Exception as exc:
            logger.error("node_tool_failed", node_id=self.node_id, error=str(exc))
            return NodeResult(
                success=False,
                output={},
                error_message=f"Tool 调用失败: {exc}",
            )

    async def _execute_mcp_tool(
        self,
        tool_doc: dict[str, Any],
        params: dict[str, Any],
    ) -> NodeResult:
        """Execute an MCP-sourced tool with timeout protection and retry."""
        try:
            from app.engine.tool.mcp_tool_cache import get_mcp_tools_cached

            # Get connection ID from tool doc
            conn_id = tool_doc.get("mcp_connection_id", "")
            if not conn_id:
                return NodeResult(
                    success=False,
                    output={},
                    error_message="MCP 工具缺少 connection_id",
                )

            # Get tools for this connection
            tools = await get_mcp_tools_cached([conn_id])

            # Find the matching tool by name
            tool_name = tool_doc.get("name", "")
            matching = [t for t in tools if t.name == tool_name or t.name.endswith(f"__{tool_name}")]
            if not matching:
                return NodeResult(
                    success=False,
                    output={},
                    error_message=f"MCP 工具 {tool_name} 未在连接中找到",
                )

            tool = matching[0]

            # Retry and timeout configuration
            timeout_ms = self.node_config.get("timeout_ms", 30000)
            timeout_s = timeout_ms / 1000
            retry_policy = self.node_config.get("retry_policy", {})
            max_retries = retry_policy.get("max_retries", 0)
            backoff_ms = retry_policy.get("backoff_ms", 1000)

            last_error: str | None = None
            for attempt in range(1 + max_retries):
                try:
                    result = await asyncio.wait_for(tool.ainvoke(params), timeout=timeout_s)
                    return NodeResult(success=True, output={"result": result, "tool_id": tool_doc["_id"]})
                except TimeoutError:
                    last_error = f"MCP 工具执行超时 ({timeout_ms}ms)"
                    logger.warning("mcp_tool_timeout", node_id=self.node_id, attempt=attempt + 1)
                except Exception as exc:
                    last_error = f"MCP 工具执行失败: {exc}"
                    logger.error("mcp_tool_execution_failed", node_id=self.node_id, error=str(exc), attempt=attempt + 1)

                if attempt < max_retries:
                    logger.info("tool_retry", node_id=self.node_id, attempt=attempt + 1, max_retries=max_retries)
                    await asyncio.sleep(backoff_ms / 1000)

            return NodeResult(
                success=False,
                output={},
                error_message=last_error or "MCP 工具执行失败",
            )

        except Exception as exc:
            logger.error("mcp_tool_setup_failed", error=str(exc))
            return NodeResult(
                success=False,
                output={},
                error_message=f"MCP 工具执行失败: {exc}",
            )


# ── Gateway ──


class GatewayNodeExecutor(BaseNodeExecutor):
    """Evaluate conditions and select a branch.

    Config::

        {
            "conditions": [
                { "expression": "{{ node_1.result.status }}", "operator": "==", "expected": "ok", "target": "node_3" },
                { "expression": "{{ node_1.count }}", "operator": ">", "expected": 10, "target": "node_4" }
            ],
            "default_branch": "node_5",
            "fallback_on_error": "node_5"
        }

    Each condition has an ``operator`` (one of ==, !=, >, <, >=, <=; defaults to
    ``"=="`` for backward compatibility). ``==``/``!=`` perform case-insensitive
    comparison for strings (e.g. ``"APPROVE"`` matches ``"approve"``), and keep
    the bool-coercion behavior for boolean expected values.

    Conditions are evaluated in order; the first match wins.
    """

    async def execute(self, variables: dict[str, Any]) -> NodeResult:
        conditions = self.node_config.get("conditions", [])
        default_branch = self.node_config.get("default_branch", "")
        fallback = self.node_config.get("fallback_on_error", default_branch)

        from app.engine.workflow.expression import ExpressionEngine

        engine = ExpressionEngine(variables)

        for cond in conditions:
            expression = cond.get("expression", "")
            expected = cond.get("expected", True)
            op = cond.get("operator", "==")  # default "==" for backward compat
            target = cond.get("target", "")

            try:
                actual = engine.resolve(expression)
                if _gateway_compare(actual, expected, op):
                    logger.debug(
                        "gateway_match",
                        node_id=self.node_id,
                        target=target,
                    )
                    return NodeResult(
                        success=True,
                        output={"selected_branch": target, "condition": expression},
                        selected_branch=target,
                    )
            except Exception as exc:
                logger.warning("gateway_condition_error", node_id=self.node_id, expression=expression, error=str(exc))
                continue

        # No condition matched — use fallback
        logger.debug("gateway_fallback", node_id=self.node_id, target=fallback)
        return NodeResult(
            success=True,
            output={"selected_branch": fallback, "condition": "default"},
            selected_branch=fallback,
        )


# Operator → implementation for ordering comparisons (>, <, >=, <=).
# == and != are handled specially in ``_gateway_compare`` for case-insensitivity.
_GATEWAY_ORDER_OPS: dict[str, Any] = {
    ">": _operator.gt,
    "<": _operator.lt,
    ">=": _operator.ge,
    "<=": _operator.le,
}


def _gateway_compare(actual: Any, expected: Any, op: str) -> bool:
    """Compare ``actual`` against ``expected`` using operator ``op``.

    - ``==`` / ``!=``: case-insensitive for str vs str (e.g. ``"APPROVE"`` ==
      ``"approve"``); bool expected coerces ``actual`` via ``bool()``; other
      types use native equality. This preserves the pre-operator behavior while
      adding case-insensitivity for the common "match approval decision" case.
    - ``contains`` / ``not_contains``: 子串包含（actual 是字符串、expected 是
      子串）或元素包含（actual 是列表/元组、expected 是元素）。字符串场景下
      同样不区分大小写，与 ``==`` 保持一致。类型不匹配（如 actual 不是
      str/list）视为不匹配。
    - ``>`` / ``<`` / ``>=`` / ``<=``: native ordering comparison; a
      ``TypeError`` (incomparable types) is treated as no-match.
    - unknown operator: no-match (returns ``False``).
    """
    if op in ("==", "!="):
        a: Any
        e: Any
        if isinstance(actual, str) and isinstance(expected, str):
            a, e = actual.lower(), expected.lower()
        elif isinstance(expected, bool):
            a, e = bool(actual), expected
        else:
            a, e = actual, expected
        return a == e if op == "==" else a != e

    if op in ("contains", "not_contains"):
        # 字符串子串包含（大小写不敏感）
        if isinstance(actual, str) and isinstance(expected, str):
            contained = expected.lower() in actual.lower()
        # 列表/元组元素包含（等值比较，不递归）
        elif isinstance(actual, (list, tuple)):
            contained = expected in actual
        else:
            # 类型不匹配（如 actual 为 None/数字/字典）一律视为不包含
            contained = False
        return contained if op == "contains" else not contained

    func = _GATEWAY_ORDER_OPS.get(op)
    if func is None:
        return False
    try:
        return bool(func(actual, expected))
    except TypeError:
        return False


# ── Parallel ──


class ParallelNodeExecutor(BaseNodeExecutor):
    """Execute multiple branches in parallel and merge results.

    Config::

        {
            "branches": [
                { "id": "branch_1", "start_node": "node_a" },
                { "id": "branch_2", "start_node": "node_b" }
            ],
            "join_strategy": "all",      # all | any | n-of-m
            "join_count": null,           # for n-of-m strategy
            "scope": "shared"             # shared | isolated
        }

    The actual branch execution is handled by the WorkflowEngine.
    This executor only manages the parallel fan-out configuration.
    """

    async def execute(self, variables: dict[str, Any]) -> NodeResult:
        branches = self.node_config.get("branches", [])
        scope = self.node_config.get("scope", "shared")

        branch_ids = [b.get("id", f"branch_{i}") for i, b in enumerate(branches)]
        start_nodes = {
            b.get("id", f"branch_{i}"): b.get("start_node", "")
            for i, b in enumerate(branches)
        }

        return NodeResult(
            success=True,
            output={
                "branches": branch_ids,
                "start_nodes": start_nodes,
                "join_strategy": self.node_config.get("join_strategy", "all"),
                "join_count": self.node_config.get("join_count"),
                "scope": scope,
            },
        )


# ── Subflow ──


class SubflowNodeExecutor(BaseNodeExecutor):
    """Create a child Task from another Workflow and wait for results.

    Config::

        {
            "workflow_id": "wf_xxx",
            "input_mapping": { "var_1": "{{ node_1.result }}" },
            "result_mapping": { "output": "{{ result.field }}" },
            "timeout_ms": 600000
        }
    """

    async def execute(self, variables: dict[str, Any]) -> NodeResult:
        workflow_id = self.node_config.get("workflow_id", "")
        if not workflow_id:
            return NodeResult(success=False, output={}, error_message="workflow_id 未配置")

        # Resolve input from variables
        from app.engine.workflow.expression import ExpressionEngine

        engine = ExpressionEngine(variables)
        input_mapping = self.node_config.get("input_mapping", {})
        resolved_input = engine.resolve_dict(input_mapping) if isinstance(input_mapping, dict) else {}

        try:
            from app.db.mongodb import get_database
            from app.engine.workflow.engine import WorkflowEngine
            from app.models.task import Task

            db = get_database()

            # Fetch child workflow definition
            child_wf_doc = await db["workflows"].find_one({"_id": workflow_id})
            if child_wf_doc is None:
                return NodeResult(
                    success=False,
                    output={},
                    error_message=f"Workflow {workflow_id} 不存在",
                )

            # Build child task document (in-memory)
            child_task = Task(
                workflow_id=workflow_id,
                input=resolved_input,
                created_by="system",
                created_by_type="system",
                parent_task_id=variables.get("_task_id"),
                call_chain=variables.get("call_chain", []) + [self.node_id],
            )
            child_doc = child_task.model_dump(by_alias=True)

            # Execute child workflow with timeout
            timeout_ms = self.node_config.get("timeout_ms", 600000)
            child_engine = WorkflowEngine()
            try:
                child_output = await asyncio.wait_for(
                    child_engine.execute_task(child_doc, child_wf_doc),
                    timeout=timeout_ms / 1000,
                )
            except TimeoutError:
                return NodeResult(
                    success=False,
                    output={},
                    error_message=f"Subflow 执行超时 ({timeout_ms}ms)",
                )

            # Map results using result_mapping against child variable pool
            result_mapping = self.node_config.get("result_mapping", {})
            if result_mapping and child_engine._pool is not None:
                child_vars = child_engine._pool.get_all()
                child_expr_engine = ExpressionEngine(child_vars)
                resolved_result = child_expr_engine.resolve_dict(result_mapping)
            else:
                resolved_result = child_output

            return NodeResult(
                success=True,
                output={
                    "child_task_id": child_doc["_id"],
                    "child_output": resolved_result,
                    "workflow_id": workflow_id,
                },
            )
        except Exception as exc:
            logger.error("node_subflow_failed", node_id=self.node_id, error=str(exc))
            return NodeResult(
                success=False,
                output={},
                error_message=f"Subflow 执行失败: {exc}",
            )


# ── Factory ──

_NODE_EXECUTOR_MAP: dict[str, type[BaseNodeExecutor]] = {
    "start": StartNodeExecutor,
    "end": EndNodeExecutor,
    "agent": AgentNodeExecutor,
    "tool": ToolNodeExecutor,
    "gateway": GatewayNodeExecutor,
    "human": None,  # lazy-loaded in get_node_executor to avoid circular import
    "parallel": ParallelNodeExecutor,
    "subflow": SubflowNodeExecutor,
}

# Lazy import cache
_human_executor_cls: type[BaseNodeExecutor] | None = None


def get_node_executor(node_type: str, node_id: str, node_config: dict[str, Any]) -> BaseNodeExecutor:
    """Factory: return the appropriate executor for *node_type*.

    Raises ``ValueError`` for unknown node types.
    """
    cls = _NODE_EXECUTOR_MAP.get(node_type)
    if cls is None and node_type != "human":
        raise ValueError(f"未知的节点类型: {node_type}")

    if node_type == "human":
        global _human_executor_cls
        if _human_executor_cls is None:
            from app.engine.workflow.nodes.human import HumanNodeExecutor
            _human_executor_cls = HumanNodeExecutor
        return _human_executor_cls(node_id=node_id, node_config=node_config)

    return cls(node_id=node_id, node_config=node_config)  # type: ignore[return-value]

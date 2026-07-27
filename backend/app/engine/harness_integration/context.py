"""harness context assembly — resolve & release harness injection objects.

Transforms backend application objects (agent doc, LLM config, tools,
sandbox settings, workspace) into the injection dict that the harness
graph expects. This is the "装配" half of the adapter layer.
"""
from __future__ import annotations

from typing import Any

from loguru import logger


def get_checkpointer() -> Any:
    """返回 harness 的 checkpointer 单例。

    harness 默认用 MemorySaver,应用层在 lifespan 启动时通过
    ``configure_checkpointer`` 覆盖为 MongoDBSaver。
    """
    from agent_flow_harness import get_checkpointer as _harness_get_checkpointer

    return _harness_get_checkpointer()


# 运行时实际注入的 harness 内建工具(单一事实源)。
# 端点 `/api/v1/tools/builtin` 与 `resolve_harness_context` 共同引用此名单,
# 保证「端点展示的 = 运行时注入的」。
_INJECTED_BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "bash", "read", "write", "glob", "grep", "ask_clarification",
)

# 可配子集 —— 用户可在 Agent 配置页勾选的内建工具(其余始终开启、不可关闭)。
# ask_clarification 是能力型工具,关闭会导致 Agent 无法澄清,故始终开启。
_CONFIGURABLE_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    {"bash", "read", "write", "glob", "grep"}
)

# 新建 Agent 时默认启用的内建工具(白名单语义:列表中的工具才会注入)。
# 保持与 _CONFIGURABLE_BUILTIN_TOOL_NAMES 一致(按 _INJECTED_BUILTIN_TOOL_NAMES
# 的顺序),创建端点与前端默认值共同引用,作为单一事实源避免名单漂移。
DEFAULT_BUILTIN_CONFIG: tuple[str, ...] = tuple(
    n for n in _INJECTED_BUILTIN_TOOL_NAMES if n in _CONFIGURABLE_BUILTIN_TOOL_NAMES
)


def _decrypt_user_args(tool_doc: dict, user_args: dict) -> dict:
    """解密 user_args 里标记为 sensitive 的字段。

    Agent 绑定时 sensitive 字段加密存储（前缀 enc:），运行时解密。
    """
    if not user_args:
        return {}
    from app.core.crypto import CryptoError, decrypt_secret

    user_schema = tool_doc.get("user_args_schema", {})
    props = user_schema.get("properties", {})
    result = {}
    for key, value in user_args.items():
        prop = props.get(key, {})
        if prop.get("sensitive") and isinstance(value, str) and value.startswith("enc:"):
            try:
                result[key] = decrypt_secret(value[4:])
            except (CryptoError, Exception):
                result[key] = value  # 解密失败用原值
        else:
            result[key] = value
    return result


async def resolve_harness_context(
    agent: dict,
    state: dict,
    *,
    enable_thinking: bool = False,
    workspace: Any | None = None,
    user_token: str | None = None,
) -> dict:
    """装配 harness 执行所需的全部注入物,返回 dict 供 graph + config 使用。

    合并三层工具策略:
      ① 应用层工具(task/工作流工具) — 来自 workflow_executor._TASK_TOOLS
      ② harness 内建工具(bash/read/write/write_to_output/glob/grep/ask_clarification)
         — 委托 Sandbox / 能力型工具,名单见 _INJECTED_BUILTIN_TOOL_NAMES
      ③ Skill(load_skill)+ MCP — harness SkillManager / McpToolLoader

    Args:
        workspace: 可选,workflow agent 节点传入已创建的 task workspace。
        user_token: 可选,外部终端用户 token(回调验证模式)。设置后 MCP
            工具调用会把该 token 放进 Authorization header 透传给 MCP
            server;为 None 时(兼容模式/平台用户)用 MCP connection 静态凭证。

    Returns:
        dict 含 keys: agent_doc, llm, tools, sb_token, ws_token,
              user_token, middlewares, context_window
              middlewares, context_window
    """
    agent_id = agent.get("_id", "agent")
    session_id = state.get("session_id", "")
    logger.debug(
        "harness_context_resolve_start",
        agent_id=agent_id,
        agent_name=agent.get("name", ""),
        session_id=session_id,
        enable_thinking=enable_thinking,
        has_workspace=workspace is not None,
    )
    load_errors: list[dict] = []

    from pathlib import Path

    from agent_flow_harness import (
        BUILTIN_TOOLS,
        DockerSandbox,
        DockerSandboxConfig,
        SandboxContext,
        SkillManager,
        UsageMiddleware,
        set_sandbox_context,
    )

    from app.core.config import settings
    from app.engine.agent.builtin_tools import set_workspace_context
    from app.engine.agent.context import get_context_window_async
    from app.engine.agent.workflow_executor import _TASK_TOOLS
    from app.engine.llm_factory import get_llm_client

    # 1. 解析 LLM + context_window
    llm = await get_llm_client(agent, enable_thinking=enable_thinking)
    model_ref = agent.get("default_model") or (agent.get("llm_config") or {}).get("default_model", "")
    context_window = await get_context_window_async(model_ref)

    # 2. 工具合并:① 应用层 task 工具 + ② harness 内建工具
    app_tools = list(_TASK_TOOLS)
    # 内建工具按 agent.builtin_config 白名单过滤(opt-in):
    #   - ask_clarification 等(configurable=false)始终注入
    #   - bash/read/write/write_to_output/glob/grep 需在 builtin_config 中显式启用
    #   - 为向后兼容,选中 bash 时隐式连带 read/write/write_to_output(与 preview/system-prompt 语义一致)
    builtin_config = set(agent.get("builtin_config") or [])
    if "bash" in builtin_config:
        builtin_config |= {"read", "write", "write_to_output"}
    harness_builtin_tools = []
    for name in _INJECTED_BUILTIN_TOOL_NAMES:
        tool = BUILTIN_TOOLS.get(name)
        if tool is None:
            continue
        if name not in _CONFIGURABLE_BUILTIN_TOOL_NAMES:
            # 始终开启的能力型工具(如 ask_clarification)
            harness_builtin_tools.append(tool)
        elif name in builtin_config:
            harness_builtin_tools.append(tool)
    all_tools = app_tools + harness_builtin_tools

    # 3. Skill:用 harness SkillManager 替换 backend 的 load_skill
    skills_dir = Path(settings.SKILLS_CONTAINER_DIR).expanduser()
    skill_mgr = SkillManager(
        skills_dir=skills_dir,
        base_path_prefix=settings.SANDBOX_CONTAINER_SKILLS_DIR if settings.SANDBOX_ENABLED else None,
    )
    from app.models.compat import resolve_skill_ids

    skill_ids = resolve_skill_ids(agent)
    if skill_ids:
        from app.services.tool_service import ToolService

        skill_docs = await ToolService.get_tools_by_ids(skill_ids)
        allowed_names = {d.get("name") for d in skill_docs if d.get("name")}
        if allowed_names:
            skill_mgr.set_allowed(allowed_names)
            all_tools.append(skill_mgr.make_load_tool())

    # 4. MCP:用 harness McpToolLoader 替换 backend 的 MCP 工具。
    # 逐 server 加载而非一次性全部加载,以便单个 server 失败时收集错误
    # 信息暴露给前端(不再静默跳过)。
    mcp_connection_ids = agent.get("mcp_connection_ids") or []
    if mcp_connection_ids:
        from agent_flow_harness import McpConnectionConfig, McpToolLoader

        from app.services.mcp_connection_service import McpConnectionService

        mcp_loader = McpToolLoader()
        for conn_id in mcp_connection_ids:
            conn_doc = await McpConnectionService.get_connection(conn_id)
            if not conn_doc:
                load_errors.append({
                    "tool_name": f"mcp:{conn_id}",
                    "error": f"MCP 连接 {conn_id} 不存在",
                })
                continue
            config = McpConnectionConfig(
                name=conn_doc.get("name", conn_id),
                url=conn_doc.get("url", ""),
                protocol=conn_doc.get("protocol", "streamable-http"),
                auth_type=conn_doc.get("auth_type", "none"),
                auth_config=conn_doc.get("auth_config") or {},
                timeout=conn_doc.get("timeout", 30),
                default_params=conn_doc.get("default_params") or {},
            )
            try:
                conn_tools = await mcp_loader.load_tools([config])
                if not conn_tools:
                    load_errors.append({
                        "tool_name": f"mcp:{conn_doc.get('name', conn_id)}",
                        "error": f"MCP server {conn_doc.get('name')} 未返回工具(可能连接失败或无可用工具)",
                    })
                all_tools.extend(conn_tools)
            except Exception as exc:
                logger.warning("mcp_connection_load_failed", connection=conn_doc.get("name"), error=str(exc))
                load_errors.append({
                    "tool_name": f"mcp:{conn_doc.get('name', conn_id)}",
                    "error": f"MCP 工具加载失败: {exc}",
                })

    # 4.5. 自定义工具 (openapi / code / prebuilt)。收集加载失败暴露给前端。
    custom_tools = agent.get("custom_tools") or []
    if custom_tools:
        from app.engine.tool.tool_builder import build_tool
        from app.services.tool_service import ToolService

        for binding in custom_tools:
            tool_id = binding.get("tool_id", "")
            user_args = binding.get("user_args", {})
            if not tool_id:
                continue
            docs = await ToolService.get_tools_by_ids([tool_id])
            if not docs:
                load_errors.append({
                    "tool_name": f"custom:{tool_id}",
                    "error": f"自定义工具 {tool_id} 不存在",
                })
                continue
            doc = docs[0]
            # 解密 user_args 里的 sensitive 字段
            user_args = _decrypt_user_args(doc, user_args)
            try:
                tool = await build_tool(doc, user_args=user_args)
            except Exception as exc:
                logger.warning("custom_tool_build_failed", tool_id=tool_id, error=str(exc))
                load_errors.append({
                    "tool_name": f"custom:{doc.get('name', tool_id)}",
                    "error": f"自定义工具构建失败: {exc}",
                })
                continue
            if tool is None:
                load_errors.append({
                    "tool_name": f"custom:{doc.get('name', tool_id)}",
                    "error": f"自定义工具 {doc.get('name')} 构建返回空",
                })
            else:
                all_tools.append(tool)

    # 5. 构造 agent_doc(含 token budget guard 防止会话被滥用)
    agent_max_tokens = int(agent.get("max_tokens") or 0)
    session_token_limit = agent_max_tokens if agent_max_tokens > 0 else settings.DEFAULT_SESSION_MAX_TOKENS
    agent_doc = {
        "_id": agent.get("_id", "agent"),
        "name": agent.get("name", "agent"),
        "guards": [
            {"name": "token_budget", "config": {"max_total_tokens": session_token_limit}},
        ],
    }

    # 6. sandbox:用 backend 配置构造 harness DockerSandbox
    sandbox_config = DockerSandboxConfig(
        image=settings.SANDBOX_IMAGE,
        enabled=settings.SANDBOX_ENABLED,
        mem_limit=settings.SANDBOX_MEM_LIMIT,
        cpu_quota=settings.SANDBOX_CPU_QUOTA,
        timeout=settings.SANDBOX_TIMEOUT,
        max_output_bytes=settings.SANDBOX_MAX_OUTPUT_BYTES,
        network_mode=settings.SANDBOX_NETWORK_MODE,
        container_workspace_dir=settings.SANDBOX_CONTAINER_WORKSPACE_DIR,
        container_skills_dir=settings.SANDBOX_CONTAINER_SKILLS_DIR,
    )

    # 7. workspace
    if workspace is not None:
        ws_token = set_workspace_context(workspace)
        work_dir = workspace.tmp_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        sandbox_mounts = {
            "tmp": workspace.tmp_dir,
            "input": workspace.input_dir,
            "output": workspace.output_dir,
        }
        sandbox_id = f"{state.get('session_id') or workspace.root.name}"
    else:
        session_id = state.get("session_id", "")
        user_id = state.get("user_id", "")
        ws_token = None
        if session_id and user_id:
            try:
                from app.engine.tool.workspace import WorkspaceManager
                ws = WorkspaceManager.get_workspace(user_id, session_id)
                ws.input_dir.mkdir(parents=True, exist_ok=True)
                ws.output_dir.mkdir(parents=True, exist_ok=True)
                ws.tmp_dir.mkdir(parents=True, exist_ok=True)
                ws_token = set_workspace_context(ws)
            except Exception:
                pass
        work_dir = Path(settings.WORKSPACES_CONTAINER_DIR) / user_id / session_id / "tmp"
        work_dir.mkdir(parents=True, exist_ok=True)
        sandbox_mounts = {
            "tmp": work_dir,
            "input": Path(settings.WORKSPACES_CONTAINER_DIR) / user_id / session_id / "input",
            "output": Path(settings.WORKSPACES_CONTAINER_DIR) / user_id / session_id / "output",
        }
        sandbox_id = f"{session_id}"

    sandbox = DockerSandbox(
        sandbox_id=sandbox_id,
        work_dir=work_dir,
        mounts=sandbox_mounts,
        config=sandbox_config,
        timeout=settings.SANDBOX_TIMEOUT,
    )

    # 8. 注入 sandbox context
    sb_token = set_sandbox_context(SandboxContext(sandbox=sandbox))

    # 8.5 注入 user_token context(供 MCP 工具透传给 MCP server)。
    # asyncio.create_task 会复制 contextvars,所以即便 stream/resume 的
    # 真正执行在后台任务里,MCP loader 的 interceptor 也能读到。
    from agent_flow_harness import set_user_token_context

    ut_token = set_user_token_context(user_token)

    logger.debug(
        "harness_context_resolved",
        agent_id=agent_id,
        model=model_ref,
        context_window=context_window,
        tool_count=len(all_tools),
        tool_names=[getattr(t, "name", str(t)) for t in all_tools],
        sandbox_enabled=settings.SANDBOX_ENABLED,
        sandbox_id=sandbox_id,
        session_token_limit=session_token_limit,
    )

    return {
        "agent_doc": agent_doc,
        "llm": llm,
        "tools": all_tools,
        "load_errors": load_errors,
        "sb_token": sb_token,
        "ws_token": ws_token,
        "ut_token": ut_token,
        "middlewares": [UsageMiddleware()],
        "context_window": context_window,
    }


def release_harness_context(hctx: dict) -> None:
    """释放 resolve_harness_context 持有的 contextvar token(在 finally 调用)。"""
    from agent_flow_harness import reset_sandbox_context, reset_user_token_context

    from app.engine.agent.builtin_tools import reset_workspace_context

    reset_sandbox_context(hctx["sb_token"])
    if hctx.get("ws_token") is not None:
        reset_workspace_context(hctx["ws_token"])
    reset_user_token_context(hctx["ut_token"])


async def _maybe_migrate_legacy(graph, config, legacy_records: list[dict] | None) -> None:
    """灌入老session历史到 thread(仅当 MIGRATE_LEGACY_SESSIONS 且 thread 空)。"""
    if not legacy_records:
        return
    from app.core.config import settings

    if not settings.MIGRATE_LEGACY_SESSIONS:
        return
    state = await graph.aget_state(config)
    if state.values:
        return
    from app.engine.harness_integration.history import rebuild_messages_from_records

    rebuilt = await rebuild_messages_from_records(legacy_records)
    if rebuilt:
        await graph.aupdate_state(config, {"messages": rebuilt})

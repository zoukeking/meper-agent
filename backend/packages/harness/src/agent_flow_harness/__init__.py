"""agent-flow-harness package.

Reusable LangGraph agent runtime. The harness intentionally knows nothing
about HTTP, persistence, auth, or workspace storage: callers inject every
external dependency (LLM, tools, checkpointer, workspace) so the same
runtime can be embedded in different backends.
"""

from __future__ import annotations

from agent_flow_harness.checkpointer import (
    build_mongo_saver,
    configure_checkpointer,
    get_checkpointer,
    reset_checkpointer,
)
from agent_flow_harness.context_engineering import (
    ContextStrategy,
    HybridStrategy,
    SlidingWindowStrategy,
    SummarizationStrategy,
)
from agent_flow_harness.graph import build_agent_graph, build_config, get_thread_messages, run_agent, run_agent_streaming
from agent_flow_harness.guards import (
    ContentGuard,
    Guard,
    GuardResult,
    TimeBudgetGuard,
    TokenBudgetGuard,
    ToolRateLimitGuard,
    resolve_guards,
)
from agent_flow_harness.llm import (
    apply_thinking_mode,
    build_client_from_doc,
    build_client_from_env,
    build_thinking_kwargs,
    supports_thinking,
)
from agent_flow_harness.middleware import (
    AuditMiddleware,
    Middleware,
    MiddlewareChain,
    MIDDLEWARE_REGISTRY,
    PromptInjectionMiddleware,
    TraceMiddleware,
    UsageMiddleware,
    resolve_middleware,
)
from agent_flow_harness.mcp import (
    McpConnectionConfig,
    McpToolLoader,
    get_user_token_context,
    reset_user_token_context,
    set_user_token_context,
)
from agent_flow_harness.state import AgentState
from agent_flow_harness.subagents import (
    SubAgentContext,
    SubAgentRegistry,
    SubAgentSpec,
    delegate_to_subagent,
    get_subagent_context,
    reset_subagent_context,
    set_subagent_context,
)
from agent_flow_harness.sandbox import (
    DockerSandbox,
    DockerSandboxConfig,
    GrepMatch,
    LocalSandbox,
    Sandbox,
    SandboxContext,
    SandboxProvider,
    SandboxResult,
    bash,
    get_sandbox_context,
    get_sandbox_provider,
    glob,
    grep,
    read,
    reset_sandbox_context,
    reset_sandbox_provider,
    set_sandbox_context,
    set_sandbox_provider,
    write,
)
from agent_flow_harness.interaction import ask_clarification, tool_search
from agent_flow_harness.tools import (
    BUILTIN_TOOL_NAMES,
    BUILTIN_TOOLS,
    TOOL_REGISTRY,
    CommunityTool,
    ToolRegistry,
    resolve_variable,
)
from agent_flow_harness.slots import (
    SLOT_NAMES,
    SLOT_SCHEMA,
    SlotDef,
    TOOL_DECLARATION_SLOT,
    render_system_prompt_full,
    render_system_prompt_simple,
)
from agent_flow_harness.skills import SkillManager, SkillSpec

__version__ = "0.1.0"

# Default checkpointer: an in-memory saver so that interrupt / aget_state /
# thread persistence work out of the box. Applications embedding the harness
# should call ``configure_checkpointer(MongoDBSaver(...), overwrite=True)`` at
# startup to switch to a durable backend.
try:
    from langgraph.checkpoint.memory import MemorySaver as _MemorySaver

    configure_checkpointer(_MemorySaver(), overwrite=True)
except ImportError:  # pragma: no cover - langgraph always provides MemorySaver
    pass

__all__ = [
    "AgentState",
    "AuditMiddleware",
    "BUILTIN_TOOL_NAMES",
    "BUILTIN_TOOLS",
    "CommunityTool",
    "ContentGuard",
    "ContextStrategy",
    "DockerSandbox",
    "DockerSandboxConfig",
    "Guard",
    "GuardResult",
    "GrepMatch",
    "HybridStrategy",
    "MIDDLEWARE_REGISTRY",
    "Middleware",
    "MiddlewareChain",
    "McpConnectionConfig",
    "McpToolLoader",
    "PromptInjectionMiddleware",
    "LocalSandbox",
    "SLOT_NAMES",
    "SLOT_SCHEMA",
    "SlotDef",
    "TOOL_DECLARATION_SLOT",
    "SubAgentContext",
    "SubAgentRegistry",
    "SubAgentSpec",
    "Sandbox",
    "SandboxContext",
    "SandboxProvider",
    "SandboxResult",
    "SkillManager",
    "SkillSpec",
    "SlidingWindowStrategy",
    "SummarizationStrategy",
    "TOOL_REGISTRY",
    "TimeBudgetGuard",
    "TokenBudgetGuard",
    "ToolRateLimitGuard",
    "ToolRegistry",
    "TraceMiddleware",
    "UsageMiddleware",
    "apply_thinking_mode",
    "ask_clarification",
    "bash",
    "build_agent_graph",
    "build_client_from_doc",
    "build_client_from_env",
    "build_thinking_kwargs",
    "build_config",
    "build_mongo_saver",
    "configure_checkpointer",
    "delegate_to_subagent",
    "get_checkpointer",
    "get_sandbox_context",
    "get_sandbox_provider",
    "get_subagent_context",
    "get_thread_messages",
    "get_user_token_context",
    "glob",
    "grep",
    "read",
    "reset_checkpointer",
    "reset_sandbox_context",
    "reset_sandbox_provider",
    "reset_subagent_context",
    "reset_user_token_context",
    "resolve_guards",
    "resolve_middleware",
    "render_system_prompt_full",
    "render_system_prompt_simple",
    "resolve_variable",
    "run_agent",
    "run_agent_streaming",
    "set_sandbox_context",
    "set_sandbox_provider",
    "set_subagent_context",
    "set_user_token_context",
    "supports_thinking",
    "tool_search",
    "write",
    "__version__",
]

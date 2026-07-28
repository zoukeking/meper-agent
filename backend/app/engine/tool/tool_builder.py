"""ToolBuilder — unified entry point for building custom tools from DB docs.

Dispatches by ``source`` to the appropriate builder, returning a LangChain
``BaseTool`` ready for injection into the harness graph.

Supports:
- ``openapi``: HTTP endpoint with {{ }} template rendering + credential injection
- ``code``: User-defined Python code executed via Sandbox
- ``prebuilt``: Pre-registered tool from TOOL_REGISTRY
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
from langchain_core.tools import StructuredTool
from loguru import logger
from pydantic import BaseModel, create_model

# ---------------------------------------------------------------------------
# Template rendering — simple {{ }} replacement (no Jinja2 dependency)
# ---------------------------------------------------------------------------


def render_template(template: str, context: dict[str, Any]) -> str:
    """Replace ``{{ key.subkey }}`` placeholders with values from context."""
    if not isinstance(template, str):
        return str(template)

    def _replace(match: re.Match) -> str:
        path = match.group(1).strip()
        return _resolve_path(path, context)

    return re.sub(r"\{\{(.+?)\}\}", _replace, template)


def _resolve_path(path: str, context: dict[str, Any]) -> str:
    """Resolve a dotted path like 'credential.token' from context."""
    parts = path.split(".")
    value: Any = context
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part, "")
        else:
            value = getattr(value, part, "")
    return str(value) if value is not None else ""


def render_dict(d: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Recursively render template strings in a dict."""
    result: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = render_template(v, context)
        elif isinstance(v, dict):
            result[k] = render_dict(v, context)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# JSON Schema → Pydantic model (for StructuredTool args_schema)
# ---------------------------------------------------------------------------


def _json_schema_to_pydantic(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Convert a simple JSON Schema to a Pydantic model.

    Supports basic types: string, integer, number, boolean.
    Falls back to ``str`` for unknown types.
    """
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}

    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
    }

    for prop_name, prop_schema in properties.items():
        json_type = prop_schema.get("type", "string")
        py_type = type_map.get(json_type, str)
        default = ... if prop_name in required else None
        fields[prop_name] = (py_type | None if prop_name not in required else py_type, default)

    return create_model(f"{name}_Args", **fields)  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# ToolBuilder — unified entry
# ---------------------------------------------------------------------------


async def build_tool(tool_doc: dict, *, user_args: dict | None = None) -> StructuredTool | None:
    """Build a LangChain tool from a tool document.

    Args:
        tool_doc: Tool document from the ``tools`` collection.
        user_args: Agent 绑定时填入的用户参数值（已解密）。

    Returns:
        A ``StructuredTool`` ready for the harness graph, or ``None``.
    """
    source = tool_doc.get("source", "")
    name = tool_doc.get("name", "")
    description = tool_doc.get("description", "")
    user_args = user_args or {}

    try:
        if source == "openapi":
            return await _build_openapi_tool(tool_doc, name, description, user_args)
        if source == "code":
            return await _build_code_tool(tool_doc, name, description, user_args)
        if source == "prebuilt":
            return _build_prebuilt_tool(tool_doc, name, description)
        logger.warning("tool_builder_unknown_source", name=name, source=source)
        return None
    except Exception as exc:
        logger.error("tool_builder_failed", name=name, source=source, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# OpenApiToolBuilder
# ---------------------------------------------------------------------------


async def _build_openapi_tool(
    tool_doc: dict, name: str, description: str, user_args: dict
) -> StructuredTool:
    """Build a tool from an OpenAPI endpoint configuration.

    Templates: {{user.xxx}} → user_args, {{llm.xxx}} → LLM runtime input.
    """
    endpoint = tool_doc.get("endpoint", {})
    llm_args_schema = tool_doc.get("llm_args_schema", {})

    args_model = _json_schema_to_pydantic(name, llm_args_schema)

    async def _handler(**kwargs: Any) -> str:
        context = {
            "user": user_args,   # Agent 绑定时填入（含解密后的凭据）
            "llm": kwargs,       # LLM 运行时填入
        }

        method = render_template(endpoint.get("method", "GET"), context).upper()
        url = render_template(endpoint.get("url", ""), context)
        headers = render_dict(endpoint.get("headers", {}), context)
        params = render_dict(endpoint.get("params", {}), context)
        body = endpoint.get("body")
        if body:
            body = render_dict(body, context)
        timeout = float(user_args.get("timeout", 30)) if "timeout" in user_args else 30.0

        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers, params=params)
            elif method == "POST":
                resp = await client.post(url, headers=headers, params=params, json=body)
            elif method == "PUT":
                resp = await client.put(url, headers=headers, params=params, json=body)
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers, params=params)
            else:
                resp = await client.request(method, url, headers=headers, params=params, json=body)

        # Extract response path if configured
        response_path = endpoint.get("response_path", "")
        if response_path:
            try:
                data = resp.json()
                for part in response_path.split("."):
                    data = data.get(part, data) if isinstance(data, dict) else data
                return json.dumps(data, ensure_ascii=False, default=str)
            except Exception:
                return resp.text
        return resp.text

    return StructuredTool.from_function(
        coroutine=_handler,
        name=name,
        description=description,
        args_schema=args_model,
    )


# ---------------------------------------------------------------------------
# CodeToolBuilder
# ---------------------------------------------------------------------------


async def _build_code_tool(
    tool_doc: dict, name: str, description: str, user_args: dict
) -> StructuredTool:
    """Build a tool from user-defined Python code.

    User args (含敏感字段) are injected as environment variables.
    LLM args are passed as function parameters.
    """
    code = tool_doc.get("code", "")
    llm_args_schema = tool_doc.get("llm_args_schema", {})

    args_model = _json_schema_to_pydantic(name, llm_args_schema)

    async def _handler(**kwargs: Any) -> str:
        # User args → environment variables
        user_env: dict[str, str] = {}
        for k, v in user_args.items():
            user_env[f"USER_{k}"] = str(v)

        # Execute code in sandbox
        from agent_flow_harness import get_sandbox_context

        sandbox_ctx = get_sandbox_context()
        if sandbox_ctx is None:
            # Fallback: exec in a restricted namespace (not recommended for production)
            logger.warning("code_tool_no_sandbox", name=name)
            namespace: dict[str, Any] = {}
            exec(code, namespace)  # noqa: S102
            # Find the function
            func = namespace.get(name) or namespace.get("run")
            if func is None:
                return f"Error: function '{name}' or 'run' not found in code"
            result = func(**kwargs)
            return str(result)

        # Use sandbox bash to run the code
        import os
        import tempfile

        full_code = _wrap_code_for_sandbox(code, name, kwargs)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(full_code)
            script_path = f.name

        try:
            result = await sandbox_ctx.sandbox.run(
                f"python3 {os.path.basename(script_path)}",
                env=user_env,
            )
            return result.output if hasattr(result, "output") else str(result)
        finally:
            os.unlink(script_path)

    return StructuredTool.from_function(
        coroutine=_handler,
        name=name,
        description=description,
        args_schema=args_model,
    )


def _wrap_code_for_sandbox(code: str, func_name: str, kwargs: dict[str, Any]) -> str:
    """Wrap user code into a runnable script that calls the function and prints result."""
    return f"""
import json, sys

# --- User code ---
{code}
# --- End user code ---

# Call the function
_args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {{}}
_func = globals().get('{func_name}') or globals().get('run')
if _func is None:
    print("Error: function '{func_name}' or 'run' not found", file=sys.stderr)
    sys.exit(1)
_result = _func(**_args)
if not isinstance(_result, str):
    _result = json.dumps(_result, default=str, ensure_ascii=False)
print(_result)
"""


# ---------------------------------------------------------------------------
# PrebuiltToolBuilder
# ---------------------------------------------------------------------------


def _build_prebuilt_tool(
    tool_doc: dict, name: str, description: str
) -> StructuredTool | None:
    """Build a tool from the prebuilt tool registry.

    Looks up ``prebuilt_name`` in the harness ``TOOL_REGISTRY``. If it is a
    ``CommunityTool``, builds it with the tool's ``config``; if it is a plain
    ``BaseTool``, returns it directly.
    """
    from agent_flow_harness import TOOL_REGISTRY

    prebuilt_name = tool_doc.get("prebuilt_name", "")
    if not prebuilt_name:
        logger.warning("prebuilt_tool_missing_name", name=name)
        return None

    entry = TOOL_REGISTRY.get(prebuilt_name)
    if entry is None:
        logger.warning("prebuilt_tool_not_found", prebuilt_name=prebuilt_name)
        return None

    # Check if it's a CommunityTool (factory) or a BaseTool instance
    if hasattr(entry, "build") and hasattr(entry, "config_schema"):
        # CommunityTool factory — build with config
        config_data = tool_doc.get("config", {})
        config_cls = entry.config_schema
        try:
            config = config_cls(**config_data)
        except Exception as exc:
            logger.error("prebuilt_tool_config_error", name=prebuilt_name, error=str(exc))
            return None
        return entry.build(config)

    # Plain BaseTool instance — return directly
    return entry


__all__ = ["build_tool", "render_template"]
